from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patchpilot.models.base import ToolCall, ToolSchema
from patchpilot.tools.base import ToolContext, ToolResult, failure, success
from patchpilot.tools.files import (
    ListFilesInput,
    ListFilesTool,
    ReadFileInput,
    ReadFileTool,
)
from patchpilot.tools.git import GitDiffInput, GitDiffTool
from patchpilot.tools.patch import ApplyPatchInput, ApplyPatchTool
from patchpilot.tools.search import SearchCodeInput, SearchCodeTool
from patchpilot.tools.tests import RunTestsInput, RunTestsTool


class FinishInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=4_000)
    tests_run: list[str] = Field(default_factory=list, max_length=100)
    remaining_risks: list[str] = Field(default_factory=list, max_length=100)


@dataclass(frozen=True, slots=True)
class RegistryExecution:
    result: ToolResult
    finish_request: FinishInput | None = None


@dataclass(frozen=True, slots=True)
class _Registration:
    description: str
    input_model: type[BaseModel]
    execute: Callable[[Any, float | None], ToolResult] | None


class ToolRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, _Registration] = {}

    def register(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel],
        execute: Callable[[Any, float | None], ToolResult] | None,
    ) -> None:
        if name in self._registrations:
            raise ValueError(f"tool is already registered: {name}")
        self._registrations[name] = _Registration(
            description=description,
            input_model=input_model,
            execute=execute,
        )

    @property
    def schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name=name,
                description=registration.description,
                parameters=registration.input_model.model_json_schema(),
            )
            for name, registration in self._registrations.items()
        ]

    def execute(
        self,
        call: ToolCall,
        *,
        timeout_seconds: float | None = None,
    ) -> RegistryExecution:
        started = time.monotonic()
        registration = self._registrations.get(call.name)
        if registration is None:
            return RegistryExecution(
                result=failure(
                    call.name,
                    "UNKNOWN_TOOL",
                    f"tool is not registered: {call.name}",
                    started=started,
                )
            )
        try:
            raw_arguments = (
                json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            )
            if not isinstance(raw_arguments, dict):
                raise ValueError("tool arguments must decode to a JSON object")
            arguments = registration.input_model.model_validate(raw_arguments)
        except json.JSONDecodeError:
            return RegistryExecution(
                result=failure(
                    call.name,
                    "INVALID_TOOL_CALL",
                    "tool arguments are not valid JSON",
                    started=started,
                )
            )
        except (ValidationError, ValueError):
            return RegistryExecution(
                result=failure(
                    call.name,
                    "INVALID_TOOL_CALL",
                    "tool arguments failed schema validation",
                    started=started,
                )
            )

        if call.name == "finish":
            if not isinstance(arguments, FinishInput):
                raise RuntimeError("finish registration has an invalid input model")
            return RegistryExecution(
                result=success(
                    "finish",
                    "finish request accepted for later Quality Gate evaluation",
                    {
                        "summary": arguments.summary,
                        "tests_run": arguments.tests_run,
                        "remaining_risks": arguments.remaining_risks,
                    },
                    started=started,
                ),
                finish_request=arguments,
            )
        if registration.execute is None:
            raise RuntimeError(f"tool registration has no executor: {call.name}")
        try:
            return RegistryExecution(result=registration.execute(arguments, timeout_seconds))
        except Exception as exc:
            return RegistryExecution(
                result=failure(
                    call.name,
                    "TOOL_ERROR",
                    f"tool execution failed: {type(exc).__name__}",
                    started=started,
                )
            )


def build_default_registry(context: ToolContext) -> ToolRegistry:
    registry = ToolRegistry()
    list_files = ListFilesTool(context)
    search_code = SearchCodeTool(context)
    read_file = ReadFileTool(context)
    apply_patch = ApplyPatchTool(context)
    git_diff = GitDiffTool(context)
    run_tests = RunTestsTool(context)
    registry.register(
        "list_files",
        "List bounded file paths below a Workspace-relative directory.",
        ListFilesInput,
        lambda arguments, _: list_files.execute(arguments),
    )
    registry.register(
        "search_code",
        "Search bounded UTF-8 source text inside the Workspace.",
        SearchCodeInput,
        lambda arguments, _: search_code.execute(arguments),
    )
    registry.register(
        "read_file",
        "Read a bounded line range from a UTF-8 Workspace file.",
        ReadFileInput,
        lambda arguments, _: read_file.execute(arguments),
    )
    registry.register(
        "apply_patch",
        "Atomically apply a scoped, budgeted unified text patch.",
        ApplyPatchInput,
        lambda arguments, _: apply_patch.execute(arguments),
    )
    registry.register(
        "git_diff",
        "Return the bounded Workspace Git diff or its statistics.",
        GitDiffInput,
        lambda arguments, _: git_diff.execute(arguments),
    )
    registry.register(
        "run_tests",
        "Run an application-defined Python pytest profile command.",
        RunTestsInput,
        lambda arguments, timeout: run_tests.execute(
            arguments,
            timeout_seconds_override=timeout,
        ),
    )
    registry.register(
        "finish",
        "Request deterministic Quality Gate evaluation; this does not mark the Run passed.",
        FinishInput,
        None,
    )
    return registry
