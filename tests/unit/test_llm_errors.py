"""LLM 错误文案契约（UXR-13）：用户可见消息不夹带诊断参数。"""

from __future__ import annotations

from types import SimpleNamespace

from papersteward.llm.errors import LLMProviderError


def test_provider_error_message_has_no_diagnostic_suffix() -> None:
    usage = SimpleNamespace(calls=3, retries=2)
    error = LLMProviderError("LLM 网络请求失败：无法连接到服务，请检查 API 地址与网络", usage=usage)
    assert str(error) == "LLM 网络请求失败：无法连接到服务，请检查 API 地址与网络"
    assert "calls=" not in str(error) and "retries=" not in str(error)
    # 诊断信息仍可通过属性获取（CLI/日志自行格式化）。
    assert error.usage is usage
