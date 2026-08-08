from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from patchpilot.sandbox.local import CommandResult, run_argv


class TrustedLocalSandbox:
    """Execute only project-owned trusted fixtures directly on the host."""

    @property
    def isolation(self) -> str:
        return "trusted-local"

    @property
    def image(self) -> None:
        return None

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output_max_chars: int,
        environment: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return run_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            output_max_chars=output_max_chars,
            environment=environment,
            cancel_event=cancel_event,
        )
