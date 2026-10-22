from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from papersteward.llm.errors import LLMProviderError, LLMTimeoutError
from papersteward.llm.openai_compatible import OpenAICompatibleProvider, validate_base_url


def _provider(handler, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key=SecretStr("sk-secret-value"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1.0,
        **kwargs,
    )


def test_success_posts_chat_completion_and_computes_usage_cost():
    requests: list[httpx.Request] = []
    ticks = iter([10.0, 10.25])

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key=SecretStr("sk-secret-value"),
        prompt_cost_per_1k_usd=0.5,
        completion_cost_per_1k_usd=1.0,
        transport=httpx.MockTransport(handle),
        clock=lambda: next(ticks),
    )
    try:
        completion = provider.complete("ping", timeout_s=5)
    finally:
        provider.close()

    assert completion.text == "pong"
    assert completion.prompt_tokens == 100
    assert completion.completion_tokens == 50
    assert completion.cost_usd == pytest.approx(0.1)
    assert completion.latency_ms == pytest.approx(250)
    request = requests[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer sk-secret-value"
    assert json.loads(request.content) == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "ping"}],
    }


def test_missing_key_and_usage_are_safe_defaults():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    provider = OpenAICompatibleProvider(
        base_url="https://llm.example/v1",
        model="test-model",
        transport=httpx.MockTransport(handle),
        clock=lambda: 1.0,
    )
    try:
        completion = provider.complete("ping", timeout_s=5)
    finally:
        provider.close()

    assert "authorization" not in seen[0].headers
    assert completion.prompt_tokens == completion.completion_tokens == 0
    assert completion.cost_usd == 0


@pytest.mark.parametrize(
    "failure,error_type,message",
    [
        (httpx.ReadTimeout("secret"), LLMTimeoutError, "超时"),
        (httpx.ConnectError("secret"), LLMProviderError, "网络"),
    ],
)
def test_transport_failures_are_sanitized(failure, error_type, message):
    def handle(_):
        raise failure

    provider = _provider(handle)
    try:
        with pytest.raises(error_type, match=message) as caught:
            provider.complete("ping", timeout_s=5)
    finally:
        provider.close()

    assert "secret" not in str(caught.value)
    assert "sk-secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "认证失败"),
        (403, "认证失败"),
        (429, "限流"),
        (500, "服务不可用"),
        (400, "请求失败"),
    ],
)
def test_http_status_errors_are_sanitized(status, message):
    provider = _provider(lambda _: httpx.Response(status, content=b'{"error":"secret body"}'))
    try:
        with pytest.raises(LLMProviderError, match=message) as caught:
            provider.complete("ping", timeout_s=5)
    finally:
        provider.close()

    assert "secret body" not in str(caught.value)
    assert caught.value.retryable is (status in {429, 500})


@pytest.mark.parametrize(
    "payload,message",
    [
        (b"not-json", "合法 JSON"),
        (b'{"choices": []}', "缺少补全内容"),
        (b'{"choices": [{"message": {"content": ""}}]}', "缺少补全内容"),
        (
            b'{"choices": [{"message": {"content": "pong"}}], "usage": {"prompt_tokens": -1}}',
            "usage 非法",
        ),
    ],
)
def test_malformed_success_payloads_are_rejected(payload, message):
    provider = _provider(lambda _: httpx.Response(200, content=payload))
    try:
        with pytest.raises(LLMProviderError, match=message):
            provider.complete("ping", timeout_s=5)
    finally:
        provider.close()


def test_oversized_response_is_rejected_without_parsing():
    provider = _provider(lambda _: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))
    try:
        with pytest.raises(LLMProviderError, match="响应过大"):
            provider.complete("ping", timeout_s=5)
    finally:
        provider.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "http://llm.example/v1"},
        {"base_url": "https://user:pass@llm.example/v1"},
        {"base_url": "https://llm.example/v1?token=secret"},
        {"model": "   "},
        {"timeout_s": 0},
        {"prompt_cost_per_1k_usd": -1},
    ],
)
def test_invalid_constructor_arguments(kwargs):
    values = {
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "transport": httpx.MockTransport(lambda _: httpx.Response(200)),
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(**values)


def test_base_url_rules():
    assert validate_base_url(" https://llm.example/v1/ ") == "https://llm.example/v1"
    assert validate_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    assert validate_base_url("http://[::1]:11434/v1") == "http://[::1]:11434/v1"
    with pytest.raises(ValueError, match="https"):
        validate_base_url("http://llm.example/v1")
