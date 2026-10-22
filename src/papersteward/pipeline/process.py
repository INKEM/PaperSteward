"""P4 离线处理编排：enrichment → R → 摘要 → 分类 → 特殊桶 → 静态分 → 审核队列。

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
    ReviewItem,
    StaticScoreSnapshot,
    Taxonomy,
)
from papersteward.core.enums import BucketType, PaperStatus, TagLevel
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
from papersteward.pipeline.review import open_review
from papersteward.pipeline.special import SpecialNominationResult, nominate_special
from papersteward.pipeline.summarize import (
    SummaryError,
    SummaryResult,
    generate_summary,
    persist_summary,
)
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
    PaperRepository,
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
    needs_review: bool
    reasons: tuple[str, ...]
    relevance: RelevanceResult
    summary: SummaryResult | None
    classification: ClassificationResult
    special: SpecialNominationResult
    static: StaticScoreSnapshot
    review: ReviewItem | None

    def as_json(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "status": self.status.value,
            "needs_review": self.needs_review,
            "reasons": list(self.reasons),
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
            "review": (
                None
                if self.review is None
                else {
                    "status": self.review.status.value,
                    "reasons": list(self.review.reasons),
                }
            ),
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


def _move_to_review(session: Session, paper: Paper, *, now: datetime) -> PaperStatus:
    if paper.status is PaperStatus.NEEDS_REVIEW:
        return PaperStatus.NEEDS_REVIEW
    if paper.status not in (
        PaperStatus.SCREENED,
        PaperStatus.ENRICHED,
        PaperStatus.SUMMARIZED,
        PaperStatus.CLASSIFIED,
    ):
        raise ConflictError("论文当前状态不能进入 needs_review")
    if (
        PaperRepository(session).transition(
            paper.id,
            PaperStatus.NEEDS_REVIEW,
            event_id=uuid4().hex,
            occurred_at=now,
            payload={"stage": "process"},
        )
        is None
    ):
        raise InvariantViolation("process.paper", "文献在状态迁移时消失")
    return PaperStatus.NEEDS_REVIEW


def _review_reasons(
    relevance: RelevanceResult,
    summary: SummaryResult | None,
    summary_reason: str | None,
    classification: ClassificationResult,
    special: SpecialNominationResult,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if relevance.needs_review:
        reasons.extend(f"relevance:{reason}" for reason in relevance.reasons)
    if summary_reason is not None:
        reasons.append(summary_reason)
    if summary is not None and summary.needs_review:
        reasons.extend(f"summary:{reason}" for reason in summary.reasons)
    if classification.needs_review:
        reasons.extend(f"classification:{reason}" for reason in classification.reasons)
    for nomination in special.nominations:
        reasons.append(f"special:nomination:{nomination.kind.value}")
    reasons.extend(f"special:{reason}" for reason in special.reasons)
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
    reasons = _review_reasons(relevance, summary, summary_reason, classification, special)
    needs_review = bool(reasons)
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
    if needs_review:
        review = open_review(session, paper.id, reasons, payload, now=now)
        status = _move_to_review(session, paper, now=now)
    else:
        review = None
        status = _move_to_classified(session, paper, now=now)
    return ProcessOutcome(
        paper_id=paper.id,
        status=status,
        needs_review=needs_review,
        reasons=reasons,
        relevance=relevance,
        summary=summary,
        classification=classification,
        special=special,
        static=static,
        review=review,
    )


__all__ = ["ProcessOutcome", "ensure_topic_buckets", "process_paper"]
