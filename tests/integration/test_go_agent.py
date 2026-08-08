from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from patchpilot.agent.events import EventEmitter, JsonlEventSink
from patchpilot.agent.loop import AgentLoop, AgentLoopStatus
from patchpilot.agent.registry import build_default_registry
from patchpilot.artifacts import ArtifactKind, ArtifactStore
from patchpilot.domain.task import TaskSpec
from patchpilot.models.base import ModelConfig, ModelResponse, TokenUsage, ToolCall
from patchpilot.models.fake import ScriptedModelClient
from patchpilot.quality.gate import QualityGate
from patchpilot.sandbox.docker import DockerSandbox
from patchpilot.sandbox.workspace import WorkspaceManager
from patchpilot.tools.base import ToolContext, ToolLimits
from patchpilot.tools.tests import RunTestsInput, RunTestsTool
from tests.helpers import create_git_repository


def docker_binary() -> str:
    if os.environ.get("PATCHPILOT_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set PATCHPILOT_RUN_DOCKER_TESTS=1 to run Docker language tests")
    return os.environ.get("SANDBOX_DOCKER_BINARY", "docker")


def task_spec(
    valid_task_data: dict[str, Any],
    source: Path,
    *,
    language: str,
) -> TaskSpec:
    data = copy.deepcopy(valid_task_data)
    data["repository"]["path"] = str(source)
    data["repository"]["language"] = language
    if language == "go":
        data["allowed_paths"] = ["calculator.go", "calculator_test.go", "go.mod"]
        data["acceptance"] = {
            "commands": [{"argv": ["go", "test", "./..."], "timeout_seconds": 120}],
            "required_tests": ["TestAdd"],
        }
    else:
        data["allowed_paths"] = ["calculator.py", "tests/**"]
        data["acceptance"] = {
            "commands": [{"argv": ["python", "-m", "pytest"], "timeout_seconds": 30}],
            "required_tests": ["test_add"],
        }
    return TaskSpec.model_validate(data)


def response(call: ToolCall) -> ModelResponse:
    return ModelResponse(
        tool_calls=[call],
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="scripted-go",
        provider_request_id=f"script-{uuid4()}",
        latency_ms=0,
    )


@pytest.mark.docker
def test_python_profile_runs_acceptance_in_docker(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    source = create_git_repository(
        tmp_path / "python-source",
        {
            "calculator.py": "def add(left: int, right: int) -> int:\n    return left + right\n",
            "tests/test_calculator.py": (
                "from calculator import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    spec = task_spec(valid_task_data, source, language="python")
    workspace = WorkspaceManager(tmp_path / "python-workspaces").create(
        source, workspace_id="python-docker", base_ref="main"
    )
    runner = DockerSandbox(
        workspace=workspace.path,
        run_id=uuid4(),
        language="python",
        image="patchpilot-python:latest",
        cpu_limit=1,
        memory_limit_mb=256,
        pids_limit=64,
        docker_binary=docker_binary(),
    )
    context = ToolContext.create(workspace, spec, command_sandbox=runner)

    result = RunTestsTool(context).execute(RunTestsInput(profile_command_id="acceptance:0"))

    assert result.ok
    assert result.data is not None
    assert result.data["isolation"] == "docker"
    assert result.data["sandbox_image"] == "patchpilot-python:latest"


@pytest.mark.asyncio
@pytest.mark.docker
async def test_scripted_go_agent_passes_quality_gate_in_docker(
    tmp_path: Path,
    valid_task_data: dict[str, Any],
) -> None:
    original_code = (
        "package calculator\n\nfunc Add(left int, right int) int { return left - right }\n"
    )
    source = create_git_repository(
        tmp_path / "go-source",
        {
            "go.mod": "module example.com/calculator\n\ngo 1.26\n",
            "calculator.go": original_code,
            "calculator_test.go": (
                'package calculator\n\nimport "testing"\n\n'
                "func TestAdd(t *testing.T) {\n"
                '    if Add(2, 3) != 5 { t.Fatal("unexpected sum") }\n'
                "}\n"
            ),
        },
    )
    spec = task_spec(valid_task_data, source, language="go")
    workspace = WorkspaceManager(tmp_path / "go-workspaces").create(
        source, workspace_id="go-docker", base_ref="main"
    )
    run_id = uuid4()
    runner = DockerSandbox(
        workspace=workspace.path,
        run_id=run_id,
        language="go",
        image="patchpilot-go:latest",
        cpu_limit=2,
        memory_limit_mb=512,
        pids_limit=64,
        docker_binary=docker_binary(),
    )
    context = ToolContext.create(
        workspace,
        spec,
        ToolLimits(output_max_chars=30_000),
        command_sandbox=runner,
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", run_id)
    events = EventEmitter(
        run_id,
        [JsonlEventSink(artifacts.path_for(ArtifactKind.EVENT_LOG))],
    )
    gate = QualityGate(run_id=run_id, context=context, events=events, artifacts=artifacts)
    patch = """diff --git a/calculator.go b/calculator.go
--- a/calculator.go
+++ b/calculator.go
@@ -1,3 +1,3 @@
 package calculator
 
-func Add(left int, right int) int { return left - right }
+func Add(left int,right int)int{return left+right}
"""
    client = ScriptedModelClient(
        [
            response(ToolCall(id="patch", name="apply_patch", arguments={"patch": patch})),
            response(
                ToolCall(
                    id="gofmt",
                    name="run_linter",
                    arguments={"profile_command_id": "go:gofmt"},
                )
            ),
            response(
                ToolCall(
                    id="finish",
                    name="finish",
                    arguments={
                        "summary": "fixed Go arithmetic",
                        "tests_run": ["acceptance:0"],
                        "remaining_risks": [],
                    },
                )
            ),
        ]
    )
    loop = AgentLoop(
        model_client=client,
        model_config=ModelConfig(model="scripted-go", max_retries=0),
        tool_context=context,
        registry=build_default_registry(context),
        events=events,
        quality_gate=gate,
    )

    result = await loop.run(run_id)

    client.assert_exhausted()
    assert result.status == AgentLoopStatus.PASSED
    assert result.scorecard is not None
    assert result.scorecard.checks.tests_passed
    assert result.scorecard.checks.required_tests_ran
    assert (workspace.path / "calculator.go").read_text(encoding="utf-8") == (
        "package calculator\n\nfunc Add(left int, right int) int { return left + right }\n"
    )
    assert (source / "calculator.go").read_text(encoding="utf-8") == original_code
