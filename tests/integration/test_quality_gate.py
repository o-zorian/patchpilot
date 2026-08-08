from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from patchpilot.agent.events import (
    DatabaseEventSink,
    EventEmitter,
    EventSink,
    EventType,
    InMemoryEventSink,
    JsonlEventSink,
)
from patchpilot.agent.loop import AgentLoop, AgentLoopStatus
from patchpilot.agent.registry import build_default_registry
from patchpilot.artifacts import ArtifactKind, ArtifactStore
from patchpilot.domain.run import RunStatus, RunStrategy
from patchpilot.domain.scorecard import QualityResult
from patchpilot.domain.task import TaskSpec
from patchpilot.models.base import (
    Message,
    ModelConfig,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from patchpilot.models.fake import ScriptedModelClient
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.persistence.models import ArtifactRow
from patchpilot.persistence.repositories import RunRepository
from patchpilot.quality.gate import QualityGate
from patchpilot.sandbox.trusted_local import TrustedLocalSandbox
from patchpilot.sandbox.workspace import WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits
from tests.helpers import create_git_repository


def make_context(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    *,
    files: dict[str, str] | None = None,
    timeout_seconds: int = 30,
    max_patch_lines: int = 300,
) -> tuple[ToolContext, Path]:
    source = create_git_repository(
        tmp_path / "source",
        files
        or {
            "calculator.py": "def add(left: int, right: int) -> int:\n    return left - right\n",
            "tests/test_calculator.py": (
                "from calculator import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    data = dict(valid_task_data)
    data["repository"] = dict(valid_task_data["repository"])
    data["repository"]["path"] = str(source)
    data["allowed_paths"] = ["calculator.py", "value.py", "tests/**"]
    data["denied_paths"] = [".git/**", "README.md"]
    data["acceptance"] = {
        "commands": [{"argv": ["python", "-m", "pytest"], "timeout_seconds": timeout_seconds}],
        "required_tests": ["test_slow" if files and "tests/test_slow.py" in files else "test_add"],
    }
    data["budget"] = dict(valid_task_data["budget"])
    data["budget"]["max_patch_lines"] = max_patch_lines
    spec = TaskSpec.model_validate(data)
    workspace = WorkspaceManager(tmp_path / "workspaces").create(
        source,
        workspace_id="quality-run",
        base_ref="main",
    )
    return (
        ToolContext.create(
            workspace,
            spec,
            ToolLimits(output_max_chars=30_000, max_file_bytes=1_000_000),
            command_sandbox=TrustedLocalSandbox(),
        ),
        source,
    )


def response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=list(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="scripted-test",
        provider_request_id=f"script-{uuid4()}",
        latency_ms=0,
    )


def call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def finish(call_id: str) -> ToolCall:
    return call(
        call_id,
        "finish",
        {"summary": "ready for deterministic gate", "tests_run": [], "remaining_risks": []},
    )


def patch_call(call_id: str, old: str, new: str) -> ToolCall:
    patch = f"""diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return {old}
+    return {new}
"""
    return call(call_id, "apply_patch", {"patch": patch})


def build_quality_loop(
    context: ToolContext,
    client: ScriptedModelClient,
    run_id: UUID,
    artifact_root: Path,
    *,
    database: Database | None = None,
    configure_context: Callable[[ToolContext], object] | None = None,
) -> tuple[AgentLoop, InMemoryEventSink, ArtifactStore]:
    if configure_context is not None:
        configure_context(context)
    memory = InMemoryEventSink()
    artifacts = ArtifactStore(artifact_root, run_id, database=database)
    sinks: list[EventSink] = [
        memory,
        JsonlEventSink(artifacts.path_for(ArtifactKind.EVENT_LOG)),
    ]
    if database is not None:
        sinks.append(DatabaseEventSink(database))
    events = EventEmitter(run_id, sinks)
    gate = QualityGate(
        run_id=run_id,
        context=context,
        events=events,
        artifacts=artifacts,
    )
    loop = AgentLoop(
        model_client=client,
        model_config=ModelConfig(model="scripted-test", max_retries=0),
        tool_context=context,
        registry=build_default_registry(context),
        events=events,
        quality_gate=gate,
    )
    return loop, memory, artifacts


@pytest.mark.asyncio
async def test_quality_gate_passes_and_persists_hashed_reports(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, source = make_context(tmp_path, valid_task_data)
    original = (source / "calculator.py").read_bytes()
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'quality.db').as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)
    try:
        async with database.session() as session:
            run = await RunRepository(session).create(
                context.task_spec,
                strategy=RunStrategy.FULL,
                model="scripted-test",
                prompt_version="m2-v1",
            )
        client = ScriptedModelClient(
            [
                response(patch_call("patch", "left - right", "left + right")),
                response(finish("finish")),
            ]
        )
        loop, memory, artifacts = build_quality_loop(
            context,
            client,
            run.id,
            tmp_path / "artifacts",
            database=database,
        )

        result = await loop.run(run.id)
        client.assert_exhausted()

        async with database.session() as session:
            persisted = await RunRepository(session).get(run.id)
            rows = list(
                (
                    await session.scalars(
                        select(ArtifactRow).where(ArtifactRow.run_id == str(run.id))
                    )
                ).all()
            )
    finally:
        await database.close()

    assert result.status == AgentLoopStatus.PASSED
    assert result.result_code == QualityResult.PASSED.value
    assert result.scorecard is not None
    assert persisted.status == RunStatus.PASSED
    assert persisted.result_code == QualityResult.PASSED.value
    assert persisted.workspace_id == context.workspace.id
    assert persisted.started_at is not None
    assert persisted.finished_at is not None
    assert result.scorecard.checks.model_dump() == {
        "has_patch": True,
        "scope_valid": True,
        "patch_size_valid": True,
        "tests_passed": True,
        "required_tests_ran": True,
        "budget_valid": True,
    }
    assert (source / "calculator.py").read_bytes() == original
    assert len(rows) == len(ArtifactKind)
    assert {row.kind for row in rows} == {kind.value for kind in ArtifactKind}
    for row in rows:
        path = artifacts.root / row.path
        data = path.read_bytes()
        assert row.sha256 == hashlib.sha256(data).hexdigest()
        assert row.size_bytes == len(data)
    assert "return left + right" in artifacts.path_for(ArtifactKind.PATCH).read_text(
        encoding="utf-8"
    )
    assert "Result: **PASSED**" in artifacts.path_for(ArtifactKind.REPORT_MARKDOWN).read_text(
        encoding="utf-8"
    )
    assert EventType.QUALITY_GATE_PASSED in {event.type for event in memory.events}


@pytest.mark.asyncio
async def test_gate_failure_feedback_allows_second_fix(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)

    def react_to_gate(
        messages: list[Message],
        _: list[ToolSchema],
        __: ModelConfig,
    ) -> ModelResponse:
        feedback = json.loads(messages[-1].content or "{}")
        assert feedback["type"] == "quality_gate_feedback"
        assert feedback["result"] == QualityResult.TEST_FAILURE.value
        assert "test_add" in " ".join(feedback["failed_tests"])
        return response(patch_call("correct", "left * right", "left + right"))

    client = ScriptedModelClient(
        [
            response(patch_call("wrong", "left - right", "left * right")),
            response(finish("first-finish")),
            react_to_gate,
            response(finish("second-finish")),
        ]
    )
    run_id = uuid4()
    loop, memory, _ = build_quality_loop(
        context,
        client,
        run_id,
        tmp_path / "artifacts",
    )

    result = await loop.run(run_id)
    client.assert_exhausted()

    assert result.status == AgentLoopStatus.PASSED
    assert result.scorecard is not None
    assert [failure.result for failure in result.scorecard.failures] == [QualityResult.TEST_FAILURE]
    event_types = [event.type for event in memory.events]
    assert event_types.count(EventType.QUALITY_GATE_FAILED) == 1
    assert event_types.count(EventType.QUALITY_GATE_PASSED) == 1


@pytest.mark.asyncio
async def test_no_patch_stops_after_two_gate_feedbacks(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    client = ScriptedModelClient(
        [response(finish("finish-1")), response(finish("finish-2")), response(finish("finish-3"))]
    )
    run_id = uuid4()
    loop, memory, _ = build_quality_loop(
        context,
        client,
        run_id,
        tmp_path / "artifacts",
    )

    result = await loop.run(run_id)
    client.assert_exhausted()

    assert result.status == AgentLoopStatus.FAILED
    assert result.result_code == QualityResult.NO_PATCH.value
    assert result.scorecard is not None
    assert len(result.scorecard.failures) == 3
    assert [event.type for event in memory.events].count(EventType.QUALITY_GATE_FAILED) == 3


@pytest.mark.asyncio
async def test_persistent_test_failure_stops_after_two_gate_feedbacks(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    client = ScriptedModelClient(
        [
            response(patch_call("wrong", "left - right", "left * right")),
            response(finish("finish-1")),
            response(finish("finish-2")),
            response(finish("finish-3")),
        ]
    )
    run_id = uuid4()
    loop, memory, _ = build_quality_loop(
        context,
        client,
        run_id,
        tmp_path / "artifacts",
    )

    result = await loop.run(run_id)
    client.assert_exhausted()

    assert result.status == AgentLoopStatus.FAILED
    assert result.result_code == QualityResult.TEST_FAILURE.value
    assert result.scorecard is not None
    assert len(result.scorecard.failures) == 3
    assert {failure.result for failure in result.scorecard.failures} == {QualityResult.TEST_FAILURE}
    assert [event.type for event in memory.events].count(EventType.QUALITY_GATE_FAILED) == 3


@pytest.mark.parametrize(
    ("mutation", "max_patch_lines", "expected"),
    [
        (
            lambda context: (context.workspace.path / "README.md").write_text(
                "outside allowed paths\n", encoding="utf-8"
            ),
            300,
            QualityResult.SCOPE_VIOLATION,
        ),
        (
            lambda context: (context.workspace.path / "calculator.py").write_text(
                "def add(left: int, right: int) -> int:\n    return left + right\n",
                encoding="utf-8",
            ),
            1,
            QualityResult.PATCH_TOO_LARGE,
        ),
    ],
    ids=["scope-violation", "patch-too-large"],
)
@pytest.mark.asyncio
async def test_nonrecoverable_gate_failures_do_not_return_to_model(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    mutation: Callable[[ToolContext], int],
    max_patch_lines: int,
    expected: QualityResult,
) -> None:
    context, source = make_context(
        tmp_path,
        valid_task_data,
        max_patch_lines=max_patch_lines,
    )
    original_status = source.joinpath("calculator.py").read_bytes()
    client = ScriptedModelClient([response(finish("finish"))])
    run_id = uuid4()
    loop, _, _ = build_quality_loop(
        context,
        client,
        run_id,
        tmp_path / "artifacts",
        configure_context=mutation,
    )

    result = await loop.run(run_id)
    client.assert_exhausted()

    assert result.status == AgentLoopStatus.FAILED
    assert result.result_code == expected.value
    assert source.joinpath("calculator.py").read_bytes() == original_status


@pytest.mark.asyncio
async def test_gate_timeout_is_deterministic(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(
        tmp_path,
        valid_task_data,
        files={
            "value.py": "VALUE = 0\n",
            "tests/test_slow.py": ("import time\n\ndef test_slow() -> None:\n    time.sleep(20)\n"),
        },
        timeout_seconds=1,
    )
    (context.workspace.path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedModelClient([response(finish("finish"))])
    run_id = uuid4()
    loop, _, _ = build_quality_loop(
        context,
        client,
        run_id,
        tmp_path / "artifacts",
    )

    result = await loop.run(run_id)

    assert result.status == AgentLoopStatus.TIMEOUT
    assert result.result_code == QualityResult.TIMEOUT.value
    assert result.scorecard is not None
    assert result.scorecard.checks.tests_passed is False


@pytest.mark.asyncio
async def test_agent_budget_terminal_still_generates_scorecard_and_reports(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    limited_spec = context.task_spec.model_copy(
        update={"budget": context.task_spec.budget.model_copy(update={"max_steps": 1})}
    )
    context = ToolContext.create(
        context.workspace,
        limited_spec,
        context.limits,
        command_sandbox=context.command_sandbox,
    )
    client = ScriptedModelClient([response(content="no tool call")])
    run_id = uuid4()
    loop, _, artifacts = build_quality_loop(
        context,
        client,
        run_id,
        tmp_path / "artifacts",
    )

    result = await loop.run(run_id)

    assert result.status == AgentLoopStatus.BUDGET_EXCEEDED
    assert result.result_code == QualityResult.BUDGET_EXCEEDED.value
    assert result.scorecard is not None
    assert result.scorecard.result == QualityResult.BUDGET_EXCEEDED
    assert artifacts.path_for(ArtifactKind.SCORECARD).is_file()
    assert artifacts.path_for(ArtifactKind.REPORT_HTML).is_file()
