from __future__ import annotations

import sys
from dataclasses import dataclass

from patchpilot.domain.task import TaskSpec, validate_pytest_selector

PYTHON_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


class CommandNotAllowedError(ValueError):
    """Raised when a command is not in the application-defined Python profile."""


@dataclass(frozen=True, slots=True)
class ResolvedTestCommand:
    command_id: str
    argv: tuple[str, ...]
    timeout_seconds: int


class PythonProfile:
    """Resolve TaskSpec references without accepting arbitrary command text."""

    def __init__(self, spec: TaskSpec) -> None:
        if spec.repository.language != "python":
            raise ValueError("PythonProfile requires a Python TaskSpec")
        self._spec = spec

    @property
    def environment(self) -> dict[str, str]:
        return {"PYTEST_ADDOPTS": "--color=no"}

    @property
    def command_ids(self) -> tuple[str, ...]:
        return tuple(
            f"acceptance:{index}" for index, _ in enumerate(self._spec.acceptance.commands)
        )

    def resolve(self, command_id: str, selector: str | None = None) -> ResolvedTestCommand:
        prefix = "acceptance:"
        if not command_id.startswith(prefix):
            raise CommandNotAllowedError(f"unknown Python profile command: {command_id}")
        index_text = command_id.removeprefix(prefix)
        if not index_text.isascii() or not index_text.isdigit():
            raise CommandNotAllowedError(f"unknown Python profile command: {command_id}")
        index = int(index_text)
        if index >= len(self._spec.acceptance.commands):
            raise CommandNotAllowedError(f"unknown Python profile command: {command_id}")

        configured = self._spec.acceptance.commands[index]
        argv = [sys.executable, "-m", "pytest", *configured.argv[3:]]
        if selector is not None:
            normalized = validate_pytest_selector(selector)
            configured_selectors = configured.argv[3:]
            if configured_selectors and normalized not in configured_selectors:
                raise CommandNotAllowedError(
                    "selector must be one of the selectors configured by the TaskSpec"
                )
            if not configured_selectors:
                argv.append(normalized)
        return ResolvedTestCommand(
            command_id=command_id,
            argv=tuple(argv),
            timeout_seconds=configured.timeout_seconds,
        )
