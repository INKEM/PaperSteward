"""OpenAI-compatible chat completions provider；不依赖厂商 SDK。

只做一次 HTTP 补全：重试、schema 校验与修复由 ``LLMGateway`` 负责。请求/响应中的
密钥、prompt 与原始错误体不进入日志或异常文本。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from papersteward import __version__
from papersteward.llm.errors import LLMProviderError, LLMTimeoutError
from papersteward.llm.provider import LLMCompletion

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_base_url(value: str) -> str:
    """返回去掉尾部斜杠的 API 根地址；非本机必须 HTTPS。"""
    url = httpx.URL(value.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError("llm_base_url 必须是 http(s) API 根地址，不能包含认证、查询或片段")
    if url.scheme == "http" and url.host not in _LOOPBACK_HOSTS:
        raise ValueError("非本机 LLM 地址必须使用 https")
    return str(url).rstrip("/")


class OpenAICompatibleProvider:
    """OpenAI-compatible ``/chat/completions`` provider。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | None = None,
        timeout_s: float = 30.0,
        prompt_cost_per_1k_usd: float = 0.0,
        completion_cost_per_1k_usd: float = 0.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model.strip():
            raise ValueError("llm_model 不能为空")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("llm_timeout_s 必须为有限正数")
        for cost in (prompt_cost_per_1k_usd, completion_cost_per_1k_usd):
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("LLM 成本单价必须为有限非负数")
        self._model = model.strip()
        self._endpoint = validate_base_url(base_url) + "/chat/completions"
        self._prompt_cost = prompt_cost_per_1k_usd
        self._completion_cost = completion_cost_per_1k_usd
        self._clock = clock
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"PaperSteward/{__version__}",
        }
        if api_key is not None and api_key.get_secret_value().strip():
            headers["Authorization"] = "Bearer " + api_key.get_secret_value().strip()
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            trust_env=False,
            follow_redirects=False,
            headers=headers,
        )

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        """便于诊断/测试；不包含查询参数或认证信息。"""
        return self._endpoint

    def close(self) -> None:
        self._client.close()

    def complete(self, prompt: str, *, timeout_s: float) -> LLMCompletion:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 不能为空")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s 必须为有限正数")
        started = self._clock()
        try:
            response = self._client.post(
                self._endpoint,
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM 请求超时") from exc
        except httpx.RequestError as exc:
            raise LLMProviderError("LLM 网络请求失败") from exc
        self._raise_for_status(response.status_code)
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise LLMProviderError("LLM 响应过大")
        try:
            payload = response.json()
        except (ValueError, UnicodeError) as exc:
            raise LLMProviderError("LLM 响应不是合法 JSON") from exc
        text, prompt_tokens, completion_tokens = self._parse_payload(payload)
        cost = (
            prompt_tokens / 1000.0 * self._prompt_cost
            + completion_tokens / 1000.0 * self._completion_cost
        )
        return LLMCompletion(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=max(0.0, (self._clock() - started) * 1000.0),
        )

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code == 200:
            return
        if status_code == 429:
            raise LLMProviderError("LLM 请求被限流")
        if status_code in {401, 403}:
            raise LLMProviderError("LLM 认证失败", retryable=False)
        if status_code >= 500:
            raise LLMProviderError("LLM 服务不可用")
        raise LLMProviderError("LLM 请求失败", retryable=False)

    @staticmethod
    def _parse_payload(payload: Any) -> tuple[str, int, int]:
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("LLM 响应缺少补全内容") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError("LLM 响应缺少补全内容")
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise LLMProviderError("LLM 响应 usage 非法")
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        for value in (prompt_tokens, completion_tokens):
            if type(value) is not int or value < 0:
                raise LLMProviderError("LLM 响应 usage 非法")
        return content, prompt_tokens, completion_tokens

    def __enter__(self) -> OpenAICompatibleProvider:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = ["OpenAICompatibleProvider", "validate_base_url"]
