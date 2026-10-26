"""写入回执的规范化请求哈希；运行时与迁移回填共用，保证口径一致。

回执身份是幂等键（工作区内唯一）。命中回执后必须同时匹配操作、目标与
规范化请求哈希，任一不同即明确冲突；哈希只覆盖决定写入结果的内容字段，
``expected_revision`` 等前置条件不参与，修正并发前提后同键可重试。
哈希输入一律是普通标量（字符串/None），日期用 ISO 文本、时区统一 UTC，
迁移从存储行恢复时与运行时从契约实例计算得到同一哈希。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from papersteward.storage.delivery import content_hash

READING_TIME_RECORD = "reading_time.record"
ASSET_CREATE = "asset.create"
ASSET_REVISE = "asset.revise"
ASSET_STATUS = "asset.status"
_OPERATIONS = (READING_TIME_RECORD, ASSET_CREATE, ASSET_REVISE, ASSET_STATUS)


def request_hash(operation: str, target_id: str, payload: dict[str, Any]) -> str:
    """操作 + 目标 + 规范化载荷的联合哈希；键冲突判定与回填共用。"""
    return content_hash({"operation": operation, "target_id": target_id, **payload})


def reading_time_payload(
    *, paper_id: str, precision: str, day: str | None, month: str | None
) -> dict[str, Any]:
    """阅读时间声明的语义内容；precision/day/month 为存储文本形态。"""
    return {"paper_id": paper_id, "precision": precision, "day": day, "month": month}


def asset_content_payload(
    kind: str | None,
    *,
    title: str,
    body: str,
    applicability: str | None,
    sources: Iterable[tuple[str, str | None]],
) -> dict[str, Any]:
    """资产内容的语义载荷；来源按 (paper_id, rating_at) 排序去重。

    ``kind`` 只在创建请求中出现（修订的目标已绑定资产身份）；
    ``rating_at`` 是归一化到 UTC 的 ISO 文本。
    """
    normalized: list[dict[str, str | None]] = [
        {"paper_id": paper_id, "rating_at": rating_at}
        for paper_id, rating_at in sorted({(pid, rating or "") for pid, rating in sources})
    ]
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "applicability": applicability,
        "sources": normalized,
    }
    if kind is not None:
        payload = {"kind": kind, **payload}
    return payload


__all__ = [
    "ASSET_CREATE",
    "ASSET_REVISE",
    "ASSET_STATUS",
    "READING_TIME_RECORD",
    "asset_content_payload",
    "reading_time_payload",
    "request_hash",
]
