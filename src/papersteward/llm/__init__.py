"""LLM 网关、provider、prompt registry、修复重试与成本记录（P1-S5/P4-S1）。"""

from __future__ import annotations

from papersteward.llm.cost import merge_usage, record_llm_usage
from papersteward.llm.errors import (
    LLMConfigurationError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    LLMValidationError,
    PromptRegistryError,
    PromptRenderError,
)
from papersteward.llm.factory import build_llm_provider
from papersteward.llm.gateway import LLMGateway, LLMPingResult, LLMResult, LLMUsage
from papersteward.llm.openai_compatible import OpenAICompatibleProvider
from papersteward.llm.prompts import (
    CLASSIFICATION_V1,
    CLASSIFICATION_V2,
    RELEVANCE_V1,
    REPAIR_V1,
    SPECIAL_NOMINATION_V1,
    SUMMARY_V1,
    SUMMARY_V2,
    PromptRegistry,
    PromptTemplate,
    default_registry,
)
from papersteward.llm.provider import FakeLLM, LLMCompletion, LLMProvider, timeout_failure

__all__ = [
    "CLASSIFICATION_V1",
    "CLASSIFICATION_V2",
    "REPAIR_V1",
    "RELEVANCE_V1",
    "SPECIAL_NOMINATION_V1",
    "SUMMARY_V1",
    "SUMMARY_V2",
    "FakeLLM",
    "LLMCompletion",
    "LLMConfigurationError",
    "LLMError",
    "LLMGateway",
    "LLMPingResult",
    "LLMProvider",
    "LLMProviderError",
    "LLMResult",
    "LLMTimeoutError",
    "LLMUsage",
    "LLMValidationError",
    "OpenAICompatibleProvider",
    "PromptRegistry",
    "PromptRegistryError",
    "PromptRenderError",
    "PromptTemplate",
    "build_llm_provider",
    "default_registry",
    "merge_usage",
    "record_llm_usage",
    "timeout_failure",
]
