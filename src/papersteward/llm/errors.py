"""LLM 网关错误类型。"""

from __future__ import annotations

from typing import Any


class LLMError(Exception):
    """LLM 网关错误基类；CLI 只打印可操作信息，不输出堆栈。"""


class LLMProviderError(LLMError):
    """provider 失败；可由网关重试。最终失败时附 ``usage`` 供成本审计。"""

    def __init__(self, message: str = "provider 调用失败", *, usage: Any = None) -> None:
        super().__init__(message)
        self.usage = usage

    def __str__(self) -> str:
        if self.usage is None:
            return str(self.args[0]) if self.args else "provider 调用失败"
        return f"{self.args[0]} (calls={self.usage.calls}, retries={self.usage.retries})"


class LLMTimeoutError(LLMProviderError):
    """provider 超时；可由网关重试。"""


class LLMValidationError(LLMError):
    """修复重试后仍无法得到符合 schema 的 JSON。"""

    def __init__(
        self,
        message: str,
        *,
        field: str,
        raw_text: str | None = None,
        repair_text: str | None = None,
        usage: Any = None,
        attempts: int = 0,
        repairs: int = 0,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.raw_text = raw_text
        self.repair_text = repair_text
        self.usage = usage
        self.attempts = attempts
        self.repairs = repairs

    def __str__(self) -> str:
        return f"{self.field}: {self.args[0]}"


class PromptRegistryError(LLMError):
    """prompt 未注册、版本冲突或 schema 配置错误。"""


class PromptRenderError(LLMError):
    """prompt 变量缺失或渲染失败。"""


__all__ = [
    "LLMError",
    "LLMProviderError",
    "LLMTimeoutError",
    "LLMValidationError",
    "PromptRegistryError",
    "PromptRenderError",
]
