from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] | str

    def arguments_text(self) -> str:
        if isinstance(self.arguments, str):
            return self.arguments
        return json.dumps(self.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: MessageRole
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self) -> Message:
        if self.role == MessageRole.TOOL and (self.tool_call_id is None or self.name is None):
            raise ValueError("tool messages require tool_call_id and name")
        if self.role != MessageRole.ASSISTANT and self.tool_calls:
            raise ValueError("only assistant messages may contain tool_calls")
        return self


class ToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1_000)
    parameters: dict[str, Any]


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated: bool = False

    @model_validator(mode="after")
    def validate_total(self) -> TokenUsage:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        return self


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(default="https://api.openai.com/v1", min_length=1)
    api_key: SecretStr | None = None
    model: str = Field(min_length=1, max_length=255)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=4_096, gt=0)
    request_timeout_seconds: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0, le=3)
    thinking_mode: Literal["enabled", "disabled"] | None = None
    retry_base_seconds: float = Field(default=0.25, ge=0)
    retry_max_seconds: float = Field(default=4, ge=0)
    input_cost_per_million_usd: Decimal = Field(default=Decimal("0"), ge=0)
    output_cost_per_million_usd: Decimal = Field(default=Decimal("0"), ge=0)

    def cost_for(self, usage: TokenUsage) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(usage.prompt_tokens) * self.input_cost_per_million_usd
            + Decimal(usage.completion_tokens) * self.output_cost_per_million_usd
        ) / million


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: TokenUsage
    model: str = Field(min_length=1, max_length=255)
    provider_request_id: str | None = Field(default=None, max_length=255)
    latency_ms: int = Field(ge=0)


class ModelClientError(RuntimeError):
    code = "MODEL_ERROR"
    retryable = False


class ModelRateLimitError(ModelClientError):
    code = "MODEL_RATE_LIMIT"
    retryable = True


class ModelServerError(ModelClientError):
    code = "MODEL_SERVER_ERROR"
    retryable = True


class ModelTimeoutError(ModelClientError):
    code = "MODEL_TIMEOUT"
    retryable = True


class ModelProtocolError(ModelClientError):
    code = "MODEL_PROTOCOL_ERROR"


class ModelClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse: ...
