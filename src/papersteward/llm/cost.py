"""将 LLM 用量累计到 ``run.cost_json``。

网关本身不碰数据库；调用方在事务中调用 ``record_llm_usage``，保证成本可审计且
不记录 prompt 内容或密钥。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from papersteward.core.entities import Run
from papersteward.llm.gateway import LLMUsage
from papersteward.storage.repositories import RunRepository


def merge_usage(existing: Mapping[str, Any], usage: LLMUsage) -> dict[str, Any]:
    """保留已有 cost_json，追加一次调用并重算汇总字段。"""
    calls = existing.get("llm_calls")
    calls_list: list[Any] = list(calls) if isinstance(calls, list) else []
    calls_list.append(usage.as_json())
    return {
        **dict(existing),
        "llm_calls": calls_list,
        "llm_call_count": len(calls_list),
        "llm_prompt_tokens": sum(int(item.get("prompt_tokens", 0)) for item in calls_list),
        "llm_completion_tokens": sum(int(item.get("completion_tokens", 0)) for item in calls_list),
        "llm_cost_usd": round(sum(float(item.get("cost_usd", 0.0)) for item in calls_list), 8),
        "llm_latency_ms": round(sum(float(item.get("latency_ms", 0.0)) for item in calls_list), 3),
    }


def record_llm_usage(session: Session, run_id: str, usage: LLMUsage) -> Run | None:
    """把一次网关用量写入运行记录；运行不存在时返回 None，不 commit。"""
    repository = RunRepository(session)
    run = repository.get(run_id)
    if run is None:
        return None
    return repository.update_cost(run_id, merge_usage(run.cost_json, usage))


__all__ = ["merge_usage", "record_llm_usage"]
