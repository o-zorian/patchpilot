from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict

from patchpilot.agent.registry import FinishInput, ToolRegistry
from patchpilot.models.base import ToolCall
from patchpilot.tools.base import success
from patchpilot.tools.patch import ApplyPatchInput


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "echo",
        "Return a validated value.",
        EchoInput,
        lambda arguments, _: success(
            "echo",
            "echoed",
            {"value": arguments.value},
            started=time.monotonic(),
        ),
    )
    registry.register("finish", "Request gate evaluation.", FinishInput, None)
    return registry


def test_registry_returns_structured_unknown_and_invalid_results() -> None:
    registry = build_registry()

    unknown = registry.execute(ToolCall(id="1", name="shell", arguments={}))
    malformed = registry.execute(ToolCall(id="2", name="echo", arguments='{"value":"secret"'))
    extra = registry.execute(
        ToolCall(
            id="3",
            name="echo",
            arguments={"value": "safe", "api_key": "must-not-be-echoed"},
        )
    )

    assert unknown.result.error is not None
    assert malformed.result.error is not None
    assert extra.result.error is not None
    assert unknown.result.error.code == "UNKNOWN_TOOL"
    assert malformed.result.error.code == "INVALID_TOOL_CALL"
    assert extra.result.error.code == "INVALID_TOOL_CALL"
    assert "must-not-be-echoed" not in extra.result.model_dump_json()


def test_registry_executes_validated_calls_and_finish_is_only_a_request() -> None:
    registry = build_registry()

    echo = registry.execute(ToolCall(id="1", name="echo", arguments={"value": "hello"}))
    finished = registry.execute(
        ToolCall(
            id="2",
            name="finish",
            arguments={
                "summary": "implementation complete",
                "tests_run": ["pytest"],
                "remaining_risks": [],
            },
        )
    )

    assert echo.result.ok
    assert echo.result.data == {"value": "hello"}
    assert finished.result.ok
    assert finished.finish_request is not None
    assert finished.finish_request.summary == "implementation complete"
    assert finished.result.data is not None
    assert "passed" not in finished.result.data


def test_registry_contains_only_explicit_tools() -> None:
    schema_names = {schema.name for schema in build_registry().schemas}

    assert schema_names == {"echo", "finish"}


def test_apply_patch_schema_explains_the_required_git_diff_format() -> None:
    schema = ApplyPatchInput.model_json_schema()
    description = schema["properties"]["patch"]["description"]

    assert "diff --git a/path b/path" in description
    assert "@@" in description
    assert "Do not use '*** Begin Patch'" in description
