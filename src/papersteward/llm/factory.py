"""从 Settings 构建 LLM provider；真实 provider 需要显式 ``live`` 授权。"""

from __future__ import annotations

import httpx

from papersteward.config import LLMProviderKind, Settings
from papersteward.llm.errors import LLMConfigurationError
from papersteward.llm.openai_compatible import OpenAICompatibleProvider
from papersteward.llm.provider import FakeLLM, LLMProvider


def build_llm_provider(
    settings: Settings,
    *,
    live: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> LLMProvider:
    """默认返回 FakeLLM；openai_compatible 只有 ``live=True`` 才会构建。"""
    if settings.llm_provider is LLMProviderKind.FAKE:
        return FakeLLM(model_id=settings.llm_model or "fake-llm")
    if not live:
        raise LLMConfigurationError("真实 LLM 调用需要显式 --live 授权")
    if not settings.llm_model:
        raise LLMConfigurationError("请配置 PAPERSTEWARD_LLM_MODEL")
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout_s=settings.llm_timeout_s,
        prompt_cost_per_1k_usd=settings.llm_prompt_cost_per_1k_usd,
        completion_cost_per_1k_usd=settings.llm_completion_cost_per_1k_usd,
        transport=transport,
    )


__all__ = ["build_llm_provider"]
