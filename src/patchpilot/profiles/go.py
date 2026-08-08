from __future__ import annotations

import re

from patchpilot.domain.task import TaskSpec, validate_go_package
from patchpilot.profiles.python import CommandNotAllowedError, ResolvedTestCommand

GO_IGNORED_DIRECTORIES = frozenset({".git", "bin", "coverage", "dist", "vendor"})
_COMMAND_ID = re.compile(r"^acceptance:([0-9]+)$")


class GoProfile:
    """Resolve only application-validated Go test and vet commands."""

    def __init__(self, spec: TaskSpec) -> None:
        if spec.repository.language != "go":
            raise ValueError("GoProfile requires a Go TaskSpec")
        self._spec = spec

    @property
    def command_ids(self) -> tuple[str, ...]:
        return tuple(
            f"acceptance:{index}" for index, _ in enumerate(self._spec.acceptance.commands)
        )

    @property
    def environment(self) -> dict[str, str]:
        return {
            "GONOSUMDB": "*",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
        }

    def resolve(self, command_id: str, selector: str | None = None) -> ResolvedTestCommand:
        match = _COMMAND_ID.fullmatch(command_id)
        if match is None:
            raise CommandNotAllowedError(f"unknown Go profile command: {command_id}")
        index = int(match.group(1))
        if index >= len(self._spec.acceptance.commands):
            raise CommandNotAllowedError(f"unknown Go profile command: {command_id}")
        configured = self._spec.acceptance.commands[index]
        argv = list(configured.argv)
        if argv[:2] == ["go", "test"]:
            argv.insert(2, "-v")
        if selector is not None:
            if configured.argv[:2] != ["go", "test"]:
                raise CommandNotAllowedError("selectors are supported only for go test")
            normalized = validate_go_package(selector)
            configured_packages = argv[2:]
            if "./..." not in configured_packages and normalized not in configured_packages:
                raise CommandNotAllowedError(
                    "selector must be within the packages configured by the TaskSpec"
                )
            argv = ["go", "test", "-v", normalized]
        return ResolvedTestCommand(
            command_id=command_id,
            argv=tuple(argv),
            timeout_seconds=configured.timeout_seconds,
        )
