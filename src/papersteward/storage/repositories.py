"""P1 仓储：领域实体与 SQLAlchemy 行之间的边界。

约定：

- 所有方法只 ``flush``，不 ``commit``；事务由调用方 ``session_scope`` 控制；
- 事件、评分快照、归档快照只提供追加/读取接口，不提供 update/delete；
- 状态迁移经领域状态机校验后再写库，并同步追加事件；
- DB 约束与 ``core.invariants`` 形成双重校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from papersteward.core.entities import (
    ArchiveSnapshot,
    Bucket,
    Config,
    Enrichment,
    Event,
    Paper,
    PaperBucket,
    PaperTag,
    Rating,
    ResearchProfile,
    ReviewItem,
    Run,
    ScoreSnapshot,
    ScoringChange,
    SpecialNominationRecord,
    SpecialTierSnapshot,
    StaticScoreSnapshot,
    Summary,
    Taxonomy,
    ViewConfig,
    utc_now,
)
from papersteward.core.enums import (
    BucketRole,
    DocumentStatus,
    PaperStatus,
    ReviewResolution,
    ReviewStatus,
    RunStatus,
    ScoringChangeStatus,
    SpecialBucketKind,
    SpecialNominationStatus,
)
from papersteward.core.errors import InvariantViolation
from papersteward.core.invariants import (
    ensure_event_appended,
    ensure_score_snapshot_append_only,
    ensure_secondary_tag_limit,
    ensure_single_filing_bucket,
    ensure_single_primary_tag,
    ensure_static_score_snapshot_append_only,
)
from papersteward.core.state_machine import (
    PAPER_STATUS_EVENTS,
    transition_document,
    transition_paper,
    transition_run,
)
from papersteward.storage.mappers import (
    archive_snapshot_from_row,
    archive_snapshot_to_row,
    as_utc,
    bucket_from_row,
    bucket_to_row,
    config_from_row,
    config_to_row,
    enrichment_from_row,
    enrichment_to_row,
    event_from_row,
    event_to_row,
    paper_bucket_from_row,
    paper_bucket_to_row,
    paper_from_row,
    paper_tag_from_row,
    paper_tag_to_row,
    paper_to_row,
    profile_from_row,
    profile_to_row,
    rating_from_row,
    rating_to_row,
    review_item_from_row,
    review_item_to_row,
    run_from_row,
    run_to_row,
    score_snapshot_from_row,
    score_snapshot_to_row,
    scoring_change_from_row,
    scoring_change_to_row,
    special_nomination_from_row,
    special_nomination_to_row,
    special_tier_from_row,
    special_tier_to_row,
    static_score_snapshot_from_row,
    static_score_snapshot_to_row,
    summary_from_row,
    summary_to_row,
    taxonomy_from_row,
    taxonomy_to_row,
    view_config_from_row,
    view_config_to_row,
)
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    BucketRow,
    ConfigRow,
    EnrichmentRow,
    EventRow,
    PaperBucketRow,
    PaperRow,
    PaperTagRow,
    RatingRow,
    ResearchProfileRow,
    ReviewItemRow,
    RunRow,
    ScoreSnapshotRow,
    ScoringChangeRow,
    SpecialNominationRow,
    SpecialTierSnapshotRow,
    StaticScoreSnapshotRow,
    SummaryRow,
    TaxonomyRow,
    ViewConfigRow,
)
from papersteward.storage.onboarding import ensure_active


def _check_execution_gate(session: Session, target: PaperStatus) -> None:
    if target in {PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.ARCHIVED}:
        ensure_active(session, action=target.value)


class _Repository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _flush(self) -> None:
        self._session.flush()


class PaperRepository(_Repository):
    """文献主记录与状态迁移。"""

    def add(self, paper: Paper) -> Paper:
        _check_execution_gate(self._session, paper.status)
        self._session.add(paper_to_row(paper))
        self._flush()
        return paper

    def get(self, paper_id: str) -> Paper | None:
        row = self._session.get(PaperRow, paper_id)
        return None if row is None else paper_from_row(row)

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Paper]:
        statement = select(PaperRow).order_by(PaperRow.first_seen_at, PaperRow.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return [paper_from_row(row) for row in self._session.scalars(statement).all()]

    def count(self) -> int:
        return self._session.scalar(select(func.count()).select_from(PaperRow)) or 0

    def transition(
        self,
        paper_id: str,
        target: PaperStatus,
        *,
        event_id: str,
        occurred_at: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Paper | None:
        """按状态机迁移并追加一条状态事件；不合法迁移不写库。"""
        row = self._session.get(PaperRow, paper_id)
        if row is None:
            return None
        updated = transition_paper(paper_from_row(row), target)
        _check_execution_gate(self._session, target)
        event = Event(
            id=event_id,
            event_type=PAPER_STATUS_EVENTS[target],
            created_at=occurred_at or utc_now(),
            paper_id=paper_id,
            payload_json=dict(payload or {}),
        )
        # 先校验/追加事件，再更新状态；回填时间不会留下半更新状态。
        EventRepository(self._session).append(event)
        row.status = updated.status.value
        self._flush()
        return updated


class PaperTagRepository(_Repository):
    """文献标签分配。

    写入后按聚合约验重新检查“恰好一个主标签 + 副标签 0–5 条”。因此首次写入
    必须是主标签；中间态非法时调用方必须回滚。
    """

    def _validate_paper(self, paper_id: str) -> None:
        tags = self.list_for_paper(paper_id)
        ensure_single_primary_tag(tags, paper_id=paper_id)
        ensure_secondary_tag_limit(tags, paper_id=paper_id)

    def add(self, tag: PaperTag) -> PaperTag:
        self._session.add(paper_tag_to_row(tag))
        self._flush()
        self._validate_paper(tag.paper_id)
        return tag

    def add_many(self, tags: Sequence[PaperTag]) -> tuple[PaperTag, ...]:
        if not tags:
            return ()
        paper_ids = {tag.paper_id for tag in tags}
        if len(paper_ids) != 1:
            raise InvariantViolation("paper_tag.paper_id", "批量标签必须属于同一篇文献")
        self._session.add_all(paper_tag_to_row(tag) for tag in tags)
        self._flush()
        self._validate_paper(tags[0].paper_id)
        return tuple(tags)

    def replace_all(self, paper_id: str, tags: Sequence[PaperTag]) -> tuple[PaperTag, ...]:
        """人工改标签：删除旧分配并原子写入新分配（调用方控制事务）。"""
        if not tags:
            raise InvariantViolation("paper_tag", "至少需要一个主标签")
        if any(tag.paper_id != paper_id for tag in tags):
            raise InvariantViolation("paper_tag.paper_id", "标签必须属于同一篇文献")
        self._session.execute(delete(PaperTagRow).where(PaperTagRow.paper_id == paper_id))
        self._session.add_all(paper_tag_to_row(tag) for tag in tags)
        self._flush()
        self._validate_paper(paper_id)
        return tuple(tags)

    def list_for_paper(self, paper_id: str) -> list[PaperTag]:
        statement = (
            select(PaperTagRow)
            .where(PaperTagRow.paper_id == paper_id)
            .order_by(PaperTagRow.role, PaperTagRow.tag_code)
        )
        return [paper_tag_from_row(row) for row in self._session.scalars(statement).all()]


class PaperBucketRepository(_Repository):
    """文献与主题桶/特殊桶的关系。

    写入后校验每篇恰好一个 filing；首次写入必须是 filing，membership 可后加。
    """

    def _validate_paper(self, paper_id: str) -> None:
        ensure_single_filing_bucket(self.list_for_paper(paper_id), paper_id=paper_id)

    def add(self, bucket: PaperBucket) -> PaperBucket:
        self._session.add(paper_bucket_to_row(bucket))
        self._flush()
        self._validate_paper(bucket.paper_id)
        return bucket

    def add_many(self, buckets: Sequence[PaperBucket]) -> tuple[PaperBucket, ...]:
        if not buckets:
            return ()
        paper_ids = {bucket.paper_id for bucket in buckets}
        if len(paper_ids) != 1:
            raise InvariantViolation("paper_bucket.paper_id", "批量桶关系必须属于同一篇文献")
        self._session.add_all(paper_bucket_to_row(bucket) for bucket in buckets)
        self._flush()
        self._validate_paper(buckets[0].paper_id)
        return tuple(buckets)

    def replace_filing(self, paper_id: str, filing: PaperBucket) -> PaperBucket:
        """人工改主标签时替换唯一归属桶，保留其他成员关系。"""
        if filing.paper_id != paper_id or filing.role is not BucketRole.FILING:
            raise InvariantViolation("paper_bucket", "必须提供同一篇文献的 FILING 桶")
        self._session.execute(
            delete(PaperBucketRow).where(
                PaperBucketRow.paper_id == paper_id,
                PaperBucketRow.role == BucketRole.FILING.value,
            )
        )
        self._session.add(paper_bucket_to_row(filing))
        self._flush()
        self._validate_paper(paper_id)
        return filing

    def list_for_paper(self, paper_id: str) -> list[PaperBucket]:
        statement = (
            select(PaperBucketRow)
            .where(PaperBucketRow.paper_id == paper_id)
            .order_by(PaperBucketRow.role, PaperBucketRow.bucket_id)
        )
        return [paper_bucket_from_row(row) for row in self._session.scalars(statement).all()]

    def list_filings(self, bucket_id: str) -> list[PaperBucket]:
        """某个桶的归属文献；读取特殊桶视图用。"""
        statement = (
            select(PaperBucketRow)
            .where(
                PaperBucketRow.bucket_id == bucket_id,
                PaperBucketRow.role == BucketRole.FILING.value,
            )
            .order_by(PaperBucketRow.paper_id)
        )
        return [paper_bucket_from_row(row) for row in self._session.scalars(statement).all()]


class SummaryRepository(_Repository):
    """每篇文献一条概括；重新生成时覆盖同一条记录。"""

    def save(self, summary: Summary) -> Summary:
        row = self._session.get(SummaryRow, summary.paper_id)
        if row is None:
            self._session.add(summary_to_row(summary))
        else:
            mapped = summary_to_row(summary)
            row.summary_100 = mapped.summary_100
            row.what = mapped.what
            row.how = mapped.how
            row.finding = mapped.finding
            row.limitation = mapped.limitation
            row.evidence_json = mapped.evidence_json
            row.validation_status = mapped.validation_status
            row.provenance_model = mapped.provenance_model
            row.provenance_prompt_version = mapped.provenance_prompt_version
            row.provenance_generated_at = mapped.provenance_generated_at
            row.provenance_input_hash = mapped.provenance_input_hash
        self._flush()
        return summary

    def get(self, paper_id: str) -> Summary | None:
        row = self._session.get(SummaryRow, paper_id)
        return None if row is None else summary_from_row(row)


class EnrichmentRepository(_Repository):
    """每篇文献一条富化结果；相同内容重复保存不改变 updated_at。"""

    _DATA_FIELDS = (
        "teams_json",
        "venue_level",
        "citation_count",
        "citation_velocity",
        "code_url",
        "data_url",
        "oa_status",
        "experiment_quality_json",
        "confidence",
        "source",
        "provenance_model",
        "provenance_prompt_version",
        "provenance_generated_at",
        "provenance_input_hash",
    )

    def save(self, enrichment: Enrichment) -> tuple[Enrichment, bool]:
        """新增或更新；返回 ``(实际保存值, 是否发生变化)``，不提交事务。"""
        row = self._session.get(EnrichmentRow, enrichment.paper_id)
        if row is None:
            self._session.add(enrichment_to_row(enrichment))
            self._flush()
            return enrichment, True
        existing = enrichment_from_row(row)
        if existing.signals == enrichment.signals and existing.provenance == enrichment.provenance:
            return existing, False
        mapped = enrichment_to_row(enrichment)
        for field in self._DATA_FIELDS:
            setattr(row, field, getattr(mapped, field))
        # created_at 保持首次写入；时间戳只前进，不回退。
        row.updated_at = max(existing.updated_at, enrichment.updated_at)
        self._flush()
        return enrichment_from_row(row), True

    def get(self, paper_id: str) -> Enrichment | None:
        row = self._session.get(EnrichmentRow, paper_id)
        return None if row is None else enrichment_from_row(row)

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Enrichment]:
        statement = select(EnrichmentRow).order_by(EnrichmentRow.paper_id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return [enrichment_from_row(row) for row in self._session.scalars(statement).all()]


class ScoreSnapshotRepository(_Repository):
    """评分快照追加写。"""

    def append(self, snapshot: ScoreSnapshot) -> ScoreSnapshot:
        ensure_score_snapshot_append_only(self.history(snapshot.paper_id), snapshot)
        self._session.add(score_snapshot_to_row(snapshot))
        self._flush()
        return snapshot

    def history(self, paper_id: str) -> list[ScoreSnapshot]:
        statement = (
            select(ScoreSnapshotRow)
            .where(ScoreSnapshotRow.paper_id == paper_id)
            .order_by(ScoreSnapshotRow.score_date)
        )
        return [score_snapshot_from_row(row) for row in self._session.scalars(statement).all()]

    def latest(self, paper_id: str) -> ScoreSnapshot | None:
        statement = (
            select(ScoreSnapshotRow)
            .where(ScoreSnapshotRow.paper_id == paper_id)
            .order_by(ScoreSnapshotRow.score_date.desc())
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else score_snapshot_from_row(row)


class StaticScoreSnapshotRepository(_Repository):
    """P4 静态分快照：版本变化后按日期追加，不提供 update/delete。"""

    def append(self, snapshot: StaticScoreSnapshot) -> StaticScoreSnapshot:
        ensure_static_score_snapshot_append_only(self.history(snapshot.paper_id), snapshot)
        self._session.add(static_score_snapshot_to_row(snapshot))
        self._flush()
        return snapshot

    def history(self, paper_id: str) -> list[StaticScoreSnapshot]:
        statement = (
            select(StaticScoreSnapshotRow)
            .where(StaticScoreSnapshotRow.paper_id == paper_id)
            .order_by(StaticScoreSnapshotRow.score_date)
        )
        return [
            static_score_snapshot_from_row(row) for row in self._session.scalars(statement).all()
        ]

    def latest(self, paper_id: str) -> StaticScoreSnapshot | None:
        statement = (
            select(StaticScoreSnapshotRow)
            .where(StaticScoreSnapshotRow.paper_id == paper_id)
            .order_by(StaticScoreSnapshotRow.score_date.desc())
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else static_score_snapshot_from_row(row)


class ReviewRepository(_Repository):
    """人工审核队列；每篇一条，开放项可重复更新，处理后保留原因/结果。"""

    def get(self, paper_id: str) -> ReviewItem | None:
        row = self._session.get(ReviewItemRow, paper_id)
        return None if row is None else review_item_from_row(row)

    def list_open(self, *, limit: int = 50, offset: int = 0) -> tuple[list[ReviewItem], int]:
        statement = (
            select(ReviewItemRow)
            .where(ReviewItemRow.status == ReviewStatus.OPEN.value)
            .order_by(ReviewItemRow.updated_at.desc(), ReviewItemRow.paper_id)
            .offset(offset)
            .limit(limit)
        )
        items = [review_item_from_row(row) for row in self._session.scalars(statement).all()]
        total = (
            self._session.scalar(
                select(func.count())
                .select_from(ReviewItemRow)
                .where(ReviewItemRow.status == ReviewStatus.OPEN.value)
            )
            or 0
        )
        return items, total

    def open(
        self,
        paper_id: str,
        reasons: Sequence[str],
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> ReviewItem:
        """创建或重新打开审核项；相同 paper_id 只保留一条。"""
        resolved_reasons = tuple(
            dict.fromkeys(reason.strip() for reason in reasons if reason.strip())
        )
        if not resolved_reasons:
            raise InvariantViolation("review.reasons", "至少需要一个审核原因")
        row = self._session.get(ReviewItemRow, paper_id)
        if row is None:
            item = ReviewItem(
                paper_id=paper_id,
                status=ReviewStatus.OPEN,
                reasons=resolved_reasons,
                payload_json=dict(payload),
                created_at=now,
                updated_at=now,
            )
            self._session.add(review_item_to_row(item))
            self._flush()
            return item
        updated = ReviewItem(
            paper_id=paper_id,
            status=ReviewStatus.OPEN,
            reasons=resolved_reasons,
            payload_json=dict(payload),
            created_at=as_utc(row.created_at),
            updated_at=now,
        )
        mapped = review_item_to_row(updated)
        row.status = mapped.status
        row.reasons_json = mapped.reasons_json
        row.payload_json = mapped.payload_json
        row.updated_at = mapped.updated_at
        row.resolved_at = None
        row.resolved_by = None
        row.resolution = None
        row.note = None
        self._flush()
        return updated

    def resolve(
        self,
        paper_id: str,
        *,
        resolved_by: str,
        resolution: ReviewResolution,
        note: str | None,
        now: datetime,
    ) -> ReviewItem:
        row = self._session.get(ReviewItemRow, paper_id)
        if row is None or row.status != ReviewStatus.OPEN.value:
            raise InvariantViolation("review.status", "审核项不存在或已处理")
        item = ReviewItem(
            paper_id=paper_id,
            status=ReviewStatus.RESOLVED,
            reasons=tuple(row.reasons_json or ()),
            payload_json=dict(row.payload_json or {}),
            created_at=as_utc(row.created_at),
            updated_at=now,
            resolved_at=now,
            resolved_by=resolved_by,
            resolution=resolution,
            note=note,
        )
        mapped = review_item_to_row(item)
        for field in (
            "status",
            "updated_at",
            "resolved_at",
            "resolved_by",
            "resolution",
            "note",
        ):
            setattr(row, field, getattr(mapped, field))
        self._flush()
        return item


class SpecialNominationRepository(_Repository):
    """特殊桶提名与人工决策。

    ``upsert`` 只刷新待审提名；已接受/已拒绝的决策不会被 process 重跑覆盖。
    """

    def get(self, nomination_id: str) -> SpecialNominationRecord | None:
        row = self._session.get(SpecialNominationRow, nomination_id)
        return None if row is None else special_nomination_from_row(row)

    def list_for_paper(self, paper_id: str) -> list[SpecialNominationRecord]:
        statement = (
            select(SpecialNominationRow)
            .where(SpecialNominationRow.paper_id == paper_id)
            .order_by(SpecialNominationRow.kind, SpecialNominationRow.id)
        )
        return [special_nomination_from_row(row) for row in self._session.scalars(statement).all()]

    def list_pending(
        self,
        *,
        kind: SpecialBucketKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SpecialNominationRecord], int]:
        statement = select(SpecialNominationRow).where(
            SpecialNominationRow.status == SpecialNominationStatus.PENDING.value
        )
        if kind is not None:
            statement = statement.where(SpecialNominationRow.kind == kind.value)
        statement = (
            statement.order_by(
                SpecialNominationRow.nominated_at.desc(),
                SpecialNominationRow.paper_id,
                SpecialNominationRow.kind,
            )
            .offset(offset)
            .limit(limit)
        )
        items = [special_nomination_from_row(row) for row in self._session.scalars(statement).all()]
        count = (
            select(func.count())
            .select_from(SpecialNominationRow)
            .where(SpecialNominationRow.status == SpecialNominationStatus.PENDING.value)
        )
        if kind is not None:
            count = count.where(SpecialNominationRow.kind == kind.value)
        return items, self._session.scalar(count) or 0

    def has_pending(self, paper_id: str) -> bool:
        statement = (
            select(SpecialNominationRow.id)
            .where(
                SpecialNominationRow.paper_id == paper_id,
                SpecialNominationRow.status == SpecialNominationStatus.PENDING.value,
            )
            .limit(1)
        )
        return self._session.scalar(statement) is not None

    def upsert(self, item: SpecialNominationRecord) -> SpecialNominationRecord:
        """按 paper_id+kind 幂等写入；已决策行保持原状。"""
        row = self._session.scalar(
            select(SpecialNominationRow).where(
                SpecialNominationRow.paper_id == item.paper_id,
                SpecialNominationRow.kind == item.kind.value,
            )
        )
        if row is None:
            self._session.add(special_nomination_to_row(item))
            self._flush()
            return item
        existing = special_nomination_from_row(row)
        if existing.status is not SpecialNominationStatus.PENDING:
            return existing
        mapped = special_nomination_to_row(item)
        for field in (
            "confidence",
            "reason",
            "evidence_json",
            "review_layers_json",
            "classic_basis_json",
            "bridge_memo_json",
            "reasons_json",
            "provenance_model",
            "provenance_prompt_version",
            "provenance_generated_at",
            "provenance_input_hash",
            "nominated_at",
        ):
            setattr(row, field, getattr(mapped, field))
        self._flush()
        return special_nomination_from_row(row)

    def decide(
        self,
        nomination_id: str,
        *,
        status: SpecialNominationStatus,
        decided_by: str,
        note: str | None,
        now: datetime,
    ) -> SpecialNominationRecord:
        if status is SpecialNominationStatus.PENDING:
            raise InvariantViolation("special_nomination.status", "决策不能回到待审")
        row = self._session.get(SpecialNominationRow, nomination_id)
        if row is None:
            raise InvariantViolation("special_nomination", "提名不存在")
        if row.status != SpecialNominationStatus.PENDING.value:
            raise InvariantViolation("special_nomination.status", "提名已决策")
        row.status = status.value
        row.decided_at = now
        row.decided_by = decided_by
        row.decision_note = note
        self._flush()
        return special_nomination_from_row(row)


class SpecialTierRepository(_Repository):
    """特殊桶档位追加写历史；读取最新/历史，不提供 update/delete。"""

    def append(self, snapshot: SpecialTierSnapshot) -> SpecialTierSnapshot:
        self._session.add(special_tier_to_row(snapshot))
        self._flush()
        return snapshot

    def latest(
        self,
        paper_id: str,
        *,
        kind: SpecialBucketKind | None = None,
        as_of: datetime | None = None,
    ) -> SpecialTierSnapshot | None:
        statement = select(SpecialTierSnapshotRow).where(
            SpecialTierSnapshotRow.paper_id == paper_id
        )
        if kind is not None:
            statement = statement.where(SpecialTierSnapshotRow.kind == kind.value)
        if as_of is not None:
            statement = statement.where(SpecialTierSnapshotRow.created_at <= as_of)
        statement = statement.order_by(
            SpecialTierSnapshotRow.created_at.desc(), SpecialTierSnapshotRow.id.desc()
        ).limit(1)
        row = self._session.scalar(statement)
        return None if row is None else special_tier_from_row(row)

    def history(self, paper_id: str) -> list[SpecialTierSnapshot]:
        statement = (
            select(SpecialTierSnapshotRow)
            .where(SpecialTierSnapshotRow.paper_id == paper_id)
            .order_by(
                SpecialTierSnapshotRow.created_at,
                SpecialTierSnapshotRow.id,
            )
        )
        return [special_tier_from_row(row) for row in self._session.scalars(statement).all()]

    def latest_all(self, *, as_of: datetime | None = None) -> list[SpecialTierSnapshot]:
        """每篇文献最新一条；用于轮换计划，避免跨桶比较。"""
        statement = select(SpecialTierSnapshotRow)
        if as_of is not None:
            statement = statement.where(SpecialTierSnapshotRow.created_at <= as_of)
        statement = statement.order_by(
            SpecialTierSnapshotRow.paper_id,
            SpecialTierSnapshotRow.created_at.desc(),
            SpecialTierSnapshotRow.id.desc(),
        )
        seen: set[str] = set()
        result: list[SpecialTierSnapshot] = []
        for row in self._session.scalars(statement).all():
            if row.paper_id in seen:
                continue
            seen.add(row.paper_id)
            result.append(special_tier_from_row(row))
        return result


class EventRepository(_Repository):
    """只追加的事件日志。"""

    def append(self, event: Event) -> Event:
        """追加事件；拒绝早于最后一条事件的时间，保持日志单调。"""
        last = self.recent(limit=1)
        if last:
            ensure_event_appended(last, event)
        self._session.add(event_to_row(event))
        self._flush()
        return event

    def get(self, event_id: str) -> Event | None:
        row = self._session.get(EventRow, event_id)
        return None if row is None else event_from_row(row)

    def recent(self, *, limit: int = 100, offset: int = 0) -> list[Event]:
        """按时间倒序返回最近事件。"""
        statement = (
            select(EventRow)
            .order_by(EventRow.created_at.desc(), EventRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [event_from_row(row) for row in self._session.scalars(statement).all()]

    def list_for_paper(self, paper_id: str, *, limit: int = 100) -> list[Event]:
        statement = (
            select(EventRow)
            .where(EventRow.paper_id == paper_id)
            .order_by(EventRow.created_at, EventRow.id)
            .limit(limit)
        )
        return [event_from_row(row) for row in self._session.scalars(statement).all()]


class RatingRepository(_Repository):
    """阅读评分与备注。"""

    def add(self, rating: Rating) -> Rating:
        self._session.add(rating_to_row(rating))
        self._flush()
        return rating

    def list_for_paper(self, paper_id: str) -> list[Rating]:
        statement = (
            select(RatingRow).where(RatingRow.paper_id == paper_id).order_by(RatingRow.created_at)
        )
        return [rating_from_row(row) for row in self._session.scalars(statement).all()]


class ConfigRepository(_Repository):
    """版本化配置；新版本追加写。"""

    def add(self, config: Config) -> Config:
        self._session.add(config_to_row(config))
        self._flush()
        return config

    def get(self, key: str, version: str) -> Config | None:
        row = self._session.get(ConfigRow, (key, version))
        return None if row is None else config_from_row(row)

    def latest(self, key: str) -> Config | None:
        statement = (
            select(ConfigRow)
            .where(ConfigRow.key == key)
            .order_by(ConfigRow.updated_at.desc(), ConfigRow.version.desc())
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else config_from_row(row)

    def versions(self, key: str) -> list[Config]:
        statement = (
            select(ConfigRow)
            .where(ConfigRow.key == key)
            .order_by(ConfigRow.updated_at, ConfigRow.version)
        )
        return [config_from_row(row) for row in self._session.scalars(statement).all()]


class ScoringChangeRepository(_Repository):
    """全局权重变更预览/确认；确认行按 effective_on 激活后续快照配置。"""

    def add(self, change: ScoringChange) -> ScoringChange:
        self._session.add(scoring_change_to_row(change))
        self._flush()
        return change

    def get(self, change_id: str) -> ScoringChange | None:
        row = self._session.get(ScoringChangeRow, change_id)
        return None if row is None else scoring_change_from_row(row)

    def list(self, *, limit: int = 50, offset: int = 0) -> list[ScoringChange]:
        statement = (
            select(ScoringChangeRow)
            .order_by(ScoringChangeRow.created_at.desc(), ScoringChangeRow.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [scoring_change_from_row(row) for row in self._session.scalars(statement).all()]

    def active_for(self, day: date) -> ScoringChange | None:
        statement = (
            select(ScoringChangeRow)
            .where(
                ScoringChangeRow.status == ScoringChangeStatus.CONFIRMED.value,
                ScoringChangeRow.effective_on <= day,
            )
            .order_by(
                ScoringChangeRow.effective_on.desc(),
                ScoringChangeRow.confirmed_at.desc(),
                ScoringChangeRow.id.desc(),
            )
            .limit(1)
        )
        row = self._session.scalar(statement)
        return None if row is None else scoring_change_from_row(row)

    def confirmed_for_version(self, version: str) -> ScoringChange | None:
        statement = (
            select(ScoringChangeRow)
            .where(
                ScoringChangeRow.status == ScoringChangeStatus.CONFIRMED.value,
                ScoringChangeRow.config_version == version,
            )
            .limit(1)
        )
        row = self._session.scalar(statement)
        return None if row is None else scoring_change_from_row(row)

    def confirm(
        self,
        change_id: str,
        *,
        confirmed_by: str,
        note: str | None,
        now: datetime,
    ) -> ScoringChange:
        row = self._session.get(ScoringChangeRow, change_id)
        if row is None:
            raise InvariantViolation("scoring_change", "变更记录不存在")
        if row.status != ScoringChangeStatus.PREVIEWED.value:
            raise InvariantViolation("scoring_change.status", "变更已确认")
        row.status = ScoringChangeStatus.CONFIRMED.value
        row.confirmed_at = now
        row.confirmed_by = confirmed_by
        row.note = note
        self._flush()
        return scoring_change_from_row(row)


class ViewConfigRepository(_Repository):
    """自定义排序视图；名称大小写不敏感唯一，视图不改全局配置。"""

    def get(self, view_id: str) -> ViewConfig | None:
        row = self._session.get(ViewConfigRow, view_id)
        return None if row is None else view_config_from_row(row)

    def find_by_name(self, name: str) -> ViewConfig | None:
        normalized = name.strip().casefold()
        if not normalized:
            return None
        statement = select(ViewConfigRow).where(func.lower(ViewConfigRow.name) == normalized)
        row = self._session.scalar(statement)
        return None if row is None else view_config_from_row(row)

    def list(self) -> list[ViewConfig]:
        statement = select(ViewConfigRow).order_by(func.lower(ViewConfigRow.name), ViewConfigRow.id)
        return [view_config_from_row(row) for row in self._session.scalars(statement).all()]

    def add(self, view: ViewConfig) -> ViewConfig:
        if self.find_by_name(view.name) is not None:
            raise InvariantViolation("view.name", "模板名已存在")
        self._session.add(view_config_to_row(view))
        self._flush()
        return view

    def delete(self, view_id: str) -> bool:
        row = self._session.get(ViewConfigRow, view_id)
        if row is None:
            return False
        self._session.delete(row)
        self._flush()
        return True


class RunRepository(_Repository):
    """运行记录与状态迁移。"""

    def add(self, run: Run) -> Run:
        self._session.add(run_to_row(run))
        self._flush()
        return run

    def get(self, run_id: str) -> Run | None:
        row = self._session.get(RunRow, run_id)
        return None if row is None else run_from_row(row)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Run]:
        statement = (
            select(RunRow).order_by(RunRow.started_at.desc(), RunRow.id).limit(limit).offset(offset)
        )
        return [run_from_row(row) for row in self._session.scalars(statement).all()]

    def transition(
        self, run_id: str, target: RunStatus, *, finished_at: datetime | None = None
    ) -> Run | None:
        row = self._session.get(RunRow, run_id)
        if row is None:
            return None
        updated = transition_run(run_from_row(row), target, finished_at=finished_at)
        row.status = updated.status.value
        row.finished_at = updated.finished_at
        self._flush()
        return updated

    def update_cost(self, run_id: str, cost_json: Mapping[str, Any]) -> Run | None:
        """替换运行的成本 JSON；供 LLM 网关等调用方累计用量。"""
        row = self._session.get(RunRow, run_id)
        if row is None:
            return None
        row.cost_json = dict(cost_json)
        self._flush()
        return run_from_row(row)


class ProfileRepository(_Repository):
    """研究方向档案版本。"""

    def add(self, profile: ResearchProfile) -> ResearchProfile:
        self._session.add(profile_to_row(profile))
        self._flush()
        return profile

    def get(self, profile_id: str, version: str) -> ResearchProfile | None:
        row = self._session.get(ResearchProfileRow, (profile_id, version))
        return None if row is None else profile_from_row(row)

    def latest_confirmed(self, profile_id: str | None = None) -> ResearchProfile | None:
        statement = select(ResearchProfileRow).where(ResearchProfileRow.status == "confirmed")
        if profile_id is not None:
            statement = statement.where(ResearchProfileRow.id == profile_id)
        statement = statement.order_by(ResearchProfileRow.confirmed_at.desc()).limit(1)
        row = self._session.scalars(statement).first()
        return None if row is None else profile_from_row(row)

    def list(self, profile_id: str | None = None) -> list[ResearchProfile]:
        statement = select(ResearchProfileRow).order_by(
            ResearchProfileRow.id, ResearchProfileRow.version
        )
        if profile_id is not None:
            statement = statement.where(ResearchProfileRow.id == profile_id)
        return [profile_from_row(row) for row in self._session.scalars(statement).all()]

    def update_status(
        self,
        profile_id: str,
        version: str,
        target: DocumentStatus,
        *,
        confirmed_at: datetime | None = None,
    ) -> ResearchProfile | None:
        row = self._session.get(ResearchProfileRow, (profile_id, version))
        if row is None:
            return None
        updated = transition_document(profile_from_row(row), target, confirmed_at=confirmed_at)
        row.status = updated.status.value
        row.confirmed_at = updated.confirmed_at
        self._flush()
        return updated


class TaxonomyRepository(_Repository):
    """分类法版本与标签定义。"""

    def add(self, taxonomy: Taxonomy) -> Taxonomy:
        self._session.add(taxonomy_to_row(taxonomy))
        self._flush()
        return taxonomy

    def get(self, taxonomy_id: str, version: str) -> Taxonomy | None:
        row = self._session.get(TaxonomyRow, (taxonomy_id, version))
        return None if row is None else taxonomy_from_row(row)

    def latest_confirmed(self, taxonomy_id: str | None = None) -> Taxonomy | None:
        statement = select(TaxonomyRow).where(TaxonomyRow.status == "confirmed")
        if taxonomy_id is not None:
            statement = statement.where(TaxonomyRow.id == taxonomy_id)
        statement = statement.order_by(TaxonomyRow.confirmed_at.desc()).limit(1)
        row = self._session.scalars(statement).first()
        return None if row is None else taxonomy_from_row(row)

    def list(self, taxonomy_id: str | None = None) -> list[Taxonomy]:
        statement = select(TaxonomyRow).order_by(TaxonomyRow.id, TaxonomyRow.version)
        if taxonomy_id is not None:
            statement = statement.where(TaxonomyRow.id == taxonomy_id)
        return [taxonomy_from_row(row) for row in self._session.scalars(statement).all()]

    def update_status(
        self,
        taxonomy_id: str,
        version: str,
        target: DocumentStatus,
        *,
        confirmed_at: datetime | None = None,
    ) -> Taxonomy | None:
        row = self._session.get(TaxonomyRow, (taxonomy_id, version))
        if row is None:
            return None
        updated = transition_document(taxonomy_from_row(row), target, confirmed_at=confirmed_at)
        row.status = updated.status.value
        row.confirmed_at = updated.confirmed_at
        self._flush()
        return updated


class BucketRepository(_Repository):
    """主题桶/特殊桶目录。"""

    def add(self, bucket: Bucket) -> Bucket:
        self._session.add(bucket_to_row(bucket))
        self._flush()
        return bucket

    def get(self, bucket_id: str) -> Bucket | None:
        row = self._session.get(BucketRow, bucket_id)
        return None if row is None else bucket_from_row(row)

    def list(self, *, active_only: bool = False) -> list[Bucket]:
        statement = select(BucketRow).order_by(BucketRow.id)
        if active_only:
            statement = statement.where(BucketRow.active.is_(True))
        return [bucket_from_row(row) for row in self._session.scalars(statement).all()]

    def set_quota(self, bucket_id: str, quota: int | None) -> Bucket:
        """设置桶配额；``None`` 表示未配置，负数/非整数拒绝。"""
        if quota is not None and (
            isinstance(quota, bool) or not isinstance(quota, int) or quota < 0
        ):
            raise InvariantViolation("bucket.quota", "必须是非负整数或 null")
        row = self._session.get(BucketRow, bucket_id)
        if row is None:
            raise InvariantViolation("bucket.id", f"桶 {bucket_id} 不存在")
        row.quota = quota
        self._flush()
        return bucket_from_row(row)


class ArchiveSnapshotRepository(_Repository):
    """只追加的归档快照；不提供 update/delete。"""

    def add(self, snapshot: ArchiveSnapshot) -> ArchiveSnapshot:
        ensure_active(self._session, action="archive")
        self._session.add(archive_snapshot_to_row(snapshot))
        self._flush()
        return snapshot

    def get(self, paper_id: str) -> ArchiveSnapshot | None:
        row = self._session.get(ArchiveSnapshotRow, paper_id)
        return None if row is None else archive_snapshot_from_row(row)


__all__ = [
    "ArchiveSnapshotRepository",
    "BucketRepository",
    "ConfigRepository",
    "EnrichmentRepository",
    "EventRepository",
    "PaperBucketRepository",
    "PaperRepository",
    "PaperTagRepository",
    "ProfileRepository",
    "RatingRepository",
    "ReviewRepository",
    "RunRepository",
    "ScoreSnapshotRepository",
    "StaticScoreSnapshotRepository",
    "SummaryRepository",
    "TaxonomyRepository",
]
