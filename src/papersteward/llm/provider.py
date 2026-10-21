"""LLM provider 协议与 FakeLLM。

真实 provider（OpenAI-compatible 等）不属于 P1-S5；FakeLLM 用于契约测试、CLI
演示以及 P3/P4 的确定性回放。网关只依赖 ``LLMProvider``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from papersteward.llm.errors import LLMTimeoutError


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    """一次 provider 调用的原始结果与成本。"""

    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float


@runtime_checkable
class LLMProvider(Protocol):
    """provider 只需实现模型标识与一次文本补全。"""

    @property
    def model_id(self) -> str: ...

    def complete(self, prompt: str, *, timeout_s: float) -> LLMCompletion: ...


class FakeLLM:
    """按顺序返回固定响应；可模拟超时/ provider 错误与成本。

    - ``responses`` 用完后重复最后一个响应；
    - ``failures`` 按调用序号注入异常，缺失的位置视为成功；
    - ``cost_per_1k_tokens`` 用于测试成本累计。
    """

    def __init__(
        self,
        responses: Sequence[str] = ("pong",),
        *,
        model_id: str = "fake-llm",
        failures: Sequence[Exception | None] = (),
        latency_ms: float = 0.0,
        cost_per_1k_tokens: float = 0.0,
    ) -> None:
        if not responses:
            raise ValueError("FakeLLM 至少需要一个响应")
        if cost_per_1k_tokens < 0:
            raise ValueError("cost_per_1k_tokens 不能为负数")
        self._responses = tuple(responses)
        self._model_id = model_id
        self._failures = tuple(failures)
        self._latency_ms = latency_ms
        self._cost_per_1k_tokens = cost_per_1k_tokens
        self.calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(self, prompt: str, *, timeout_s: float) -> LLMCompletion:
        index = len(self.calls)
        self.calls.append(prompt)
        if index < len(self._failures) and self._failures[index] is not None:
            raise self._failures[index]  # type: ignore[misc]
        text = self._responses[min(index, len(self._responses) - 1)]
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(text) // 4)
        cost = (prompt_tokens + completion_tokens) / 1000.0 * self._cost_per_1k_tokens
        return LLMCompletion(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=self._latency_ms,
        )


def timeout_failure(message: str = "fake timeout") -> LLMTimeoutError:
    return LLMTimeoutError(message)


__all__ = ["FakeLLM", "LLMCompletion", "LLMProvider", "timeout_failure"]
