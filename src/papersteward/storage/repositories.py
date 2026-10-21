"""P1 仓储：领域实体与 SQLAlchemy 行之间的边界。

约定：

- 所有方法只 ``flush``，不 ``commit``；事务由调用方 ``session_scope`` 控制；
- 事件、评分快照、归档快照只提供追加/读取接口，不提供 update/delete；
- 状态迁移经领域状态机校验后再写库，并同步追加事件；
- DB 约束与 ``core.invariants`` 形成双重校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papersteward.core.entities import (
    ArchiveSnapshot,
    Bucket,
    Config,
    Event,
    Paper,
    PaperBucket,
    PaperTag,
    Rating,
    ResearchProfile,
    Run,
    ScoreSnapshot,
    Summary,
    Taxonomy,
    utc_now,
)
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.core.invariants import ensure_score_snapshot_append_only
from papersteward.core.state_machine import (
    PAPER_STATUS_EVENTS,
    transition_paper,
    transition_run,
)
from papersteward.storage.mappers import (
    archive_snapshot_from_row,
    archive_snapshot_to_row,
    bucket_from_row,
    bucket_to_row,
    config_from_row,
    config_to_row,
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
    run_from_row,
    run_to_row,
    score_snapshot_from_row,
    score_snapshot_to_row,
    summary_from_row,
    summary_to_row,
    taxonomy_from_row,
    taxonomy_to_row,
)
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    BucketRow,
    ConfigRow,
    EventRow,
    PaperBucketRow,
    PaperRow,
    PaperTagRow,
    RatingRow,
    ResearchProfileRow,
    RunRow,
    ScoreSnapshotRow,
    SummaryRow,
    TaxonomyRow,
)


class _Repository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _flush(self) -> None:
        self._session.flush()


class PaperRepository(_Repository):
    """文献主记录与状态迁移。"""

    def add(self, paper: Paper) -> Paper:
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
        row.status = updated.status.value
        event = Event(
            id=event_id,
            event_type=PAPER_STATUS_EVENTS[target],
            created_at=occurred_at or utc_now(),
            paper_id=paper_id,
            payload_json=dict(payload or {}),
        )
        self._session.add(event_to_row(event))
        self._flush()
        return updated


class PaperTagRepository(_Repository):
    """文献标签分配。"""

    def add(self, tag: PaperTag) -> PaperTag:
        self._session.add(paper_tag_to_row(tag))
        self._flush()
        return tag

    def add_many(self, tags: Sequence[PaperTag]) -> tuple[PaperTag, ...]:
        self._session.add_all(paper_tag_to_row(tag) for tag in tags)
        self._flush()
        return tuple(tags)

    def list_for_paper(self, paper_id: str) -> list[PaperTag]:
        statement = (
            select(PaperTagRow)
            .where(PaperTagRow.paper_id == paper_id)
            .order_by(PaperTagRow.role, PaperTagRow.tag_code)
        )
        return [paper_tag_from_row(row) for row in self._session.scalars(statement).all()]


class PaperBucketRepository(_Repository):
    """文献与主题桶/特殊桶的关系。"""

    def add(self, bucket: PaperBucket) -> PaperBucket:
        self._session.add(paper_bucket_to_row(bucket))
        self._flush()
        return bucket

    def add_many(self, buckets: Sequence[PaperBucket]) -> tuple[PaperBucket, ...]:
        self._session.add_all(paper_bucket_to_row(bucket) for bucket in buckets)
        self._flush()
        return tuple(buckets)

    def list_for_paper(self, paper_id: str) -> list[PaperBucket]:
        statement = (
            select(PaperBucketRow)
            .where(PaperBucketRow.paper_id == paper_id)
            .order_by(PaperBucketRow.role, PaperBucketRow.bucket_id)
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


class EventRepository(_Repository):
    """只追加的事件日志。"""

    def append(self, event: Event) -> Event:
        self._session.add(event_to_row(event))
        self._flush()
        return event

    def get(self, event_id: str) -> Event | None:
        row = self._session.get(EventRow, event_id)
        return None if row is None else event_from_row(row)

    def recent(self, *, limit: int = 100, offset: int = 0) -> list[Event]:
        statement = (
            select(EventRow).order_by(EventRow.created_at, EventRow.id).limit(limit).offset(offset)
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


class ArchiveSnapshotRepository(_Repository):
    """只追加的归档快照；不提供 update/delete。"""

    def add(self, snapshot: ArchiveSnapshot) -> ArchiveSnapshot:
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
    "EventRepository",
    "PaperBucketRepository",
    "PaperRepository",
    "PaperTagRepository",
    "ProfileRepository",
    "RatingRepository",
    "RunRepository",
    "ScoreSnapshotRepository",
    "SummaryRepository",
    "TaxonomyRepository",
]
