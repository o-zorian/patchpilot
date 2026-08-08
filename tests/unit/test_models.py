from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from patchpilot.config import AppSettings, SettingsError
from patchpilot.models.base import (
    Message,
    MessageRole,
    ModelConfig,
    ModelRateLimitError,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from patchpilot.models.fake import ScriptedModelClient
from patchpilot.models.openai_compatible import OpenAICompatibleClient


def model_config() -> ModelConfig:
    return ModelConfig(
        base_url="https://model.example.invalid/v1",
        api_key=SecretStr("test-key"),
        model="offline-test",
        input_cost_per_million_usd=Decimal("2"),
        output_cost_per_million_usd=Decimal("8"),
    )


@pytest.mark.asyncio
async def test_openai_compatible_client_maps_structured_calls_without_network() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "user"
        assert body["tools"][0]["function"]["name"] == "read_file"
        return httpx.Response(
            200,
            json={
                "id": "provider-request",
                "model": "offline-test",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"calculator.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenAICompatibleClient(http_client)
        response = await client.complete(
            [Message(role=MessageRole.USER, content="inspect")],
            [
                ToolSchema(
                    name="read_file",
                    description="Read one file.",
                    parameters={"type": "object"},
                )
            ],
            model_config(),
        )

    assert response.provider_request_id == "provider-request"
    assert response.tool_calls == [
        ToolCall(
            id="call-1",
            name="read_file",
            arguments='{"path":"calculator.py"}',
        )
    ]
    assert response.usage.estimated is True
    assert response.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_openai_compatible_client_classifies_rate_limits() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(429))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenAICompatibleClient(http_client)
        with pytest.raises(ModelRateLimitError):
            await client.complete([], [], model_config())


@pytest.mark.asyncio
async def test_scripted_model_is_finite_and_records_complete_history() -> None:
    response = ModelResponse(
        content="done",
        usage=TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        model="offline-test",
        latency_ms=0,
    )
    client = ScriptedModelClient([response])
    messages = [Message(role=MessageRole.USER, content="task")]

    actual = await client.complete(messages, [], model_config())
    client.assert_exhausted()

    assert actual == response
    assert client.calls[0].messages == tuple(messages)
    with pytest.raises(AssertionError, match="more calls"):
        await client.complete(messages, [], model_config())


def test_model_cost_uses_separate_input_and_output_rates() -> None:
    usage = TokenUsage(
        prompt_tokens=500_000,
        completion_tokens=250_000,
        total_tokens=750_000,
    )

    assert model_config().cost_for(usage) == Decimal("3")


def test_real_model_settings_are_explicit_and_keep_the_key_secret() -> None:
    with pytest.raises(SettingsError, match="PATCHPILOT_ENABLE_REAL_MODEL"):
        AppSettings(_env_file=None).real_model_config()

    with pytest.raises(SettingsError, match="MODEL_NAME"):
        AppSettings(_env_file=None, patchpilot_enable_real_model=True).real_model_config()

    settings = AppSettings(
        _env_file=None,
        patchpilot_enable_real_model=True,
        model_name="configured-model",
        model_api_key="configured-secret",
    )
    configured = settings.real_model_config()

    assert configured.model == "configured-model"
    assert configured.api_key is not None
    assert configured.api_key.get_secret_value() == "configured-secret"
    assert "configured-secret" not in repr(configured)
