from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from patchpilot.sandbox.base import SandboxError
from patchpilot.sandbox.workspace import PathPolicyError
from patchpilot.tools.base import ToolContext, ToolResult, failure, path_failure, success
from patchpilot.tools.git import collect_diff


class RunLinterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_command_id: Literal["go:vet", "go:gofmt"]


class RunLinterTool:
    """Run fixed Go lint/format commands inside the configured command sandbox."""

    name = "run_linter"

    def __init__(self, context: ToolContext) -> None:
        if context.task_spec.repository.language != "go":
            raise ValueError("RunLinterTool requires a Go TaskSpec")
        self.context = context

    def execute(
        self,
        arguments: RunLinterInput,
        *,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        try:
            argv = self._resolve_argv(arguments.profile_command_id)
        except PathPolicyError as exc:
            return path_failure(self.name, exc, started=started)
        except OSError as exc:
            return failure(self.name, "GIT_DIFF_ERROR", str(exc), started=started)

        if argv is None:
            return success(
                self.name,
                "no modified Go files require formatting",
                {
                    "profile_command_id": arguments.profile_command_id,
                    "argv": [],
                    "exit_code": 0,
                    "isolation": self.context.command_sandbox.isolation,
                    "sandbox_image": self.context.command_sandbox.image,
                },
                started=started,
            )

        timeout_seconds = min(60.0, timeout_seconds_override or 60.0)
        try:
            result = self.context.command_sandbox.run(
                argv,
                cwd=self.context.workspace.path,
                timeout_seconds=max(0.001, timeout_seconds),
                output_max_chars=self.context.limits.output_max_chars,
                environment={
                    "GONOSUMDB": "*",
                    "GOPROXY": "off",
                    "GOSUMDB": "off",
                    "GOTOOLCHAIN": "local",
                },
                cancel_event=self.context.cancellation_token.event,
            )
        except SandboxError as exc:
            return failure(self.name, "SANDBOX_ERROR", str(exc), started=started)
        except OSError as exc:
            return failure(
                self.name,
                "COMMAND_EXECUTION_ERROR",
                f"failed to start the configured lint command: {exc}",
                started=started,
            )

        data = {
            "profile_command_id": arguments.profile_command_id,
            "argv": list(argv),
            "exit_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "isolation": self.context.command_sandbox.isolation,
            "sandbox_image": self.context.command_sandbox.image,
        }
        if result.cancelled:
            return ToolResult(
                ok=False,
                tool=self.name,
                summary="linter cancelled",
                data=data,
                error={"code": "CANCELLED", "message": "lint process tree was cancelled"},
                truncated=result.truncated,
                duration_ms=result.duration_ms,
            )
        if result.timed_out:
            return ToolResult(
                ok=False,
                tool=self.name,
                summary=f"linter timed out after {timeout_seconds:g} seconds",
                data=data,
                error={"code": "TIMEOUT", "message": "lint process exceeded its timeout"},
                truncated=result.truncated,
                duration_ms=result.duration_ms,
            )
        return success(
            self.name,
            "linter passed" if result.return_code == 0 else "linter failed",
            data,
            started=started,
            truncated=result.truncated,
        )

    def _resolve_argv(self, command_id: str) -> tuple[str, ...] | None:
        if command_id == "go:vet":
            return ("go", "vet", "./...")

        snapshot = collect_diff(self.context, output_max_chars=100_000)
        changed_go_files: list[str] = []
        for logical_path in snapshot.changed_paths:
            if not logical_path.endswith(".go"):
                continue
            resolved = self.context.path_policy.resolve(logical_path, must_exist=False)
            if resolved.candidate_path.is_file():
                changed_go_files.append(resolved.logical_path)
        if not changed_go_files:
            return None
        return ("gofmt", "-w", *changed_go_files)
