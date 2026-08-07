from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.profiles.python import CommandNotAllowedError, PythonProfile
from patchpilot.sandbox.local import run_argv
from patchpilot.sandbox.workspace import PathPolicyError
from patchpilot.tools.base import ToolContext, ToolResult, failure, path_failure, success


class RunTestsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_command_id: str = Field(min_length=1, max_length=100)
    selector: str | None = Field(default=None, max_length=500)


class RunTestsTool:
    name = "run_tests"

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self.profile = PythonProfile(context.task_spec)

    def execute(
        self,
        arguments: RunTestsInput,
        *,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        try:
            command = self.profile.resolve(arguments.profile_command_id, arguments.selector)
            if arguments.selector is not None:
                selector_path = arguments.selector.split("::", 1)[0]
                if selector_path.endswith(".py") or "/" in selector_path or "\\" in selector_path:
                    self.context.path_policy.resolve(selector_path, must_exist=True)
        except CommandNotAllowedError as exc:
            return failure(self.name, "COMMAND_NOT_ALLOWED", str(exc), started=started)
        except (PathPolicyError, ValueError) as exc:
            if isinstance(exc, PathPolicyError):
                return path_failure(self.name, exc, started=started)
            return failure(self.name, "INVALID_SELECTOR", str(exc), started=started)

        effective_timeout = (
            command.timeout_seconds
            if timeout_seconds_override is None
            else min(command.timeout_seconds, max(0.001, timeout_seconds_override))
        )
        try:
            result = run_argv(
                command.argv,
                cwd=self.context.workspace.path,
                timeout_seconds=effective_timeout,
                output_max_chars=self.context.limits.output_max_chars,
                environment={"PYTEST_ADDOPTS": "--color=no"},
            )
        except OSError as exc:
            return failure(
                self.name,
                "COMMAND_EXECUTION_ERROR",
                f"failed to start the configured test command: {exc}",
                started=started,
            )
        data = {
            "profile_command_id": command.command_id,
            "argv": list(command.argv),
            "exit_code": result.return_code,
            "passed": result.return_code == 0 and not result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "isolation": "trusted-local",
        }
        if result.timed_out:
            return ToolResult(
                ok=False,
                tool=self.name,
                summary=f"tests timed out after {effective_timeout:g} seconds",
                data=data,
                error={
                    "code": "TIMEOUT",
                    "message": "test process tree was terminated after the configured timeout",
                },
                truncated=result.truncated,
                duration_ms=result.duration_ms,
            )
        return success(
            self.name,
            "tests passed" if result.return_code == 0 else "tests failed",
            data,
            started=started,
            truncated=result.truncated,
        )
