"""P7-S1 特殊桶目录、提名持久化与人工审核。

本模块只处理“提名 → 接受/拒绝 → Filing/状态”这一层；档位、配额、轮换与展示
属于 P7-S2/S3。提名本身不计算 P，已归入特殊桶的文献沿用 P5 的 special 排除。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from papersteward.core.entities import (
    Bucket,
    Event,
    NominationEvidence,
    Paper,
    PaperBucket,
    SpecialNominationRecord,
    SpecialTierSnapshot,
    StaticScoreSnapshot,
    utc_now,
)
from papersteward.core.enums import (
    SPECIAL_BUCKET_PRIORITY,
    AssignedBy,
    BucketRole,
    BucketType,
    EventType,
    PaperStatus,
    SpecialBucketKind,
    SpecialNominationStatus,
    TagRole,
)
from papersteward.core.errors import DomainError
from papersteward.core.invariants import choose_special_filing_kind, ensure_shelvable
from papersteward.errors import ConflictError, NotFoundError
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.pipeline.special import SpecialNominationResult
from papersteward.scoring.activation import append_future_activation_snapshots
from papersteward.scoring.service import read_scoring_config
from papersteward.scoring.special import DEFAULT_SPECIAL_QUOTAS, assess_special
from papersteward.storage.repositories import (
    BucketRepository,
    EventRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    SpecialNominationRepository,
    StaticScoreSnapshotRepository,
)

SPECIAL_BUCKETS: Final[dict[SpecialBucketKind, tuple[str, str]]] = {
    SpecialBucketKind.REVIEW: ("b-special-review", "跨层综述"),
    SpecialBucketKind.DISTANT: ("b-special-distant", "远邻领域"),
    SpecialBucketKind.CLASSIC: ("b-special-classic", "经典/奠基"),
}


@dataclass(frozen=True, slots=True)
class ShelfPromotion:
    """特殊桶提升回主题桶的结果；``static`` 是当前或新追加的静态快照。"""

    paper: Paper
    bucket: Bucket
    static: StaticScoreSnapshot


@dataclass(frozen=True, slots=True)
class ShelfDecision:
    """一次接受/拒绝的结果；``superseded`` 是接受时自动拒绝的其他待审提名。"""

    paper: Paper
    nomination: SpecialNominationRecord
    bucket: Bucket | None
    tier: SpecialTierSnapshot | None = None
    superseded: tuple[SpecialNominationRecord, ...] = ()


def ensure_special_buckets(session: Session) -> dict[SpecialBucketKind, Bucket]:
    """幂等补齐三个特殊桶；已存在但停用的桶不静默重建。"""
    repository = BucketRepository(session)
    existing = {
        bucket.special_kind: bucket
        for bucket in repository.list()
        if bucket.bucket_type is BucketType.SPECIAL and bucket.special_kind is not None
    }
    result: dict[SpecialBucketKind, Bucket] = {}
    for kind, (bucket_id, name) in SPECIAL_BUCKETS.items():
        bucket = existing.get(kind)
        if bucket is not None:
            if not bucket.active:
                raise ConflictError(f"特殊桶 {kind.value} 已存在但未激活")
            if bucket.quota is None:
                bucket = repository.set_quota(bucket.id, DEFAULT_SPECIAL_QUOTAS[kind])
            result[kind] = bucket
            continue
        collision = repository.get(bucket_id)
        if collision is not None:
            raise ConflictError(f"特殊桶 ID {bucket_id} 已被其他桶占用")
        created = Bucket(
            id=bucket_id,
            name=name,
            bucket_type=BucketType.SPECIAL,
            special_kind=kind,
            quota=DEFAULT_SPECIAL_QUOTAS[kind],
        )
        repository.add(created)
        result[kind] = created
    return result


def persist_nominations(
    session: Session,
    result: SpecialNominationResult,
    *,
    now: datetime | None = None,
) -> tuple[SpecialNominationRecord, ...]:
    """把 P4 提名落库为待审记录；重复运行只刷新未决策的同一 kind。"""
    repository = SpecialNominationRepository(session)
    timestamp = now or utc_now()
    records: list[SpecialNominationRecord] = []
    for nomination in result.nominations:
        record = SpecialNominationRecord(
            id=uuid4().hex,
            paper_id=result.paper_id,
            kind=nomination.kind,
            status=SpecialNominationStatus.PENDING,
            confidence=nomination.confidence,
            reason=nomination.reason,
            evidence=tuple(
                NominationEvidence(quote=entry.quote, valid=entry.valid)
                for entry in nomination.evidence
            ),
            review_layers=nomination.review_layers,
            classic_basis=nomination.classic_basis,
            bridge_memo=(
                nomination.bridge_memo.model_dump(mode="json")
                if nomination.bridge_memo is not None
                else None
            ),
            reasons=nomination.reasons,
            provenance=result.provenance,
            nominated_at=timestamp,
        )
        records.append(repository.upsert(record))
    return tuple(records)


def list_pending_nominations(
    session: Session, *, limit: int = 50, offset: int = 0
) -> tuple[list[tuple[SpecialNominationRecord, Paper]], int]:
    """待审提名及其文献；文献被删除时跳过，不伪造标题。"""
    records, total = SpecialNominationRepository(session).list_pending(limit=limit, offset=offset)
    papers = PaperRepository(session)
    result: list[tuple[SpecialNominationRecord, Paper]] = []
    for record in records:
        paper = papers.get(record.paper_id)
        if paper is not None:
            result.append((record, paper))
    return result, total


def _pending_for_paper(session: Session, paper_id: str) -> list[SpecialNominationRecord]:
    return [
        record
        for record in SpecialNominationRepository(session).list_for_paper(paper_id)
        if record.status is SpecialNominationStatus.PENDING
    ]


def _select_pending(
    pending: list[SpecialNominationRecord],
    kind: SpecialBucketKind | None,
) -> SpecialNominationRecord:
    if not pending:
        raise ConflictError("没有待审的特殊桶提名")
    if kind is None:
        selected = choose_special_filing_kind([record.kind for record in pending])
        if selected is None:  # pragma: no cover - choose 对非空列表不会返回 None
            raise ConflictError("无法确定特殊桶类型")
        kind = selected
    for record in pending:
        if record.kind is kind:
            return record
    raise NotFoundError(f"没有 {kind.value} 类型的待审提名")


def _classified_paper(session: Session, paper_id: str) -> tuple[Paper, str]:
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    if paper.status is not PaperStatus.CLASSIFIED:
        raise ConflictError("只有 classified 文献能归入特殊桶")
    tags = PaperTagRepository(session).list_for_paper(paper_id)
    primary = [tag for tag in tags if tag.role is TagRole.PRIMARY]
    if len(primary) != 1:
        raise ConflictError("文献缺少唯一主标签，不能归入特殊桶")
    return paper, primary[0].tag_code


def accept_nomination(
    session: Session,
    paper_id: str,
    *,
    kind: SpecialBucketKind | None = None,
    reviewed_by: str,
    note: str | None = None,
    force: bool = False,
    now: datetime | None = None,
    assigned_by: AssignedBy = AssignedBy.USER,
) -> ShelfDecision:
    """接受待审提名：special filing + shelved；同一文献其他待审提名作废。"""
    if not reviewed_by.strip():
        raise ConflictError("必须记录审核人")
    if force and not (note and note.strip()):
        raise ConflictError("覆盖未通过校验的提名必须提供备注")
    timestamp = now or utc_now()
    pending = _pending_for_paper(session, paper_id)
    nomination = _select_pending(pending, kind)
    if nomination.reasons and not force:
        raise ConflictError(
            "提名存在未通过校验："
            + ", ".join(nomination.reasons)
            + "；如确认接受请使用 --force 并记录备注"
        )
    paper, primary_tag = _classified_paper(session, paper_id)
    filings = [
        item
        for item in PaperBucketRepository(session).list_for_paper(paper_id)
        if item.role is BucketRole.FILING
    ]
    if len(filings) != 1:
        raise ConflictError("文献缺少唯一归属桶，不能归入特殊桶")
    current = BucketRepository(session).get(filings[0].bucket_id)
    if current is None:
        raise ConflictError("文献当前归属桶不存在")
    if current.bucket_type is BucketType.SPECIAL:
        raise ConflictError("文献已归属特殊桶；特殊桶间移动不在本卡范围")
    if current.tag_code != primary_tag:
        raise ConflictError("主题归属与主标签不一致；请先重新分类/审核")
    bucket = ensure_special_buckets(session)[nomination.kind]
    ensure_shelvable(status=paper.status, filing_bucket=bucket)

    PaperBucketRepository(session).replace_filing(
        paper_id,
        PaperBucket(
            paper_id=paper_id,
            bucket_id=bucket.id,
            role=BucketRole.FILING,
            assigned_by=assigned_by,
            reason=f"special nomination accepted: {nomination.kind.value}",
        ),
    )
    updated = PaperRepository(session).transition(
        paper_id,
        PaperStatus.SHELVED,
        event_id=uuid4().hex,
        occurred_at=timestamp,
        payload={
            "stage": "shelf",
            "special_kind": nomination.kind.value,
            "nomination_id": nomination.id,
            "decided_by": reviewed_by.strip(),
        },
    )
    if updated is None:  # pragma: no cover - 已在上方确认存在
        raise NotFoundError("文献不存在")
    EventRepository(session).append(
        Event(
            id=uuid4().hex,
            event_type=EventType.PAPER_BUCKET_MOVED,
            created_at=timestamp,
            paper_id=paper_id,
            payload_json={
                "stage": "shelf",
                "from_bucket_id": current.id,
                "to_bucket_id": bucket.id,
                "special_kind": nomination.kind.value,
            },
        )
    )
    decided = SpecialNominationRepository(session).decide(
        nomination.id,
        status=SpecialNominationStatus.ACCEPTED,
        decided_by=reviewed_by.strip(),
        note=note,
        now=timestamp,
    )
    tier = assess_special(
        session,
        paper_id,
        nomination=decided,
        as_of=timestamp.date(),
        now=timestamp,
    )
    superseded = tuple(
        SpecialNominationRepository(session).decide(
            other.id,
            status=SpecialNominationStatus.REJECTED,
            decided_by=reviewed_by.strip(),
            note=f"superseded by {nomination.kind.value}",
            now=timestamp,
        )
        for other in pending
        if other.id != nomination.id
    )
    return ShelfDecision(
        paper=updated,
        nomination=decided,
        bucket=bucket,
        tier=tier,
        superseded=superseded,
    )


def auto_decide_special(session: Session, paper_id: str, *, now: datetime) -> bool:
    """按特殊桶优先级接受有效且高置信提名，其余自动拒绝。"""
    pending = sorted(
        _pending_for_paper(session, paper_id),
        key=lambda item: (-SPECIAL_BUCKET_PRIORITY[item.kind], item.id),
    )
    for nomination in pending:
        if nomination.confidence >= 0.8 and not nomination.reasons:
            try:
                # 接受失败时撤销该次归属、状态、档位等所有写入。
                with session.begin_nested():
                    accept_nomination(
                        session,
                        paper_id,
                        kind=nomination.kind,
                        reviewed_by="automatic",
                        now=now,
                        assigned_by=AssignedBy.SYSTEM,
                    )
                return True
            except (DomainError, ConflictError, NotFoundError, SQLAlchemyError):
                pass
        reject_nomination(
            session,
            paper_id,
            kind=nomination.kind,
            reviewed_by="automatic",
            note="低置信、校验未通过或无法自动归入",
            now=now,
        )
    return False


def promote_to_topic(
    session: Session,
    paper_id: str,
    *,
    reviewed_by: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ShelfPromotion:
    """把 shelved 特殊桶文献提升到主标签主题桶，并确保静态分可用。"""
    if not reviewed_by.strip():
        raise ConflictError("必须记录审核人")
    timestamp = now or utc_now()
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    if paper.status is not PaperStatus.SHELVED:
        raise ConflictError("只有 shelved 特殊桶文献可以提升到主题桶")
    filings = [
        item
        for item in PaperBucketRepository(session).list_for_paper(paper_id)
        if item.role is BucketRole.FILING
    ]
    if len(filings) != 1:
        raise ConflictError("文献缺少唯一归属桶")
    current = BucketRepository(session).get(filings[0].bucket_id)
    if current is None or current.bucket_type is not BucketType.SPECIAL:
        raise ConflictError("文献当前未归属特殊桶")
    tags = PaperTagRepository(session).list_for_paper(paper_id)
    primary = [tag.tag_code for tag in tags if tag.role is TagRole.PRIMARY]
    if len(primary) != 1:
        raise ConflictError("文献缺少唯一主标签")
    topic = next(
        (
            bucket
            for bucket in BucketRepository(session).list(active_only=True)
            if bucket.bucket_type is BucketType.TOPIC and bucket.tag_code == primary[0]
        ),
        None,
    )
    if topic is None:
        raise ConflictError("主标签没有可用的激活主题桶")

    _, today_binding = bound_context(
        session,
        timestamp,
        action="shelf.promote",
        enforce_schedule=False,
    )
    repository = StaticScoreSnapshotRepository(session)
    latest = repository.latest(paper_id)
    if latest is None:
        raise ConflictError("文献缺少静态分快照，无法提升")
    local_day = runtime_local_day(session, timestamp, today_binding)
    target_day = max(local_day, latest.score_date)
    _, target_binding = bound_context(
        session,
        timestamp,
        action="shelf.promote",
        enforce_schedule=False,
        day=target_day,
    )
    config = read_scoring_config(session, version=target_binding["scoring_version"])
    if config is None:
        raise ConflictError("目标日的评分配置不存在")
    if latest.relevance < max(55.0, config.gates.min_relevance_for_push):
        raise ConflictError("相关度低于主队列门槛，不能提升到主题桶")
    versions = (
        config.version,
        target_binding["profile_version"],
        target_binding["taxonomy_version"],
    )
    if (latest.config_version, latest.profile_version, latest.taxonomy_version) == versions:
        static = latest
    else:
        append_day = max(local_day, latest.score_date + timedelta(days=1))
        _, append_binding = bound_context(
            session,
            timestamp,
            action="shelf.promote",
            enforce_schedule=False,
            day=append_day,
        )
        append_config = read_scoring_config(session, version=append_binding["scoring_version"])
        if append_config is None:
            raise ConflictError("补写日的评分配置不存在")
        static_score = round(
            latest.relevance * append_config.static.relevance
            + latest.authority * append_config.static.authority
            + latest.extra_value * append_config.static.extra_value,
            1,
        )
        breakdown = dict(latest.breakdown_json)
        breakdown["promoted"] = {"from_input_hash": latest.input_hash}
        static = repository.append(
            StaticScoreSnapshot(
                paper_id=paper_id,
                score_date=append_day,
                relevance=latest.relevance,
                authority=latest.authority,
                extra_value=latest.extra_value,
                static_score=static_score,
                confidence=latest.confidence,
                config_version=append_config.version,
                profile_version=append_binding["profile_version"],
                taxonomy_version=append_binding["taxonomy_version"],
                input_hash=hashlib.sha256(
                    f"{latest.input_hash}:promote:{append_config.version}:"
                    f"{append_day.isoformat()}".encode()
                ).hexdigest(),
                breakdown_json=breakdown,
                created_at=timestamp,
            )
        )
    append_future_activation_snapshots(
        session,
        paper_id=paper_id,
        relevance=static.relevance,
        authority=static.authority,
        extra_value=static.extra_value,
        confidence=static.confidence,
        profile_version=static.profile_version,
        taxonomy_version=static.taxonomy_version,
        base_input_hash=static.input_hash,
        breakdown=static.breakdown_json,
        after_day=static.score_date,
        now=timestamp,
    )

    PaperBucketRepository(session).replace_filing(
        paper_id,
        PaperBucket(
            paper_id=paper_id,
            bucket_id=topic.id,
            role=BucketRole.FILING,
            assigned_by=AssignedBy.USER,
            reason="promoted from special shelf",
        ),
    )
    updated = PaperRepository(session).transition(
        paper_id,
        PaperStatus.CLASSIFIED,
        event_id=uuid4().hex,
        occurred_at=timestamp,
        payload={
            "stage": "shelf",
            "action": "promote",
            "from_bucket_id": current.id,
            "to_bucket_id": topic.id,
            "decided_by": reviewed_by.strip(),
            "note": note,
        },
    )
    if updated is None:  # pragma: no cover
        raise NotFoundError("文献不存在")
    EventRepository(session).append(
        Event(
            id=uuid4().hex,
            event_type=EventType.PAPER_BUCKET_MOVED,
            created_at=timestamp,
            paper_id=paper_id,
            payload_json={
                "stage": "shelf",
                "action": "promote",
                "from_bucket_id": current.id,
                "to_bucket_id": topic.id,
            },
        )
    )
    return ShelfPromotion(paper=updated, bucket=topic, static=static)


def reject_nomination(
    session: Session,
    paper_id: str,
    *,
    kind: SpecialBucketKind | None = None,
    reviewed_by: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ShelfDecision:
    """拒绝待审提名；不改变文献状态、主标签或主题归属。"""
    if not reviewed_by.strip():
        raise ConflictError("必须记录审核人")
    pending = _pending_for_paper(session, paper_id)
    nomination = _select_pending(pending, kind)
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    decided = SpecialNominationRepository(session).decide(
        nomination.id,
        status=SpecialNominationStatus.REJECTED,
        decided_by=reviewed_by.strip(),
        note=note,
        now=now or utc_now(),
    )
    return ShelfDecision(paper=paper, nomination=decided, bucket=None)


def reopen_nomination(
    session: Session,
    paper_id: str,
    *,
    kind: SpecialBucketKind | None = None,
    reviewed_by: str,
    note: str | None = None,
    now: datetime | None = None,
) -> SpecialNominationRecord:
    """把已拒绝提名受控回到 pending；只留审计事件，不重新跑 LLM。"""
    if not reviewed_by.strip():
        raise ConflictError("必须记录审核人")
    timestamp = now or utc_now()
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    if paper.status is not PaperStatus.CLASSIFIED:
        raise ConflictError("只有 classified 文献可以重新打开特殊桶提名")
    repository = SpecialNominationRepository(session)
    records = repository.list_for_paper(paper_id)
    rejected = [item for item in records if item.status is SpecialNominationStatus.REJECTED]
    if kind is not None:
        rejected = [item for item in rejected if item.kind is kind]
    if not rejected:
        raise ConflictError("没有可重新打开的被拒绝提名")
    rejected.sort(key=lambda item: (-SPECIAL_BUCKET_PRIORITY[item.kind], item.kind.value))
    target = rejected[0]
    reopened = repository.reopen(target.id, now=timestamp)
    EventRepository(session).append(
        Event(
            id=uuid4().hex,
            event_type=EventType.SPECIAL_NOMINATION_REOPENED,
            created_at=timestamp,
            paper_id=paper_id,
            payload_json={
                "nomination_id": target.id,
                "kind": target.kind.value,
                "previous_decided_at": (
                    target.decided_at.isoformat() if target.decided_at else None
                ),
                "previous_decided_by": target.decided_by,
                "previous_note": target.decision_note,
                "reopened_by": reviewed_by.strip(),
                "note": note,
            },
        )
    )
    return reopened


__all__ = [
    "SPECIAL_BUCKETS",
    "ShelfDecision",
    "ShelfPromotion",
    "accept_nomination",
    "ensure_special_buckets",
    "list_pending_nominations",
    "persist_nominations",
    "promote_to_topic",
    "reopen_nomination",
    "reject_nomination",
]
