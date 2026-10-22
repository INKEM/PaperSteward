from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from papersteward.config import LLMProviderKind, Settings
from papersteward.llm import FakeLLM, LLMConfigurationError, OpenAICompatibleProvider
from papersteward.llm.factory import build_llm_provider


def test_default_provider_is_offline_fake():
    settings = Settings(_env_file=None)

    provider = build_llm_provider(settings)

    assert isinstance(provider, FakeLLM)
    assert provider.model_id == "fake-llm"


def test_real_provider_requires_explicit_live_authorization():
    settings = Settings(
        _env_file=None,
        llm_provider=LLMProviderKind.OPENAI_COMPATIBLE,
        llm_model="test-model",
    )

    with pytest.raises(LLMConfigurationError, match="--live"):
        build_llm_provider(settings)


def test_real_provider_uses_configured_endpoint_and_transport():
    settings = Settings(
        _env_file=None,
        llm_provider=LLMProviderKind.OPENAI_COMPATIBLE,
        llm_base_url="https://llm.example/v1",
        llm_api_key=SecretStr("sk-secret-value"),
        llm_model="test-model",
        llm_timeout_s=12,
        llm_prompt_cost_per_1k_usd=0.5,
        llm_completion_cost_per_1k_usd=1.0,
    )
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = build_llm_provider(settings, live=True, transport=httpx.MockTransport(handle))
    assert isinstance(provider, OpenAICompatibleProvider)
    try:
        completion = provider.complete("ping", timeout_s=12)
    finally:
        provider.close()

    assert completion.text == "pong"
    assert str(requests[0].url) == "https://llm.example/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer sk-secret-value"


def test_fake_provider_can_use_configured_model_label():
    settings = Settings(
        _env_file=None,
        llm_provider=LLMProviderKind.FAKE,
        llm_model="fake-test-model",
    )

    assert build_llm_provider(settings).model_id == "fake-test-model"
