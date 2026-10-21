"""LLM 网关、provider、prompt registry、修复重试与成本记录（P1-S5）。"""

from __future__ import annotations

from papersteward.llm.cost import merge_usage, record_llm_usage
from papersteward.llm.errors import (
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    LLMValidationError,
    PromptRegistryError,
    PromptRenderError,
)
from papersteward.llm.gateway import LLMGateway, LLMPingResult, LLMResult, LLMUsage
from papersteward.llm.prompts import (
    CLASSIFICATION_V1,
    REPAIR_V1,
    SUMMARY_V1,
    PromptRegistry,
    PromptTemplate,
    default_registry,
)
from papersteward.llm.provider import FakeLLM, LLMCompletion, LLMProvider, timeout_failure

__all__ = [
    "CLASSIFICATION_V1",
    "REPAIR_V1",
    "SUMMARY_V1",
    "FakeLLM",
    "LLMCompletion",
    "LLMError",
    "LLMGateway",
    "LLMPingResult",
    "LLMProvider",
    "LLMProviderError",
    "LLMResult",
    "LLMTimeoutError",
    "LLMUsage",
    "LLMValidationError",
    "PromptRegistry",
    "PromptRegistryError",
    "PromptRenderError",
    "PromptTemplate",
    "default_registry",
    "merge_usage",
    "record_llm_usage",
    "timeout_failure",
]
