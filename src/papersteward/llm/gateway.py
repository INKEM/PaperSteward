"""LLM 网关：provider 重试、schema 校验、一次修复重试、溯源与成本。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from papersteward.core.entities import Provenance, utc_now
from papersteward.llm.errors import (
    LLMProviderError,
    LLMValidationError,
    PromptRegistryError,
)
from papersteward.llm.prompts import PromptRegistry, default_registry
from papersteward.llm.provider import LLMCompletion, LLMProvider
from papersteward.schemas import SchemaError, validate_json_text


@dataclass(slots=True)
class LLMUsage:
    """一次网关调用的累计成本；不含 prompt 内容或密钥。"""

    model_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    calls: int = 0
    retries: int = 0
    repairs: int = 0

    def add_completion(self, completion: LLMCompletion) -> None:
        self.prompt_tokens += completion.prompt_tokens
        self.completion_tokens += completion.completion_tokens
        self.cost_usd += completion.cost_usd
        self.latency_ms += completion.latency_ms

    def as_json(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "latency_ms": round(self.latency_ms, 3),
            "calls": self.calls,
            "retries": self.retries,
            "repairs": self.repairs,
        }


@dataclass(frozen=True, slots=True)
class LLMResult:
    data: BaseModel
    raw_text: str
    usage: LLMUsage
    provenance: Provenance
    prompt_name: str
    prompt_version: str
    repair_used: bool
    attempts: int

    def as_json(self) -> dict[str, Any]:
        return {
            "data": self.data.model_dump(mode="json"),
            "raw_text": self.raw_text,
            "usage": self.usage.as_json(),
            "provenance": {
                "model": self.provenance.model,
                "prompt_version": self.provenance.prompt_version,
                "generated_at": self.provenance.generated_at.isoformat(),
                "input_hash": self.provenance.input_hash,
            },
            "prompt_name": self.prompt_name,
            "prompt_version": self.prompt_version,
            "repair_used": self.repair_used,
            "attempts": self.attempts,
        }


@dataclass(frozen=True, slots=True)
class LLMPingResult:
    text: str
    usage: LLMUsage
    model_id: str


class LLMGateway:
    """确定性的 LLM 调用入口；真实 provider 由外部注入。"""

    def __init__(
        self,
        provider: LLMProvider,
        registry: PromptRegistry | None = None,
        *,
        max_attempts: int = 3,
        repair_attempts: int = 1,
        timeout_s: float = 30.0,
        backoff_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须 ≥ 1")
        if repair_attempts not in (0, 1):
            raise ValueError("repair_attempts 只能为 0 或 1")
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须大于 0")
        self._provider = provider
        self._registry = registry or default_registry()
        self._max_attempts = max_attempts
        self._repair_attempts = repair_attempts
        self._timeout_s = timeout_s
        self._backoff_s = max(0.0, backoff_s)
        self._sleep = sleep
        self._clock = clock

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def _call_with_retries(self, prompt: str, usage: LLMUsage) -> tuple[str, int]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                completion = self._provider.complete(prompt, timeout_s=self._timeout_s)
            except LLMProviderError as exc:
                usage.calls += 1
                if exc.retryable and attempt < self._max_attempts:
                    usage.retries += 1
                    self._sleep(self._backoff_s * (2 ** (attempt - 1)))
                    continue
                exc.usage = usage
                raise
            usage.calls += 1
            usage.add_completion(completion)
            return completion.text, attempt
        fallback = LLMProviderError("provider 调用失败")
        fallback.usage = usage
        raise fallback

    def _input_hash(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def complete_json(
        self,
        prompt_name: str,
        variables: Mapping[str, Any],
        *,
        prompt_version: str | None = None,
    ) -> LLMResult:
        prompt = self._registry.get(prompt_name, prompt_version)
        if prompt.schema is None:
            raise PromptRegistryError(f"prompt {prompt.name} 未配置 JSON schema")
        rendered = prompt.render(variables)
        input_hash = self._input_hash(rendered)
        usage = LLMUsage(model_id=self._provider.model_id)

        raw_text, attempts = self._call_with_retries(rendered, usage)
        try:
            data = validate_json_text(prompt.schema, raw_text, source=f"llm:{prompt.name}")
            final_prompt_version = prompt.version
            repair_used = False
        except SchemaError as first_error:
            if self._repair_attempts == 0:
                raise LLMValidationError(
                    first_error.message,
                    field=first_error.field,
                    raw_text=raw_text,
                    usage=usage,
                    attempts=attempts,
                    repairs=0,
                ) from first_error

            try:
                repair_prompt = self._registry.get("repair")
            except PromptRegistryError as exc:
                raise LLMValidationError(
                    f"{first_error.field}: {first_error.message}",
                    field=first_error.field,
                    raw_text=raw_text,
                    usage=usage,
                    attempts=attempts,
                    repairs=0,
                ) from exc
            usage.repairs += 1
            repair_text = repair_prompt.render(
                {
                    "schema": prompt.schema,
                    "error": f"{first_error.field}: {first_error.message}",
                    "raw": raw_text,
                }
            )
            repaired_text, repair_attempts = self._call_with_retries(repair_text, usage)
            attempts += repair_attempts
            try:
                data = validate_json_text(
                    prompt.schema, repaired_text, source=f"llm:{prompt.name}:repair"
                )
            except SchemaError as repair_error:
                raise LLMValidationError(
                    repair_error.message,
                    field=repair_error.field,
                    raw_text=raw_text,
                    repair_text=repaired_text,
                    usage=usage,
                    attempts=attempts,
                    repairs=1,
                ) from repair_error
            raw_text = repaired_text
            final_prompt_version = f"{prompt.version}+repair:{repair_prompt.version}"
            repair_used = True

        provenance = Provenance(
            model=self._provider.model_id,
            prompt_version=final_prompt_version,
            generated_at=self._clock(),
            input_hash=input_hash,
        )
        return LLMResult(
            data=data,
            raw_text=raw_text,
            usage=usage,
            provenance=provenance,
            prompt_name=prompt.name,
            prompt_version=final_prompt_version,
            repair_used=repair_used,
            attempts=attempts,
        )

    def ping(self) -> LLMPingResult:
        usage = LLMUsage(model_id=self._provider.model_id)
        text, _attempts = self._call_with_retries("ping", usage)
        return LLMPingResult(text=text, usage=usage, model_id=self._provider.model_id)


__all__ = ["LLMGateway", "LLMPingResult", "LLMResult", "LLMUsage"]
