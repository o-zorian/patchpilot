from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchpilot.domain.task import TaskSpec
from patchpilot.sandbox.workspace import PathPolicyError, Workspace, WorkspacePathPolicy


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    tool: str
    summary: str
    data: dict[str, Any] | None
    error: ToolError | None
    truncated: bool = False
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_success_error_pair(self) -> ToolResult:
        if self.ok == (self.error is not None):
            raise ValueError(
                "successful results cannot have errors and failed results must have errors"
            )
        return self


@dataclass(frozen=True, slots=True)
class ToolLimits:
    output_max_chars: int = 20_000
    list_max_files: int = 1_000
    search_max_results: int = 100
    read_max_lines: int = 400
    max_file_bytes: int = 1_048_576


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Workspace
    task_spec: TaskSpec
    limits: ToolLimits
    path_policy: WorkspacePathPolicy

    @classmethod
    def create(
        cls,
        workspace: Workspace,
        task_spec: TaskSpec,
        limits: ToolLimits | None = None,
    ) -> ToolContext:
        return cls(
            workspace=workspace,
            task_spec=task_spec,
            limits=limits or ToolLimits(),
            path_policy=WorkspacePathPolicy(
                workspace,
                allowed_paths=task_spec.allowed_paths,
                denied_paths=task_spec.denied_paths,
            ),
        )


def elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1_000))


def success(
    tool: str,
    summary: str,
    data: dict[str, Any],
    *,
    started: float,
    truncated: bool = False,
) -> ToolResult:
    return ToolResult(
        ok=True,
        tool=tool,
        summary=summary,
        data=data,
        error=None,
        truncated=truncated,
        duration_ms=elapsed_ms(started),
    )


def failure(
    tool: str,
    code: str,
    message: str,
    *,
    started: float,
    summary: str | None = None,
) -> ToolResult:
    return ToolResult(
        ok=False,
        tool=tool,
        summary=summary or message,
        data=None,
        error=ToolError(code=code, message=message),
        truncated=False,
        duration_ms=elapsed_ms(started),
    )


def path_failure(tool: str, error: PathPolicyError, *, started: float) -> ToolResult:
    return failure(tool, error.code, str(error), started=started)
