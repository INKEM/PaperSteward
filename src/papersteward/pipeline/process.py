"""离线处理编排：enrichment → R → 摘要 → 分类 → 特殊桶提名落库 → 静态分 → 阅读队列。

只处理已 screened 的文献；所有写入在同一事务内由调用方提交。不调用真实网络；
LLM 由显式注入的 gateway 提供。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.core.entities import (
    Bucket,
    Paper,
    ResearchProfile,
    StaticScoreSnapshot,
    Taxonomy,
)
from papersteward.core.enums import (
    BucketType,
    PaperStatus,
    ReviewResolution,
    ReviewStatus,
    TagLevel,
)
from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError
from papersteward.llm import LLMGateway
from papersteward.pipeline.classify import (
    ClassificationResult,
    classify_paper,
    persist_classification,
)
from papersteward.pipeline.embeddings import EmbeddingProvider
from papersteward.pipeline.enrich_service import persist_enrichment
from papersteward.pipeline.relevance import RelevanceResult, evaluate_relevance
from papersteward.pipeline.shelf import auto_decide_special, persist_nominations
from papersteward.pipeline.special import SpecialNominationResult, nominate_special
from papersteward.pipeline.summarize import (
    SummaryError,
    SummaryResult,
    generate_summary,
    persist_summary,
)
from papersteward.scoring.activation import append_future_activation_snapshots
from papersteward.scoring.config import ScoringConfig
from papersteward.scoring.static import (
    AuthorityInputs,
    ExtraValueInputs,
    StaticScoreError,
    build_static_snapshot,
    compute_authority,
    compute_extra_value,
    compute_static_score,
    persist_static_snapshot,
)
from papersteward.storage.repositories import (
    BucketRepository,
    EnrichmentRepository,
    PaperAnalysisRepository,
    PaperRepository,
    ReviewRepository,
    StaticScoreSnapshotRepository,
)

_ALLOWED_INPUT_STATUSES = (
    PaperStatus.SCREENED,
    PaperStatus.ENRICHED,
    PaperStatus.SUMMARIZED,
)


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    paper_id: str
    status: PaperStatus
    quality_flags: tuple[str, ...]
    auto_shelved: bool
    relevance: RelevanceResult
    summary: SummaryResult | None
    classification: ClassificationResult
    special: SpecialNominationResult
    static: StaticScoreSnapshot

    def as_json(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "status": self.status.value,
            "quality_flags": list(self.quality_flags),
            "auto_shelved": self.auto_shelved,
            "relevance": self.relevance.as_json(),
            "summary": self.summary.as_json() if self.summary is not None else None,
            "classification": self.classification.as_json(),
            "special": self.special.as_json(),
            "static": {
                "score_date": self.static.score_date.isoformat(),
                "relevance": self.static.relevance,
                "authority": self.static.authority,
                "extra_value": self.static.extra_value,
                "static_score": self.static.static_score,
                "confidence": self.static.confidence,
                "config_version": self.static.config_version,
                "profile_version": self.static.profile_version,
                "taxonomy_version": self.static.taxonomy_version,
                "input_hash": self.static.input_hash,
                "breakdown": dict(self.static.breakdown_json),
            },
        }


def _move_to_classified(session: Session, paper: Paper, *, now: datetime) -> PaperStatus:
    repository = PaperRepository(session)
    path = {
        PaperStatus.SCREENED: (
            PaperStatus.ENRICHED,
            PaperStatus.SUMMARIZED,
            PaperStatus.CLASSIFIED,
        ),
        PaperStatus.ENRICHED: (PaperStatus.SUMMARIZED, PaperStatus.CLASSIFIED),
        PaperStatus.SUMMARIZED: (PaperStatus.CLASSIFIED,),
        PaperStatus.NEEDS_REVIEW: (PaperStatus.CLASSIFIED,),
        PaperStatus.CLASSIFIED: (),
    }.get(paper.status)
    if path is None:
        raise ConflictError("论文当前状态不能进入 classified")
    for target in path:
        if (
            repository.transition(
                paper.id,
                target,
                event_id=uuid4().hex,
                occurred_at=now,
                payload={"stage": "process", "target": target.value},
            )
            is None
        ):
            raise InvariantViolation("process.paper", "文献在状态迁移时消失")
    return PaperStatus.CLASSIFIED


def _quality_flags(
    relevance: RelevanceResult,
    summary: SummaryResult | None,
    summary_reason: str | None,
    classification: ClassificationResult,
) -> tuple[str, ...]:
    """收集质量标记，不阻止文献进入阅读队列。"""
    reasons: list[str] = []
    if relevance.needs_review:
        reasons.extend(f"relevance:{reason}" for reason in relevance.reasons)
    if summary_reason is not None:
        reasons.append(summary_reason)
    if summary is not None and summary.needs_review:
        reasons.extend(f"summary:{reason}" for reason in summary.reasons)
    if classification.needs_review:
        reasons.extend(f"classification:{reason}" for reason in classification.reasons)
    return tuple(dict.fromkeys(reasons))


def _static_snapshot(
    session: Session,
    paper: Paper,
    relevance: RelevanceResult,
    *,
    profile: ResearchProfile,
    taxonomy: Taxonomy,
    config: ScoringConfig,
    now: datetime,
    score_date: date,
    author_scores: tuple[float, ...] = (),
    citation_percentile: float | None = None,
    reproducibility: float | None = None,
    rigor: float | None = None,
) -> StaticScoreSnapshot:
    enrichment = EnrichmentRepository(session).get(paper.id)
    if enrichment is None:
        enrichment, _ = persist_enrichment(session, paper, now=lambda: now)
    signals = enrichment.signals
    authority = compute_authority(
        AuthorityInputs(
            venue_level=signals.venue_level,
            author_scores=author_scores,
            citation_percentile=citation_percentile,
            published_at=paper.published_at,
            as_of=score_date,
        )
    )
    extra = compute_extra_value(
        ExtraValueInputs(
            code=1.0 if signals.code_url else None,
            data=1.0 if signals.data_url else None,
            reproducibility=reproducibility,
            rigor=rigor,
            oa_status=signals.oa_status,
        )
    )
    result = compute_static_score(relevance.R, authority, extra, config)
    versions = (config.version, profile.version, taxonomy.version)
    repository = StaticScoreSnapshotRepository(session)
    latest = repository.latest(paper.id)
    if (
        latest is not None
        and (
            latest.config_version,
            latest.profile_version,
            latest.taxonomy_version,
        )
        == versions
    ):
        return latest
    if latest is not None and score_date <= latest.score_date:
        raise StaticScoreError("static.score_date", "版本变化后需要晚于已有静态快照的新日期")
    snapshot = build_static_snapshot(
        result,
        paper,
        score_date=score_date,
        config_version=config.version,
        profile_version=profile.version,
        taxonomy_version=taxonomy.version,
        created_at=now,
    )
    return persist_static_snapshot(session, snapshot)


def ensure_topic_buckets(session: Session, taxonomy: Taxonomy) -> dict[str, Bucket]:
    """从已确认分类法补齐激活主题桶；已存在的桶保持不变。"""
    repository = BucketRepository(session)
    existing = {
        bucket.tag_code: bucket
        for bucket in repository.list()
        if bucket.bucket_type is BucketType.TOPIC and bucket.tag_code
    }
    for tag in taxonomy.tags:
        if tag.level is not TagLevel.PRIMARY or not tag.active or tag.code in existing:
            continue
        bucket_id = f"b-{tag.code.casefold()}"
        collision = repository.get(bucket_id)
        if collision is not None:
            raise InvariantViolation("process.bucket", f"主题桶 ID {bucket_id} 已被其他桶占用")
        bucket = Bucket(
            id=bucket_id,
            name=tag.name,
            bucket_type=BucketType.TOPIC,
            tag_code=tag.code,
        )
        repository.add(bucket)
        existing[tag.code] = bucket
    return existing


def process_paper(
    session: Session,
    paper: Paper,
    *,
    profile: ResearchProfile,
    taxonomy: Taxonomy,
    topic_buckets: Mapping[str, Bucket],
    config: ScoringConfig,
    gateway: LLMGateway,
    embedding_provider: EmbeddingProvider,
    now: datetime,
    score_date: date | None = None,
    author_scores: tuple[float, ...] = (),
    citation_percentile: float | None = None,
    reproducibility: float | None = None,
    rigor: float | None = None,
) -> ProcessOutcome:
    """处理一篇 screened 文献；P4 不计算动态分/P。"""
    if paper.status not in _ALLOWED_INPUT_STATUSES:
        raise ConflictError("只处理 screened/enriched/summarized 文献")
    relevance = evaluate_relevance(profile, paper, gateway, embedding_provider)
    summary: SummaryResult | None = None
    summary_reason: str | None = None
    if not paper.abstract:
        summary_reason = "summary:missing_abstract"
    else:
        try:
            summary = generate_summary(paper, gateway)
            persist_summary(session, summary)
        except SummaryError as exc:
            summary_reason = (
                "summary:missing_abstract"
                if exc.field == "summary.abstract"
                else "summary:unavailable"
            )
    classification = classify_paper(paper, taxonomy, topic_buckets, gateway)
    persist_classification(session, classification)
    special = nominate_special(paper, profile, gateway)
    persist_nominations(session, special, now=now)
    static = _static_snapshot(
        session,
        paper,
        relevance,
        profile=profile,
        taxonomy=taxonomy,
        config=config,
        now=now,
        score_date=score_date or now.date(),
        author_scores=author_scores,
        citation_percentile=citation_percentile,
        reproducibility=reproducibility,
        rigor=rigor,
    )
    append_future_activation_snapshots(
        session,
        paper_id=paper.id,
        relevance=static.relevance,
        authority=static.authority,
        extra_value=static.extra_value,
        confidence=static.confidence,
        profile_version=static.profile_version,
        taxonomy_version=static.taxonomy_version,
        base_input_hash=static.input_hash,
        breakdown=static.breakdown_json,
        after_day=static.score_date,
        now=now,
    )
    reasons = _quality_flags(relevance, summary, summary_reason, classification)
    payload = {
        "taxonomy_id": taxonomy.id,
        "taxonomy_version": taxonomy.version,
        "profile_id": profile.id,
        "profile_version": profile.version,
        "config_version": config.version,
        "relevance": relevance.as_json(),
        "summary": summary.as_json() if summary is not None else None,
        "summary_error": summary_reason,
        "classification": classification.as_json(),
        "special": special.as_json(),
        "static": {
            "static_score": static.static_score,
            "confidence": static.confidence,
            "input_hash": static.input_hash,
        },
    }
    reasons += tuple(f"special:{reason}" for reason in special.reasons)
    PaperAnalysisRepository(session).save(paper.id, payload, reasons, now=now)
    status = _move_to_classified(session, paper, now=now)
    auto_shelved = auto_decide_special(session, paper.id, now=now)
    if auto_shelved:
        status = PaperStatus.SHELVED
    return ProcessOutcome(
        paper_id=paper.id,
        status=status,
        quality_flags=reasons,
        auto_shelved=auto_shelved,
        relevance=relevance,
        summary=summary,
        classification=classification,
        special=special,
        static=static,
    )


def reconcile_legacy_reviews(session: Session, *, now: datetime) -> int:
    """无模型调用地迁移旧审核数据，保留标签、快照与审计记录。"""
    from sqlalchemy import select

    from papersteward.storage.mappers import as_utc
    from papersteward.storage.models import PaperRow, ReviewItemRow

    analyses = PaperAnalysisRepository(session)
    reviews = ReviewRepository(session)
    for row in session.scalars(select(ReviewItemRow)):
        if analyses.get(row.paper_id) is None:
            analyses.save(
                row.paper_id,
                row.payload_json,
                row.reasons_json,
                now=max(now, as_utc(row.updated_at)),
            )
        if row.status == ReviewStatus.OPEN.value:
            reviews.resolve(
                row.paper_id,
                resolved_by="automatic-migration",
                resolution=ReviewResolution.APPROVED,
                note="移除人工审核关卡；保留原始分析与质量标记",
                now=now,
            )
    papers = list(
        session.scalars(select(PaperRow).where(PaperRow.status == PaperStatus.NEEDS_REVIEW.value))
    )
    for paper_row in papers:
        paper = PaperRepository(session).get(paper_row.id)
        assert paper is not None
        _move_to_classified(session, paper, now=now)
        auto_decide_special(session, paper.id, now=now)
    return len(papers)


__all__ = ["ProcessOutcome", "ensure_topic_buckets", "process_paper", "reconcile_legacy_reviews"]
