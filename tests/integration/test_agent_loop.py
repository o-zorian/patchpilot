from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
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
from patchpilot.domain.run import RunStrategy
from patchpilot.domain.task import TaskSpec
from patchpilot.models.base import (
    Message,
    ModelClientError,
    ModelConfig,
    ModelRateLimitError,
    ModelResponse,
    ModelServerError,
    ModelTimeoutError,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from patchpilot.models.fake import ScriptedModelClient
from patchpilot.persistence.database import Database
from patchpilot.persistence.migrations import upgrade_database
from patchpilot.persistence.models import EventRow, ModelCallRow, ToolCallRow
from patchpilot.persistence.repositories import RunRepository
from patchpilot.sandbox.trusted_local import TrustedLocalSandbox
from patchpilot.sandbox.workspace import WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits
from tests.helpers import create_git_repository


def make_context(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    *,
    budget: dict[str, object] | None = None,
) -> tuple[ToolContext, Path]:
    source = create_git_repository(
        tmp_path / "source",
        {
            "calculator.py": ("def add(left: int, right: int) -> int:\n    return left - right\n"),
            "tests/test_calculator.py": (
                "from calculator import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    data = dict(valid_task_data)
    data["repository"] = dict(valid_task_data["repository"])
    data["repository"]["path"] = str(source)
    data["allowed_paths"] = ["calculator.py", "tests/**"]
    data["denied_paths"] = [".git/**"]
    data["acceptance"] = {
        "commands": [{"argv": ["python", "-m", "pytest"], "timeout_seconds": 30}],
        "required_tests": ["test_add"],
    }
    data["budget"] = budget or dict(valid_task_data["budget"])
    spec = TaskSpec.model_validate(data)
    workspace = WorkspaceManager(tmp_path / "workspaces").create(
        source,
        workspace_id="agent-run",
        base_ref="main",
    )
    return (
        ToolContext.create(
            workspace,
            spec,
            ToolLimits(output_max_chars=20_000, max_file_bytes=1_000_000),
            command_sandbox=TrustedLocalSandbox(),
        ),
        source,
    )


def response(
    *calls: ToolCall,
    content: str | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=list(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model="scripted-test",
        provider_request_id=f"script-{uuid4()}",
        latency_ms=0,
    )


def call(call_id: str, name: str, arguments: dict[str, object] | str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def config(**updates: object) -> ModelConfig:
    values: dict[str, object] = {
        "model": "scripted-test",
        "max_retries": 0,
        "retry_base_seconds": 0,
    }
    values.update(updates)
    return ModelConfig.model_validate(values)


def build_loop(
    context: ToolContext,
    client: ScriptedModelClient,
    run_id: UUID,
    *,
    sinks: list[EventSink] | None = None,
    model_config: ModelConfig | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[AgentLoop, InMemoryEventSink]:
    memory = InMemoryEventSink()
    selected_sinks = [memory, *(sinks or [])]
    emitter = EventEmitter(run_id, selected_sinks)
    if sleep is None:
        loop = AgentLoop(
            model_client=client,
            model_config=model_config or config(),
            tool_context=context,
            registry=build_default_registry(context),
            events=emitter,
        )
    else:
        loop = AgentLoop(
            model_client=client,
            model_config=model_config or config(),
            tool_context=context,
            registry=build_default_registry(context),
            events=emitter,
            sleep=sleep,
        )
    return loop, memory


@pytest.mark.asyncio
async def test_agent_loop_repairs_only_workspace_and_records_jsonl(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, source = make_context(tmp_path, valid_task_data)
    original = (source / "calculator.py").read_text(encoding="utf-8")
    patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
"""
    client = ScriptedModelClient(
        [
            response(call("read", "read_file", {"path": "calculator.py"})),
            response(call("patch", "apply_patch", {"patch": patch})),
            response(call("tests", "run_tests", {"profile_command_id": "acceptance:0"})),
            response(
                call(
                    "finish",
                    "finish",
                    {
                        "summary": "fixed addition",
                        "tests_run": ["acceptance:0"],
                        "remaining_risks": [],
                    },
                )
            ),
        ]
    )
    run_id = uuid4()
    jsonl_path = tmp_path / "artifacts" / "events.jsonl"
    loop, memory = build_loop(
        context,
        client,
        run_id,
        sinks=[JsonlEventSink(jsonl_path)],
    )

    result = await loop.run(run_id)
    client.assert_exhausted()
    jsonl_events = [
        json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]

    assert result.status == AgentLoopStatus.FINISH_REQUESTED
    assert result.result_code == "FINISH_REQUESTED"
    assert result.finish_request is not None
    assert result.metrics.steps == 4
    assert result.metrics.tool_calls == 4
    assert (source / "calculator.py").read_text(encoding="utf-8") == original
    assert "return left + right" in (context.workspace.path / "calculator.py").read_text(
        encoding="utf-8"
    )
    assert [event["sequence"] for event in jsonl_events] == list(range(1, len(jsonl_events) + 1))
    assert [event["type"] for event in jsonl_events] == [
        event.type.value for event in memory.events
    ]
    assert EventType.RUN_COMPLETED in {event.type for event in memory.events}
    assert "return left + right" not in jsonl_path.read_text(encoding="utf-8")
    test_feedback = json.loads(client.calls[3].messages[-1].content or "{}")
    assert test_feedback["data"]["passed"] is True


@pytest.mark.asyncio
async def test_agent_loop_uses_failed_test_feedback_for_a_second_fix(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    wrong_patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left * right
"""
    correct_patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left * right
+    return left + right
"""

    def react_to_failure(
        messages: list[Message],
        _: list[ToolSchema],
        __: ModelConfig,
    ) -> ModelResponse:
        feedback = json.loads(messages[-1].content or "{}")
        assert feedback["data"]["passed"] is False
        assert feedback["data"]["exit_code"] != 0
        return response(call("correct", "apply_patch", {"patch": correct_patch}))

    client = ScriptedModelClient(
        [
            response(call("wrong", "apply_patch", {"patch": wrong_patch})),
            response(call("first-tests", "run_tests", {"profile_command_id": "acceptance:0"})),
            react_to_failure,
            response(call("second-tests", "run_tests", {"profile_command_id": "acceptance:0"})),
            response(
                call(
                    "finish",
                    "finish",
                    {
                        "summary": "fixed after observing the test failure",
                        "tests_run": ["acceptance:0", "acceptance:0"],
                        "remaining_risks": [],
                    },
                )
            ),
        ]
    )
    run_id = uuid4()
    loop, _ = build_loop(context, client, run_id)

    result = await loop.run(run_id)
    client.assert_exhausted()

    assert result.status == AgentLoopStatus.FINISH_REQUESTED
    final_test_feedback = json.loads(client.calls[4].messages[-1].content or "{}")
    assert final_test_feedback["data"]["passed"] is True


@pytest.mark.asyncio
async def test_agent_loop_stops_three_identical_invalid_tool_calls(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    client = ScriptedModelClient(
        [response(call(f"invalid-{index}", "shell", {"command": "pytest"})) for index in range(3)]
    )
    run_id = uuid4()
    loop, memory = build_loop(context, client, run_id)

    result = await loop.run(run_id)

    assert result.status == AgentLoopStatus.FAILED
    assert result.result_code == "INVALID_TOOL_LOOP"
    assert result.error_code == "INVALID_TOOL_LOOP"
    assert result.metrics.tool_calls == 3
    assert memory.events[-1].type == EventType.RUN_FAILED


@pytest.mark.parametrize(
    "transient_error",
    [
        ModelRateLimitError("scripted 429"),
        ModelServerError("scripted 503"),
        ModelTimeoutError("scripted timeout"),
    ],
    ids=["rate-limit", "server-error", "timeout"],
)
@pytest.mark.asyncio
async def test_agent_loop_retries_transient_model_failure_with_scripted_model(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
    transient_error: ModelClientError,
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    client = ScriptedModelClient(
        [
            transient_error,
            response(
                call(
                    "finish",
                    "finish",
                    {
                        "summary": "retry succeeded",
                        "tests_run": [],
                        "remaining_risks": ["tests not requested in this retry test"],
                    },
                )
            ),
        ]
    )
    delays: list[float] = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    run_id = uuid4()
    loop, memory = build_loop(
        context,
        client,
        run_id,
        model_config=config(max_retries=1, retry_base_seconds=0),
        sleep=no_wait,
    )

    result = await loop.run(run_id)

    assert result.status == AgentLoopStatus.FINISH_REQUESTED
    assert result.metrics.model_calls == 2
    assert delays == [0]
    assert EventType.MODEL_RETRYING in {event.type for event in memory.events}


@pytest.mark.asyncio
async def test_agent_loop_enforces_step_and_token_budgets_before_tools(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    step_budget = dict(valid_task_data["budget"])
    step_budget["max_steps"] = 2
    step_context, _ = make_context(tmp_path / "step", valid_task_data, budget=step_budget)
    step_client = ScriptedModelClient(
        [response(content="thinking"), response(content="still thinking")]
    )
    step_run_id = uuid4()
    step_loop, _ = build_loop(step_context, step_client, step_run_id)

    step_result = await step_loop.run(step_run_id)

    token_budget = dict(valid_task_data["budget"])
    token_budget["max_input_tokens"] = 10
    token_context, _ = make_context(tmp_path / "token", valid_task_data, budget=token_budget)
    token_client = ScriptedModelClient(
        [
            response(
                call(
                    "must-not-run",
                    "apply_patch",
                    {"patch": "not a patch and must not be executed"},
                ),
                prompt_tokens=10,
                completion_tokens=0,
            )
        ]
    )
    token_run_id = uuid4()
    token_loop, _ = build_loop(token_context, token_client, token_run_id)

    token_result = await token_loop.run(token_run_id)

    assert step_result.status == AgentLoopStatus.BUDGET_EXCEEDED
    assert step_result.error_code == "max_steps"
    assert token_result.status == AgentLoopStatus.BUDGET_EXCEEDED
    assert token_result.error_code == "max_input_tokens"
    assert token_result.metrics.tool_calls == 0
    assert "return left - right" in (token_context.workspace.path / "calculator.py").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_agent_loop_enforces_cost_budget_before_tools(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    cost_budget = dict(valid_task_data["budget"])
    cost_budget["max_cost_usd"] = Decimal("0.00001")
    context, _ = make_context(tmp_path, valid_task_data, budget=cost_budget)
    client = ScriptedModelClient(
        [
            response(
                call(
                    "must-not-run",
                    "apply_patch",
                    {"patch": "not a patch and must not be executed"},
                ),
                prompt_tokens=10,
                completion_tokens=0,
            )
        ]
    )
    run_id = uuid4()
    loop, _ = build_loop(
        context,
        client,
        run_id,
        model_config=config(input_cost_per_million_usd=Decimal("1")),
    )

    result = await loop.run(run_id)

    assert result.status == AgentLoopStatus.BUDGET_EXCEEDED
    assert result.error_code == "max_cost_usd"
    assert result.metrics.tool_calls == 0


@pytest.mark.asyncio
async def test_event_trace_is_semantically_identical_in_jsonl_and_database(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    context, _ = make_context(tmp_path, valid_task_data)
    database_path = tmp_path / "trace.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)
    jsonl_path = tmp_path / "artifacts" / "trace.jsonl"

    try:
        async with database.session() as session:
            persisted_run = await RunRepository(session).create(
                context.task_spec,
                strategy=RunStrategy.AGENT_LOOP,
                model="scripted-test",
                prompt_version="m2-v1",
            )
        client = ScriptedModelClient(
            [
                response(
                    call(
                        "finish",
                        "finish",
                        {
                            "summary": "trace persistence complete",
                            "tests_run": [],
                            "remaining_risks": [],
                        },
                    )
                )
            ]
        )
        loop, memory = build_loop(
            context,
            client,
            persisted_run.id,
            sinks=[JsonlEventSink(jsonl_path), DatabaseEventSink(database)],
        )
        result = await loop.run(persisted_run.id)

        async with database.session() as session:
            event_rows = list(
                (
                    await session.scalars(
                        select(EventRow)
                        .where(EventRow.run_id == str(persisted_run.id))
                        .order_by(EventRow.sequence)
                    )
                ).all()
            )
            model_rows = list(
                (
                    await session.scalars(
                        select(ModelCallRow).where(ModelCallRow.run_id == str(persisted_run.id))
                    )
                ).all()
            )
            tool_rows = list(
                (
                    await session.scalars(
                        select(ToolCallRow).where(ToolCallRow.run_id == str(persisted_run.id))
                    )
                ).all()
            )
    finally:
        await database.close()

    jsonl_events = [
        json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert result.status == AgentLoopStatus.FINISH_REQUESTED
    assert [row.sequence for row in event_rows] == [event["sequence"] for event in jsonl_events]
    assert [row.event_type for row in event_rows] == [event.type.value for event in memory.events]
    assert len(model_rows) == 1
    assert model_rows[0].status == "completed"
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_name == "finish"
