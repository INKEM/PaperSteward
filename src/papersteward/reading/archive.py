"""阅读完成时冻结全文上下文；修订留在Rating/事件，不覆盖归档。"""

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import ArchiveImportInput, ArchiveSnapshot, Event, PaperTag
from papersteward.core.enums import AssignedBy, BucketRole, EventType, PaperStatus, TagRole
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import ArchiveRecord
from papersteward.errors import NotFoundError
from papersteward.pipeline.context import bound_context
from papersteward.scoring.contracts import ScoreExplanation
from papersteward.scoring.queue import explain_score
from papersteward.storage.delivery import content_hash, read_digest
from papersteward.storage.models import ArchiveSnapshotRow, DigestRow
from papersteward.storage.onboarding import ensure_active
from papersteward.storage.repositories import (
    ArchiveSnapshotRepository,
    BucketRepository,
    ConfigRepository,
    EnrichmentRepository,
    EventRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    RatingRepository,
    SummaryRepository,
)


def archive_done(session: Session, paper_id: str, *, now: datetime) -> None:
    ensure_active(session, action="archive")
    paper = PaperRepository(session).get(paper_id)
    if paper is None or paper.status is not PaperStatus.DONE:
        raise InvariantViolation("archive.status", "归档唯一入口为done")
    if ArchiveSnapshotRepository(session).get(paper_id) is not None:
        raise InvariantViolation("archive", "归档已存在且不可覆盖")
    ratings = RatingRepository(session).list_for_paper(paper_id)
    if not ratings:
        raise InvariantViolation("archive.rating", "完成记录缺少阅读反馈")
    rating = ratings[0]
    tags = PaperTagRepository(session).list_for_paper(paper_id)
    primary = [t for t in tags if t.role is TagRole.PRIMARY]
    buckets = PaperBucketRepository(session).list_for_paper(paper_id)
    filing = [b for b in buckets if b.role is BucketRole.FILING]
    summary = SummaryRepository(session).get(paper_id)
    score = explain_score(session, paper_id, now=now)
    if (
        len(primary) != 1
        or len(filing) != 1
        or summary is None
        or not isinstance(score, ScoreExplanation)
    ):
        raise InvariantViolation("archive.context", "缺少唯一标签/归属/摘要/主题评分快照")
    if score.primary_tag != primary[0].tag_code:
        raise InvariantViolation("archive.context", "当前主标签与评分快照不匹配")
    enrichment = EnrichmentRepository(session).get(paper_id)
    suggestion = None
    runtime = ConfigRepository(session).get("onboarding_runtime", score.binding["runtime_version"])
    assert runtime is not None
    local_day = now.astimezone(ZoneInfo(runtime.value_json["timezone"])).date()
    for row in session.scalars(select(DigestRow).order_by(DigestRow.digest_date.desc())):
        if row.digest_date <= local_day:
            suggestion = next(
                (i for i in read_digest(session, row.digest_date).items if i.paper_id == paper_id),
                None,
            )
            if suggestion is not None:
                break
    content: dict[str, Any] = jsonable_encoder(
        {
            "metadata": {
                "paper_id": paper_id,
                "title": paper.title,
                "authors": list(paper.authors),
                "venue": paper.venue,
                "published_at": paper.published_at,
                "doi": paper.doi,
                "url": paper.url,
                "pdf_url": paper.pdf_url,
                "code_url": None if enrichment is None else enrichment.signals.code_url,
            },
            "tags": [asdict(t) for t in tags],
            "buckets": [
                {
                    **asdict(b),
                    "definition": asdict(bucket)
                    if (bucket := BucketRepository(session).get(b.bucket_id))
                    else None,
                }
                for b in buckets
            ],
            "summary": asdict(summary),
            "score": score.model_dump(mode="json"),
            "reading": {
                "completed_at": rating.created_at,
                "actual_mode": rating.read_mode,
                "suggested_mode": None if suggestion is None else suggestion.read_mode,
                "reason": None if suggestion is None else suggestion.reason,
                "overridden": suggestion is not None and suggestion.read_mode != rating.read_mode,
            },
            "initial_feedback": {"score": rating.score, "note": rating.note},
        }
    )
    ArchiveSnapshotRepository(session).add(
        ArchiveSnapshot(
            paper_id=paper_id,
            archived_at=now,
            primary_tag=primary[0].tag_code,
            secondary_tags=tuple(t.tag_code for t in tags if t.role is TagRole.SECONDARY),
            filing_bucket_id=filing[0].bucket_id,
            profile_version=score.profile_version,
            taxonomy_version=score.taxonomy_version,
            config_version=score.config_version,
            content_hash=content_hash(content),
            content_json=content,
            rating=rating.score,
            note=rating.note,
            score_date=score.score_date,
            priority=score.scores["P"],
        )
    )
    if suggestion is not None and suggestion.read_mode != rating.read_mode:
        EventRepository(session).append(
            Event(
                uuid4().hex,
                EventType.PAPER_READ_MODE_OVERRIDDEN,
                now,
                paper_id,
                {
                    "suggested": suggestion.read_mode.value,
                    "actual": rating.read_mode.value if rating.read_mode else None,
                },
            )
        )
    PaperRepository(session).transition(
        paper_id,
        PaperStatus.ARCHIVED,
        event_id=uuid4().hex,
        occurred_at=now,
        payload={"content_hash": content_hash(content)},
    )


