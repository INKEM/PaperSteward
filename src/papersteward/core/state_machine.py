"""状态机。

- 文献：按设计文档 §2.5 的流水线迁移，``needs_review`` 不能直达队列/推送；
- 文档：``draft → confirmed → archived``；
- 运行：``running → succeeded/failed/cancelled``。

状态机只判断“允许的边”；队列门槛、评分冻结等需要上下文的规则在
``papersteward.core.invariants`` 中校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Final, TypeVar

from papersteward.core.entities import Paper, ResearchProfile, Run, Taxonomy
from papersteward.core.enums import DocumentStatus, EventType, PaperStatus, RunStatus
from papersteward.core.errors import InvalidTransition

PAPER_TRANSITIONS: Final[Mapping[PaperStatus, frozenset[PaperStatus]]] = MappingProxyType(
    {
        PaperStatus.DISCOVERED: frozenset({PaperStatus.DEDUPED, PaperStatus.REJECTED}),
        PaperStatus.DEDUPED: frozenset({PaperStatus.SCREENED, PaperStatus.REJECTED}),
        PaperStatus.SCREENED: frozenset(
            {PaperStatus.ENRICHED, PaperStatus.NEEDS_REVIEW, PaperStatus.REJECTED}
        ),
        PaperStatus.NEEDS_REVIEW: frozenset(
            {
                PaperStatus.ENRICHED,
                PaperStatus.SUMMARIZED,
                PaperStatus.CLASSIFIED,
                PaperStatus.REJECTED,
            }
        ),
        PaperStatus.ENRICHED: frozenset({PaperStatus.SUMMARIZED, PaperStatus.NEEDS_REVIEW}),
        PaperStatus.SUMMARIZED: frozenset({PaperStatus.CLASSIFIED, PaperStatus.NEEDS_REVIEW}),
        PaperStatus.CLASSIFIED: frozenset(
            {PaperStatus.SCORED, PaperStatus.SHELVED, PaperStatus.NEEDS_REVIEW}
        ),
        PaperStatus.SHELVED: frozenset(
            {
                PaperStatus.CLASSIFIED,
                PaperStatus.SCORED,
                PaperStatus.PUSHED,
                PaperStatus.READING,
                PaperStatus.REJECTED,
            }
        ),
        PaperStatus.SCORED: frozenset(
            {
                PaperStatus.QUEUED,
                PaperStatus.SHELVED,
                PaperStatus.NEEDS_REVIEW,
                PaperStatus.REJECTED,
            }
        ),
        PaperStatus.QUEUED: frozenset(
            {
                PaperStatus.PUSHED,
                PaperStatus.SNOOZED,
                PaperStatus.SHELVED,
                PaperStatus.REJECTED,
            }
        ),
        PaperStatus.SNOOZED: frozenset({PaperStatus.QUEUED, PaperStatus.REJECTED}),
        PaperStatus.PUSHED: frozenset(
            {
                PaperStatus.READING,
                PaperStatus.QUEUED,
                PaperStatus.SNOOZED,
                PaperStatus.REJECTED,
            }
        ),
        PaperStatus.READING: frozenset(
            {PaperStatus.DONE, PaperStatus.SNOOZED, PaperStatus.REJECTED}
        ),
        PaperStatus.DONE: frozenset({PaperStatus.ARCHIVED}),
        PaperStatus.ARCHIVED: frozenset(),
        # 用户可从拒绝列表手动恢复；恢复后重新进入筛选，不直接回到队列。
        PaperStatus.REJECTED: frozenset({PaperStatus.SCREENED}),
    }
)

DOCUMENT_TRANSITIONS: Final[Mapping[DocumentStatus, frozenset[DocumentStatus]]] = MappingProxyType(
    {
        DocumentStatus.DRAFT: frozenset({DocumentStatus.CONFIRMED}),
        DocumentStatus.CONFIRMED: frozenset({DocumentStatus.ARCHIVED}),
        DocumentStatus.ARCHIVED: frozenset(),
    }
)

RUN_TRANSITIONS: Final[Mapping[RunStatus, frozenset[RunStatus]]] = MappingProxyType(
    {
        RunStatus.RUNNING: frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}),
        RunStatus.SUCCEEDED: frozenset(),
        RunStatus.FAILED: frozenset(),
        RunStatus.CANCELLED: frozenset(),
    }
)

PAPER_STATUS_EVENTS: Final[Mapping[PaperStatus, EventType]] = MappingProxyType(
    {
        PaperStatus.DISCOVERED: EventType.PAPER_DISCOVERED,
        PaperStatus.DEDUPED: EventType.PAPER_DEDUPED,
        PaperStatus.REJECTED: EventType.PAPER_REJECTED,
        PaperStatus.SCREENED: EventType.PAPER_SCREENED,
        PaperStatus.NEEDS_REVIEW: EventType.PAPER_NEEDS_REVIEW,
        PaperStatus.ENRICHED: EventType.PAPER_ENRICHED,
        PaperStatus.SUMMARIZED: EventType.PAPER_SUMMARIZED,
        PaperStatus.CLASSIFIED: EventType.PAPER_CLASSIFIED,
        PaperStatus.SHELVED: EventType.PAPER_SHELVED,
        PaperStatus.SCORED: EventType.PAPER_SCORED,
        PaperStatus.QUEUED: EventType.PAPER_QUEUED,
        PaperStatus.SNOOZED: EventType.PAPER_SNOOZED,
        PaperStatus.PUSHED: EventType.PAPER_PUSHED,
        PaperStatus.READING: EventType.PAPER_READING,
        PaperStatus.DONE: EventType.PAPER_DONE,
        PaperStatus.ARCHIVED: EventType.PAPER_ARCHIVED,
    }
)

DocumentT = TypeVar("DocumentT", ResearchProfile, Taxonomy)


def allowed_paper_transitions(status: PaperStatus) -> frozenset[PaperStatus]:
    """返回某个状态允许迁移到的目标状态集合。"""
    return PAPER_TRANSITIONS[status]


def can_transition(current: PaperStatus, target: PaperStatus) -> bool:
    """状态不迁移到自身；只按白名单判断。"""
    return target in PAPER_TRANSITIONS[current]


def ensure_paper_transition(
    current: PaperStatus, target: PaperStatus, *, paper_id: str | None = None
) -> None:
    """不合法时抛出 ``InvalidTransition``，错误信息包含文献与两端状态。"""
    if can_transition(current, target):
        return
    subject = f"文献 {paper_id} " if paper_id else "文献"
    raise InvalidTransition(
        "paper.status",
        f"{subject}不能从 {current.value} 迁移到 {target.value}",
    )


def transition_paper(paper: Paper, target: PaperStatus) -> Paper:
    """返回迁移后的新 ``Paper``；原对象不变。"""
    ensure_paper_transition(paper.status, target, paper_id=paper.id)
    return replace(paper, status=target)


def ensure_document_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    if target not in DOCUMENT_TRANSITIONS[current]:
        raise InvalidTransition(
            "document.status",
            f"文档不能从 {current.value} 迁移到 {target.value}",
        )


def transition_document(
    document: DocumentT,
    target: DocumentStatus,
    *,
    confirmed_at: datetime | None = None,
) -> DocumentT:
    """迁移 Profile/Taxonomy；确认时必须显式传入确认时间。"""
    ensure_document_transition(document.status, target)
    if target is DocumentStatus.CONFIRMED:
        if confirmed_at is None:
            raise InvalidTransition("document.confirmed_at", "确认文档时必须提供确认时间")
        return replace(document, status=target, confirmed_at=confirmed_at)
    return replace(document, status=target)


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidTransition(
            "run.status",
            f"运行不能从 {current.value} 迁移到 {target.value}",
        )


def transition_run(run: Run, target: RunStatus, *, finished_at: datetime | None = None) -> Run:
    """迁移运行状态；终态时间由调用方传入，避免核心层偷偷读取时钟。"""
    ensure_run_transition(run.status, target)
    if finished_at is None:
        return replace(run, status=target)
    return replace(run, status=target, finished_at=finished_at)


__all__ = [
    "DOCUMENT_TRANSITIONS",
    "PAPER_STATUS_EVENTS",
    "PAPER_TRANSITIONS",
    "RUN_TRANSITIONS",
    "allowed_paper_transitions",
    "can_transition",
    "ensure_document_transition",
    "ensure_paper_transition",
    "ensure_run_transition",
    "transition_document",
    "transition_paper",
    "transition_run",
]
