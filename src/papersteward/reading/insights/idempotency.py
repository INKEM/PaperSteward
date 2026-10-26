"""写入回执：规范化请求哈希、命中判定与回执编排；运行时与迁移回填共用。

回执身份是幂等键（工作区内唯一）。命中后请求哈希必须完全一致——哈希已把
操作与目标绑进内容，任一不同即明确冲突；``request_hash`` 为空（旧数据无法
证明）也按冲突处理，不静默承认新请求。哈希只覆盖决定写入结果的内容字段，
``expected_revision`` 等前置条件不参与，修正并发前提后同键可重试。哈希输入
一律是普通标量（字符串/None），日期用 ISO 文本、时区统一 UTC，迁移从存储
行恢复时与运行时从契约实例计算得到同一哈希。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from papersteward.core.errors import InvariantViolation
from papersteward.storage.delivery import content_hash
from papersteward.storage.insights import ReadingWriteReceiptRepository
from papersteward.storage.models import ReadingWriteReceiptRow

__all__ = [
    "ASSET_CREATE",
    "ASSET_REVISE",
    "ASSET_STATUS",
    "READING_TIME_RECORD",
    "PendingReceipt",
    "ReplayResult",
    "asset_content_payload",
    "complete_keyed_write",
    "discard_keyed_write",
    "open_keyed_write",
    "reading_time_payload",
    "request_hash",
]

READING_TIME_RECORD = "reading_time.record"
ASSET_CREATE = "asset.create"
ASSET_REVISE = "asset.revise"
ASSET_STATUS = "asset.status"


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
        {"paper_id": paper_id, "rating_at": rating_at or None}
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


@dataclass(frozen=True)
class ReplayResult:
    """同键同请求命中；``result`` 为空表示旧数据回填，调用方退回当前视图。"""

    result: dict[str, Any] | None
    target_id: str


@dataclass(frozen=True)
class PendingReceipt:
    """miss 后已插入的回执行；与业务写入同一事务提交，失败回滚即不消耗键。"""

    session: Session
    row: ReadingWriteReceiptRow


def discard_keyed_write(receipt: PendingReceipt) -> None:
    """版本冲突等可重试失败时移除回执行：不依赖调用方回滚，键不被消耗。"""
    receipt.session.delete(receipt.row)
    receipt.session.flush()


def open_keyed_write(
    session: Session,
    *,
    operation: str,
    target_id: str,
    key: str,
    payload: dict[str, Any],
    now: datetime,
) -> ReplayResult | PendingReceipt:
    """查回执并判定：命中同哈希重放，不同请求明确冲突；miss 先落回执行。

    必须在写事务内调用（``storage.tx.write_tx`` / ``BEGIN IMMEDIATE``），
    保证"查回执→插回执→版本检查→业务写入"在同一把写锁内完成；唯一索引
    兜底真并发，IntegrityError 转换为可恢复的 409 语义。
    """
    digest = request_hash(operation, target_id, payload)
    existing = ReadingWriteReceiptRepository(session).find(key)
    if existing is not None:
        if existing.request_hash != digest:
            raise InvariantViolation(
                "idempotency.key",
                "该请求ID已用于不同的操作或内容，不能重复使用；请刷新后重试",
            )
        return ReplayResult(result=existing.result_json, target_id=existing.target_id)
    row = ReadingWriteReceiptRow(
        id=uuid4().hex,
        idempotency_key=key,
        operation=operation,
        target_id=target_id,
        request_hash=digest,
        result_json=None,
        created_at=now,
    )
    try:
        ReadingWriteReceiptRepository(session).add(row)
    except IntegrityError:
        raise InvariantViolation(
            "idempotency.key",
            "同一请求ID正在被并发处理，请稍后重试或先查询结果",
        ) from None
    return PendingReceipt(session=session, row=row)


def complete_keyed_write(
    receipt: PendingReceipt, *, target_id: str | None = None, result: dict[str, Any]
) -> None:
    """业务写入成功后回填原结果；随调用方事务一起提交。"""
    if target_id is not None:
        receipt.row.target_id = target_id
    receipt.row.result_json = result