def archive_imported(
    session: Session, paper_id: str, *, now: datetime, import_input: ArchiveImportInput
) -> None:
    """导入外部已读论文的归档；与 ``archive_done`` 并列，不放宽其任何不变量。

    前置：论文已存在且为 DONE、评分已先写入且必含 1–5 分、标题非空、无既有快照。
    三个版本列取自 ``bound_context``（与 ScoreExplanation.binding 同源）；
    未选主标签时不写标签行，快照 primary_tag 为空。
    """
    paper = PaperRepository(session).get(paper_id)
    if paper is None or paper.status is not PaperStatus.DONE:
        raise InvariantViolation("archive.status", "导入归档仅接受已读完的论文")
    if ArchiveSnapshotRepository(session).get(paper_id) is not None:
        raise InvariantViolation("archive", "归档已存在且不可覆盖")
    if not paper.title.strip():
        raise InvariantViolation("archive.title", "导入论文标题不能为空")
    ratings = RatingRepository(session).list_for_paper(paper_id)
    if not ratings or ratings[0].score is None:
        raise InvariantViolation("archive.rating", "导入必须附 1–5 的阅读评分")
    rating = ratings[0]
    _, binding = bound_context(session, now, action="archive")
    tags = PaperTagRepository(session).list_for_paper(paper_id)
    if import_input.primary_tag is not None:
        PaperTagRepository(session).add(
            PaperTag(
                paper_id,
                import_input.primary_tag,
                TagRole.PRIMARY,
                binding["taxonomy_version"],
                assigned_by=AssignedBy.USER,
            )
        )
        tags = PaperTagRepository(session).list_for_paper(paper_id)
    content: dict[str, Any] = jsonable_encoder(
        {
            "origin": "import",
            "identifier_input": import_input.identifier_input,
            "resolve": dict(import_input.resolve) if import_input.resolve else None,
            "metadata": {
                "paper_id": paper_id,
                "title": paper.title,
                "authors": list(paper.authors),
                "venue": paper.venue,
                "published_at": paper.published_at,
                "abstract": paper.abstract,
                "doi": paper.doi,
                "arxiv_id": paper.arxiv_id,
                "url": paper.url,
                "pdf_url": paper.pdf_url,
            },
            "tags": [asdict(t) for t in tags],
            "initial_feedback": {"score": rating.score, "note": rating.note},
        }
    )
    ArchiveSnapshotRepository(session).add(
        ArchiveSnapshot(
            paper_id=paper_id,
            archived_at=now,
            primary_tag=import_input.primary_tag,
            secondary_tags=(),
            filing_bucket_id=None,
            profile_version=binding["profile_version"],
            taxonomy_version=binding["taxonomy_version"],
            config_version=binding["scoring_version"],
            content_hash=content_hash(content),
            content_json=content,
            rating=rating.score,
            note=rating.note,
        )
    )
    PaperRepository(session).transition(
        paper_id,
        PaperStatus.ARCHIVED,
        event_id=uuid4().hex,
        occurred_at=now,
        payload={"content_hash": content_hash(content), "origin": "import"},
    )
    # 补发完成事件供标签补给饥饿度计入；归档事件由上面的迁移自动发出。
    EventRepository(session).append(
        Event(uuid4().hex, EventType.PAPER_DONE, now, paper_id, {"origin": "import"})
    )


