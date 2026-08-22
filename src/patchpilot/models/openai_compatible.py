from __future__ import annotations

import json
import time
from typing import Any

import httpx

from patchpilot.models.base import (
    Message,
    MessageRole,
    ModelClientError,
    ModelConfig,
    ModelProtocolError,
    ModelRateLimitError,
    ModelResponse,
    ModelServerError,
    ModelTimeoutError,
    TokenUsage,
    ToolCall,
    ToolSchema,
)


def _message_payload(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role == MessageRole.ASSISTANT and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_text()},
            }
            for call in message.tool_calls
        ]
    if message.role == MessageRole.TOOL:
        payload["tool_call_id"] = message.tool_call_id
        payload["name"] = message.name
    return payload


def _estimated_token_count(value: object) -> int:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, (len(serialized) + 3) // 4)


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible Chat Completions adapter."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        config: ModelConfig,
    ) -> ModelResponse:
        if config.api_key is None:
            raise ModelClientError("MODEL_API_KEY is required for the real model client")
        message_payloads = [_message_payload(message) for message in messages]
        tool_payloads = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]
        request_payload: dict[str, Any] = {
            "model": config.model,
            "messages": message_payloads,
            "tools": tool_payloads,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        if config.thinking_mode is not None:
            request_payload["thinking"] = {"type": config.thinking_mode}
        endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
        started = time.monotonic()
        try:
            response = await self._client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=config.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError("model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelClientError(f"model transport failed: {exc}") from exc

        if response.status_code == 429:
            raise ModelRateLimitError("model provider returned HTTP 429")
        if response.status_code >= 500:
            raise ModelServerError(f"model provider returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ModelClientError(f"model provider returned HTTP {response.status_code}")
        try:
            body = response.json()
            choice = body["choices"][0]
            response_message = choice["message"]
            raw_calls = response_message.get("tool_calls") or []
            calls = [
                ToolCall(
                    id=raw_call["id"],
                    name=raw_call["function"]["name"],
                    arguments=raw_call["function"].get("arguments", "{}"),
                )
                for raw_call in raw_calls
            ]
            raw_usage = body.get("usage")
            if raw_usage is None:
                prompt_tokens = _estimated_token_count(message_payloads)
                completion_tokens = _estimated_token_count(response_message)
                usage = TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    estimated=True,
                )
            else:
                prompt_tokens = int(raw_usage.get("prompt_tokens", 0))
                completion_tokens = int(raw_usage.get("completion_tokens", 0))
                usage = TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    estimated=False,
                )
            return ModelResponse(
                content=response_message.get("content"),
                tool_calls=calls,
                finish_reason=choice.get("finish_reason"),
                usage=usage,
                model=body.get("model") or config.model,
                provider_request_id=body.get("id"),
                latency_ms=round((time.monotonic() - started) * 1_000),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelProtocolError("invalid Chat Completions response payload") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
