from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from patchpilot.models.base import (
    Message,
    ModelClientError,
    ModelConfig,
    ModelResponse,
    ToolSchema,
)

ScriptResponder = Callable[[list[Message], list[ToolSchema], ModelConfig], ModelResponse]
ScriptItem = ModelResponse | ModelClientError | ScriptResponder


@dataclass(frozen=True, slots=True)
class RecordedModelCall:
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    config: ModelConfig


class ScriptedModelClient:
    """Return a finite deterministic script without performing network I/O."""

    def __init__(self, script: list[ScriptItem]) -> None:
        if not script:
            raise ValueError("script must contain at least one item")
        self._script = list(script)
        self.calls: list[RecordedModelCall] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse:
        self.calls.append(
            RecordedModelCall(messages=tuple(messages), tools=tuple(tools), config=config)
        )
        if not self._script:
            raise AssertionError("ScriptedModelClient received more calls than scripted")
        item = self._script.pop(0)
        if isinstance(item, ModelClientError):
            raise item
        if callable(item):
            return item(list(messages), list(tools), config)
        return item

    def assert_exhausted(self) -> None:
        if self._script:
            raise AssertionError(f"{len(self._script)} scripted model responses were not consumed")


class FakeModelClient:
    """Repeat a deterministic response and record every request."""

    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.calls: list[RecordedModelCall] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse:
        self.calls.append(
            RecordedModelCall(messages=tuple(messages), tools=tuple(tools), config=config)
        )
        return self.response