def verified_archive_content(
    content_json: Mapping[str, Any] | None, expected_hash: str
) -> tuple[dict[str, Any], bool]:
    """校验归档内容哈希，返回 ``(内容, 是否可用)``。

    空内容视为不可用且跳过校验；哈希不匹配抛 ``InvariantViolation``，
    由调用方决定拒绝读取（``read_archive``）还是降级为缺失（分析投影）。
    """
    content = dict(content_json or {})
    if content and content_hash(content) != expected_hash:
        raise InvariantViolation("archive.hash", "归档内容校验失败")
    return content, bool(content)


def read_archive(session: Session, paper_id: str) -> ArchiveRecord:
    ensure_active(session, action="archive.read")
    snapshot = ArchiveSnapshotRepository(session).get(paper_id)
    if snapshot is None:
        raise NotFoundError("归档不存在")
    content, content_available = verified_archive_content(
        snapshot.content_json, snapshot.content_hash
    )
    ratings = RatingRepository(session).list_for_paper(paper_id)
    latest = ratings[-1] if ratings else None
    return ArchiveRecord(
        paper_id=paper_id,
        archived_at=snapshot.archived_at,
        primary_tag=snapshot.primary_tag,
        secondary_tags=list(snapshot.secondary_tags),
        filing_bucket_id=snapshot.filing_bucket_id,
        content_hash=snapshot.content_hash,
        content=content,
        content_available=content_available,
        initial_score=snapshot.rating,
        initial_note=snapshot.note,
        score=snapshot.rating if latest is None else latest.score,
        note=snapshot.note if latest is None else latest.note,
        revision_history=[
            {"created_at": r.created_at.isoformat(), "score": r.score, "note": r.note}
            for r in ratings
        ],
    )


def list_archives(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    tag: str | None = None,
    min_score: int | None = None,
    query: str | None = None,
    bucket: str | None = None,
) -> dict[str, Any]:
    ensure_active(session, action="archive.list")
    if not 1 <= limit <= 200 or offset < 0 or (min_score is not None and not 1 <= min_score <= 5):
        raise InvariantViolation("archive.filter", "分页或评分筛选参数非法")
    items = []
    for key in session.scalars(
        select(ArchiveSnapshotRow.paper_id).order_by(
            ArchiveSnapshotRow.archived_at.desc(), ArchiveSnapshotRow.paper_id
        )
    ):
        record = read_archive(session, key)
        if tag and tag not in [record.primary_tag, *record.secondary_tags]:
            continue
        if min_score is not None and (record.score is None or record.score < min_score):
            continue
        if bucket and bucket != record.filing_bucket_id:
            continue
        searchable = (
            str(record.content.get("metadata", {}))
            + str([record.primary_tag, *record.secondary_tags])
            + (record.note or "")
        )
        if query and query.casefold() not in searchable.casefold():
            continue
        items.append(record)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "limit": limit,
        "offset": offset,
    }
