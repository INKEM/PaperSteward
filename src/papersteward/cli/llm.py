"""LLM 相关 CLI：P1 只提供 FakeLLM 连通性检查。"""

from __future__ import annotations

import json
import sys

from papersteward.llm import FakeLLM, LLMGateway


def run_llm_ping(*, fake: bool, json_output: bool) -> int:
    if not fake:
        print(
            "错误：P1 仅实现 FakeLLM；真实 provider 尚未配置。请使用 `llm ping --fake`。",
            file=sys.stderr,
        )
        return 1

    provider = FakeLLM(responses=("pong",), model_id="fake-llm")
    gateway = LLMGateway(provider)
    result = gateway.ping()

    payload = {
        "ok": True,
        "provider": "fake",
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
