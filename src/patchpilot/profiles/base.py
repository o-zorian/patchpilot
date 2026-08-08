from __future__ import annotations

from typing import Protocol

from patchpilot.domain.task import TaskSpec
from patchpilot.profiles.python import ResolvedTestCommand


class LanguageProfile(Protocol):
    @property
    def command_ids(self) -> tuple[str, ...]: ...

    @property
    def environment(self) -> dict[str, str]: ...

    def resolve(self, command_id: str, selector: str | None = None) -> ResolvedTestCommand: ...


def profile_for(spec: TaskSpec) -> LanguageProfile:
    if spec.repository.language == "python":
        from patchpilot.profiles.python import PythonProfile

        return PythonProfile(spec)
    if spec.repository.language == "go":
        from patchpilot.profiles.go import GoProfile

        return GoProfile(spec)
    raise ValueError(f"unsupported language profile: {spec.repository.language}")
