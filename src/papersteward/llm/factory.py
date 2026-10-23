"""从解析后的运行时配置构建 LLM provider。

数据库设置页保存的配置自带授权（``authorized=True``）；环境变量路径保留原有
CLI 语义：必须显式 ``live=True`` 才发起真实调用。
"""

from __future__ import annotations

import httpx

from papersteward.config import LLMProviderKind, Settings
from papersteward.llm.errors import LLMConfigurationError
from papersteward.llm.openai_compatible import OpenAICompatibleProvider
from papersteward.llm.provider import FakeLLM, LLMProvider
from papersteward.llm.runtime import LLMRuntimeConfig


def config_from_settings(settings: Settings) -> LLMRuntimeConfig:
    """环境变量路径的兼容入口；CLI 仍需显式 ``live=True``。"""
    return LLMRuntimeConfig.from_settings(settings)


def build_llm_provider(
    config: LLMRuntimeConfig | Settings,
    *,
    live: bool | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LLMProvider:
    """``live=None`` 表示信任配置自带的授权标志；显式传入时覆盖（CLI --live）。"""
    if isinstance(config, Settings):
        config = config_from_settings(config)
    if config.provider is LLMProviderKind.FAKE:
        return FakeLLM(model_id=config.model or "fake-llm")
    authorized = config.authorized if live is None else live
    if not authorized:
        raise LLMConfigurationError("真实 LLM 调用需要授权：请在设置页保存模型配置，或使用 --live")
    if not (config.model and config.model.strip()):
        raise LLMConfigurationError("请配置模型名称（设置页或 PAPERSTEWARD_LLM_MODEL）")
    return OpenAICompatibleProvider(
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        timeout_s=config.timeout_s,
        prompt_cost_per_1k_usd=config.prompt_cost_per_1k_usd,
        completion_cost_per_1k_usd=config.completion_cost_per_1k_usd,
        transport=transport,
    )


__all__ = ["build_llm_provider", "config_from_settings"]
