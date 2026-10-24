"""LLM 相关 CLI：测试用 FakeLLM 或显式授权的真实 provider 连通性检查。"""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from papersteward.config import LLMProviderKind, Settings, format_settings_error
from papersteward.llm import (
    FakeLLM,
    LLMError,
    LLMGateway,
    LLMProvider,
    build_llm_provider,
)


def run_llm_ping(*, fake: bool, live: bool, json_output: bool) -> int:
    if fake == live:
        print(
            "错误：请选择 `--fake`（仅开发/测试）或 `--live`（真实调用）之一。",
            file=sys.stderr,
        )
        return 1

    provider: LLMProvider | None = None
    try:
        if fake:
            provider = FakeLLM(responses=("pong",), model_id="fake-llm")
            provider_name, max_attempts, timeout_s = "fake", 1, 30.0
        else:
            settings = Settings()
            if settings.llm_provider is LLMProviderKind.FAKE:
                print(
                    "错误：当前 LLM provider=fake；请先设置 "
                    "PAPERSTEWARD_LLM_PROVIDER=openai_compatible 并配置模型。",
                    file=sys.stderr,
                )
                return 1
            provider = build_llm_provider(settings, live=True)
            provider_name = settings.llm_provider.value
            max_attempts = settings.llm_max_attempts
            timeout_s = settings.llm_timeout_s
        gateway = LLMGateway(provider, max_attempts=max_attempts, timeout_s=timeout_s)
        result = gateway.ping()
    except ValidationError as exc:
        print(format_settings_error(exc), file=sys.stderr)
        return 1
    except (LLMError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if provider is not None:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    payload = {
        "ok": True,
        "provider": provider_name,
        "model_id": result.model_id,
        "text": result.text,
        "usage": result.usage.as_json(),
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"OK llm {result.model_id}: {result.text} "
            f"(calls={result.usage.calls}, cost={result.usage.cost_usd:g})"
        )
    return 0


__all__ = ["run_llm_ping"]
