from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from patchpilot.sandbox.local import CommandResult


class SandboxError(OSError):
    """Raised when an execution sandbox cannot safely start or clean up."""


class CommandSandbox(Protocol):
    @property
    def isolation(self) -> str: ...

    @property
    def image(self) -> str | None: ...

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output_max_chars: int,
        environment: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult: ...
