"""ORM 行与不可变领域实体之间的映射。

SQLite 读取的 datetime 是 naive；这里统一补成 UTC。枚举在 DB 中以字符串存储，
读写边界集中转换，避免仓储与 API 各自处理。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from papersteward.core.entities import (
    ArchiveSnapshot,
    Bucket,
    Config,
    Enrichment,
    EnrichmentSignals,
    Event,
    NominationEvidence,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    Rating,
    ResearchProfile,
    ReviewItem,
    Run,
    ScoreSnapshot,
    SpecialNominationRecord,
    SpecialTierSnapshot,
    StaticScoreSnapshot,
    Summary,
    Tag,
    Taxonomy,
    ViewConfig,
)
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    DocumentStatus,
    EventType,
    OaStatus,
    PaperStatus,
    ReadMode,
    ReviewResolution,
    ReviewStatus,
    RunStatus,
    ScoreTier,
    SpecialBucketKind,
    SpecialNominationStatus,
    SummaryValidationStatus,
    TagLevel,
    TagRole,
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
    SpecialNominationRow,
    SpecialTierSnapshotRow,
    StaticScoreSnapshotRow,
    SummaryRow,
    TagRow,
    TaxonomyRow,
    ViewConfigRow,
)


def as_utc(value: datetime) -> datetime:
    """把 SQLite 的 naive datetime 解释为 UTC，并统一时区。"""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value)


def _provenance_columns(provenance: Provenance | None) -> dict[str, Any]:
    if provenance is None:
        return {
            "provenance_model": None,
            "provenance_prompt_version": None,
            "provenance_generated_at": None,
            "provenance_input_hash": None,
        }
    return {
        "provenance_model": provenance.model,
        "provenance_prompt_version": provenance.prompt_version,
        "provenance_generated_at": provenance.generated_at,
        "provenance_input_hash": provenance.input_hash,
    }


def _provenance_from_columns(
    model: str | None,
    prompt_version: str | None,
    generated_at: datetime | None,
    input_hash: str | None,
) -> Provenance | None:
    if model is None:
        return None
    if prompt_version is None or generated_at is None or input_hash is None:
        raise ValueError("provenance columns must be all null or all non-null")
    return Provenance(
        model=model,
        prompt_version=prompt_version,
        generated_at=as_utc(generated_at),
        input_hash=input_hash,
    )


def paper_to_row(paper: Paper) -> PaperRow:
    return PaperRow(
        id=paper.id,
        title=paper.title,
        status=paper.status.value,
        authors_json=list(paper.authors),
        venue=paper.venue,
        published_at=paper.published_at,
        first_seen_at=paper.first_seen_at,
        abstract=paper.abstract,
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        url=paper.url,
        pdf_url=paper.pdf_url,
        oa_status=paper.oa_status.value,
        canonical_id=paper.canonical_id,
    )


def paper_from_row(row: PaperRow) -> Paper:
    return Paper(
        id=row.id,
        title=row.title,
        status=PaperStatus(row.status),
        authors=tuple(row.authors_json or ()),
        venue=row.venue,
        published_at=optional_utc(row.published_at),
        first_seen_at=as_utc(row.first_seen_at),
        abstract=row.abstract,
        doi=row.doi,
        arxiv_id=row.arxiv_id,
        url=row.url,
        pdf_url=row.pdf_url,
        oa_status=OaStatus(row.oa_status),
        canonical_id=row.canonical_id,
    )


def paper_tag_to_row(tag: PaperTag) -> PaperTagRow:
    return PaperTagRow(
        paper_id=tag.paper_id,
        tag_code=tag.tag_code,
        role=tag.role.value,
        taxonomy_version=tag.taxonomy_version,
        confidence=tag.confidence,
        assigned_by=tag.assigned_by.value,
        **_provenance_columns(tag.provenance),
    )


def paper_tag_from_row(row: PaperTagRow) -> PaperTag:
    return PaperTag(
        paper_id=row.paper_id,
        tag_code=row.tag_code,
        role=TagRole(row.role),
        taxonomy_version=row.taxonomy_version,
        confidence=row.confidence,
        assigned_by=AssignedBy(row.assigned_by),
        provenance=_provenance_from_columns(
            row.provenance_model,
            row.provenance_prompt_version,
            row.provenance_generated_at,
            row.provenance_input_hash,
        ),
    )


def paper_bucket_to_row(bucket: PaperBucket) -> PaperBucketRow:
    return PaperBucketRow(
        paper_id=bucket.paper_id,
        bucket_id=bucket.bucket_id,
        role=bucket.role.value,
        assigned_by=bucket.assigned_by.value,
        reason=bucket.reason,
    )


def paper_bucket_from_row(row: PaperBucketRow) -> PaperBucket:
    return PaperBucket(
        paper_id=row.paper_id,
        bucket_id=row.bucket_id,
        role=BucketRole(row.role),
        assigned_by=AssignedBy(row.assigned_by),
        reason=row.reason,
    )


def summary_to_row(summary: Summary) -> SummaryRow:
    return SummaryRow(
        paper_id=summary.paper_id,
        summary_100=summary.summary_100,
        what=summary.what,
        how=summary.how,
        finding=summary.finding,
        limitation=summary.limitation,
        evidence_json=list(summary.evidence),
        validation_status=summary.validation_status.value,
        **_provenance_columns(summary.provenance),
    )


def summary_from_row(row: SummaryRow) -> Summary:
    return Summary(
        paper_id=row.paper_id,
        summary_100=row.summary_100,
        what=row.what,
        how=row.how,
        finding=row.finding,
        limitation=row.limitation,
        evidence=tuple(row.evidence_json or ()),
        validation_status=SummaryValidationStatus(row.validation_status),
        provenance=_provenance_from_columns(
            row.provenance_model,
            row.provenance_prompt_version,
            row.provenance_generated_at,
            row.provenance_input_hash,
        ),
    )


def enrichment_to_row(enrichment: Enrichment) -> EnrichmentRow:
    signals = enrichment.signals
    return EnrichmentRow(
        paper_id=enrichment.paper_id,
        teams_json=list(signals.teams),
        venue_level=signals.venue_level,
        citation_count=signals.citation_count,
        citation_velocity=signals.citation_velocity,
        code_url=signals.code_url,
        data_url=signals.data_url,
        oa_status=signals.oa_status.value,
        experiment_quality_json=list(signals.experiment_quality),
        confidence=signals.confidence,
        source=signals.source,
        created_at=enrichment.created_at,
        updated_at=enrichment.updated_at,
        **_provenance_columns(enrichment.provenance),
    )


def enrichment_from_row(row: EnrichmentRow) -> Enrichment:
    return Enrichment(
        paper_id=row.paper_id,
        signals=EnrichmentSignals(
            teams=tuple(row.teams_json or ()),
            venue_level=row.venue_level,
            citation_count=row.citation_count,
            citation_velocity=row.citation_velocity,
            code_url=row.code_url,
            data_url=row.data_url,
            oa_status=OaStatus(row.oa_status),
            experiment_quality=tuple(row.experiment_quality_json or ()),
            confidence=row.confidence,
            source=row.source,
        ),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        provenance=_provenance_from_columns(
            row.provenance_model,
            row.provenance_prompt_version,
            row.provenance_generated_at,
            row.provenance_input_hash,
        ),
    )


def score_snapshot_to_row(snapshot: ScoreSnapshot) -> ScoreSnapshotRow:
    return ScoreSnapshotRow(
        paper_id=snapshot.paper_id,
        score_date=snapshot.score_date,
        relevance=snapshot.relevance,
        authority=snapshot.authority,
        extra_value=snapshot.extra_value,
        static_score=snapshot.static_score,
        freshness=snapshot.freshness,
        hunger=snapshot.hunger,
        dynamic_score=snapshot.dynamic_score,
        preference=snapshot.preference,
        priority=snapshot.priority,
        tier=snapshot.tier.value if snapshot.tier is not None else None,
        breakdown_json=dict(snapshot.breakdown_json),
        config_version=snapshot.config_version,
        profile_version=snapshot.profile_version,
        taxonomy_version=snapshot.taxonomy_version,
        **_provenance_columns(snapshot.provenance),
    )


def score_snapshot_from_row(row: ScoreSnapshotRow) -> ScoreSnapshot:
    return ScoreSnapshot(
        paper_id=row.paper_id,
        score_date=row.score_date,
        relevance=row.relevance,
        authority=row.authority,
        extra_value=row.extra_value,
        static_score=row.static_score,
        freshness=row.freshness,
        hunger=row.hunger,
        dynamic_score=row.dynamic_score,
        preference=row.preference,
        priority=row.priority,
        tier=ScoreTier(row.tier) if row.tier is not None else None,
        breakdown_json=dict(row.breakdown_json or {}),
        config_version=row.config_version,
        profile_version=row.profile_version,
        taxonomy_version=row.taxonomy_version,
        provenance=_provenance_from_columns(
            row.provenance_model,
            row.provenance_prompt_version,
            row.provenance_generated_at,
            row.provenance_input_hash,
        ),
    )


def static_score_snapshot_to_row(snapshot: StaticScoreSnapshot) -> StaticScoreSnapshotRow:
    return StaticScoreSnapshotRow(
        paper_id=snapshot.paper_id,
        score_date=snapshot.score_date,
        relevance=snapshot.relevance,
        authority=snapshot.authority,
        extra_value=snapshot.extra_value,
        static_score=snapshot.static_score,
        confidence=snapshot.confidence,
        config_version=snapshot.config_version,
        profile_version=snapshot.profile_version,
        taxonomy_version=snapshot.taxonomy_version,
        input_hash=snapshot.input_hash,
        breakdown_json=dict(snapshot.breakdown_json),
        created_at=snapshot.created_at,
    )


def static_score_snapshot_from_row(row: StaticScoreSnapshotRow) -> StaticScoreSnapshot:
    return StaticScoreSnapshot(
        paper_id=row.paper_id,
        score_date=row.score_date,
        relevance=row.relevance,
        authority=row.authority,
        extra_value=row.extra_value,
        static_score=row.static_score,
        confidence=row.confidence,
        config_version=row.config_version,
        profile_version=row.profile_version,
        taxonomy_version=row.taxonomy_version,
        input_hash=row.input_hash,
        breakdown_json=dict(row.breakdown_json or {}),
        created_at=as_utc(row.created_at),
    )


def review_item_to_row(item: ReviewItem) -> ReviewItemRow:
    return ReviewItemRow(
        paper_id=item.paper_id,
        status=item.status.value,
        reasons_json=list(item.reasons),
        payload_json=dict(item.payload_json),
        created_at=item.created_at,
        updated_at=item.updated_at,
        resolved_at=item.resolved_at,
        resolved_by=item.resolved_by,
        resolution=item.resolution.value if item.resolution is not None else None,
        note=item.note,
    )


def review_item_from_row(row: ReviewItemRow) -> ReviewItem:
    return ReviewItem(
        paper_id=row.paper_id,
        status=ReviewStatus(row.status),
        reasons=tuple(row.reasons_json or ()),
        payload_json=dict(row.payload_json or {}),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        resolved_at=optional_utc(row.resolved_at),
        resolved_by=row.resolved_by,
        resolution=ReviewResolution(row.resolution) if row.resolution is not None else None,
        note=row.note,
    )


def special_nomination_to_row(item: SpecialNominationRecord) -> SpecialNominationRow:
    return SpecialNominationRow(
        id=item.id,
        paper_id=item.paper_id,
        kind=item.kind.value,
        status=item.status.value,
        confidence=item.confidence,
        reason=item.reason,
        evidence_json=[{"quote": entry.quote, "valid": entry.valid} for entry in item.evidence],
        review_layers_json=list(item.review_layers),
        classic_basis_json=list(item.classic_basis),
        bridge_memo_json=dict(item.bridge_memo) if item.bridge_memo is not None else None,
        reasons_json=list(item.reasons),
        **_provenance_columns(item.provenance),
        nominated_at=item.nominated_at,
        decided_at=item.decided_at,
        decided_by=item.decided_by,
        decision_note=item.decision_note,
    )


def special_nomination_from_row(row: SpecialNominationRow) -> SpecialNominationRecord:
    provenance = _provenance_from_columns(
        row.provenance_model,
        row.provenance_prompt_version,
        row.provenance_generated_at,
        row.provenance_input_hash,
    )
    if provenance is None:
        raise ValueError("special_nomination.provenance columns must be non-null")
    return SpecialNominationRecord(
        id=row.id,
        paper_id=row.paper_id,
        kind=SpecialBucketKind(row.kind),
        status=SpecialNominationStatus(row.status),
        confidence=row.confidence,
        reason=row.reason,
        evidence=tuple(
            NominationEvidence(quote=str(entry["quote"]), valid=bool(entry["valid"]))
            for entry in (row.evidence_json or ())
        ),
        review_layers=tuple(row.review_layers_json or ()),
        classic_basis=tuple(row.classic_basis_json or ()),
        bridge_memo=dict(row.bridge_memo_json) if row.bridge_memo_json is not None else None,
        reasons=tuple(row.reasons_json or ()),
        provenance=provenance,
        nominated_at=as_utc(row.nominated_at),
        decided_at=optional_utc(row.decided_at),
        decided_by=row.decided_by,
        decision_note=row.decision_note,
    )


def special_tier_to_row(item: SpecialTierSnapshot) -> SpecialTierSnapshotRow:
    return SpecialTierSnapshotRow(
        id=item.id,
        paper_id=item.paper_id,
        nomination_id=item.nomination_id,
        kind=item.kind.value,
        value=item.value,
        tier=item.tier.value,
        dimensions_json=dict(item.dimensions),
        weights_json=dict(item.weights),
        reasons_json=list(item.reasons),
        explanation_json=list(item.explanation),
        evidence_json=[{"quote": entry.quote, "valid": entry.valid} for entry in item.evidence],
        breakdown_json=dict(item.breakdown_json),
        rubric_version=item.rubric_version,
        assigned_by=item.assigned_by.value,
        note=item.note,
        created_at=item.created_at,
    )


def special_tier_from_row(row: SpecialTierSnapshotRow) -> SpecialTierSnapshot:
    return SpecialTierSnapshot(
        id=row.id,
        paper_id=row.paper_id,
        nomination_id=row.nomination_id,
        kind=SpecialBucketKind(row.kind),
        value=row.value,
        tier=ScoreTier(row.tier),
        dimensions={str(key): float(value) for key, value in (row.dimensions_json or {}).items()},
        weights={str(key): float(value) for key, value in (row.weights_json or {}).items()},
        reasons=tuple(row.reasons_json or ()),
        explanation=tuple(row.explanation_json or ()),
        evidence=tuple(
            NominationEvidence(quote=str(entry["quote"]), valid=bool(entry["valid"]))
            for entry in (row.evidence_json or ())
        ),
        breakdown_json=dict(row.breakdown_json or {}),
        rubric_version=row.rubric_version,
        assigned_by=AssignedBy(row.assigned_by),
        note=row.note,
        created_at=as_utc(row.created_at),
    )


def view_config_to_row(view: ViewConfig) -> ViewConfigRow:
    return ViewConfigRow(
        id=view.id,
        name=view.name,
        weights_json=dict(view.weights),
        preset=view.preset,
        description=view.description,
        created_at=view.created_at,
    )


def view_config_from_row(row: ViewConfigRow) -> ViewConfig:
    return ViewConfig(
        id=row.id,
        name=row.name,
        weights={str(key): float(value) for key, value in (row.weights_json or {}).items()},
        preset=row.preset,
        description=row.description,
        created_at=as_utc(row.created_at),
    )


def event_to_row(event: Event) -> EventRow:
    return EventRow(
        id=event.id,
        event_type=event.event_type.value,
        created_at=event.created_at,
        paper_id=event.paper_id,
        payload_json=dict(event.payload_json),
    )


def event_from_row(row: EventRow) -> Event:
    return Event(
        id=row.id,
        event_type=EventType(row.event_type),
        created_at=as_utc(row.created_at),
        paper_id=row.paper_id,
        payload_json=dict(row.payload_json or {}),
    )


def rating_to_row(rating: Rating) -> RatingRow:
    return RatingRow(
        paper_id=rating.paper_id,
        created_at=rating.created_at,
        score=rating.score,
        note=rating.note,
        tags_at_read_json=list(rating.tags_at_read),
        read_mode=rating.read_mode.value if rating.read_mode is not None else None,
    )


def rating_from_row(row: RatingRow) -> Rating:
    return Rating(
        paper_id=row.paper_id,
        created_at=as_utc(row.created_at),
        score=row.score,
        note=row.note,
        tags_at_read=tuple(row.tags_at_read_json or ()),
        read_mode=ReadMode(row.read_mode) if row.read_mode is not None else None,
    )


def config_to_row(config: Config) -> ConfigRow:
    return ConfigRow(
        key=config.key,
        version=config.version,
        value_json=dict(config.value_json),
        updated_by=config.updated_by.value,
        updated_at=config.updated_at,
    )


def config_from_row(row: ConfigRow) -> Config:
    return Config(
        key=row.key,
        version=row.version,
        value_json=dict(row.value_json or {}),
        updated_by=AssignedBy(row.updated_by),
        updated_at=as_utc(row.updated_at),
    )


def run_to_row(run: Run) -> RunRow:
    return RunRow(
        id=run.id,
        run_type=run.run_type,
        status=run.status.value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats_json=dict(run.stats_json),
        errors_json=dict(run.errors_json),
        cost_json=dict(run.cost_json),
    )


def run_from_row(row: RunRow) -> Run:
    return Run(
        id=row.id,
        run_type=row.run_type,
        status=RunStatus(row.status),
        started_at=as_utc(row.started_at),
        finished_at=optional_utc(row.finished_at),
        stats_json=dict(row.stats_json or {}),
        errors_json=dict(row.errors_json or {}),
        cost_json=dict(row.cost_json or {}),
    )


def archive_snapshot_to_row(snapshot: ArchiveSnapshot) -> ArchiveSnapshotRow:
    return ArchiveSnapshotRow(
        paper_id=snapshot.paper_id,
        archived_at=snapshot.archived_at,
        primary_tag=snapshot.primary_tag,
        filing_bucket_id=snapshot.filing_bucket_id,
        profile_version=snapshot.profile_version,
        taxonomy_version=snapshot.taxonomy_version,
        config_version=snapshot.config_version,
        content_hash=snapshot.content_hash,
        content_json=dict(snapshot.content_json),
        secondary_tags_json=list(snapshot.secondary_tags),
        rating=snapshot.rating,
        note=snapshot.note,
        score_date=snapshot.score_date,
        priority=snapshot.priority,
    )


def archive_snapshot_from_row(row: ArchiveSnapshotRow) -> ArchiveSnapshot:
    return ArchiveSnapshot(
        paper_id=row.paper_id,
        archived_at=as_utc(row.archived_at),
        primary_tag=row.primary_tag,
        filing_bucket_id=row.filing_bucket_id,
        profile_version=row.profile_version,
        taxonomy_version=row.taxonomy_version,
        config_version=row.config_version,
        content_hash=row.content_hash,
        content_json=dict(row.content_json or {}),
        secondary_tags=tuple(row.secondary_tags_json or ()),
        rating=row.rating,
        note=row.note,
        score_date=row.score_date,
        priority=row.priority,
    )


def profile_to_row(profile: ResearchProfile) -> ResearchProfileRow:
    return ResearchProfileRow(
        id=profile.id,
        version=profile.version,
        status=profile.status.value,
        content_md=profile.content_md,
        parsed_json=dict(profile.parsed_json),
        confirmed_at=profile.confirmed_at,
        change_log_json=list(profile.change_log),
    )


def profile_from_row(row: ResearchProfileRow) -> ResearchProfile:
    return ResearchProfile(
        id=row.id,
        version=row.version,
        status=DocumentStatus(row.status),
        content_md=row.content_md,
        parsed_json=dict(row.parsed_json or {}),
        confirmed_at=optional_utc(row.confirmed_at),
        change_log=tuple(row.change_log_json or ()),
    )


def tag_to_row(tag: Tag, *, taxonomy_id: str, taxonomy_version: str) -> TagRow:
    return TagRow(
        taxonomy_id=taxonomy_id,
        taxonomy_version=taxonomy_version,
        code=tag.code,
        name=tag.name,
        level=tag.level.value,
        definition=tag.definition,
        parent_code=tag.parent_code,
        include_json=list(tag.include),
        exclude_json=list(tag.exclude),
        examples_json=list(tag.examples),
        aliases_json=list(tag.aliases),
        active=tag.active,
    )


def tag_from_row(row: TagRow) -> Tag:
    return Tag(
        code=row.code,
        name=row.name,
        level=TagLevel(row.level),
        definition=row.definition,
        parent_code=row.parent_code,
        include=tuple(row.include_json or ()),
        exclude=tuple(row.exclude_json or ()),
        examples=tuple(row.examples_json or ()),
        aliases=tuple(row.aliases_json or ()),
        active=row.active,
    )


def tag_snapshot(tag: Tag) -> dict[str, Any]:
    """``taxonomy.tags_json`` 的可读快照；查询仍以 ``tag`` 表为准。"""
    return {
        "code": tag.code,
        "name": tag.name,
        "level": tag.level.value,
        "definition": tag.definition,
        "parent_code": tag.parent_code,
        "include": list(tag.include),
        "exclude": list(tag.exclude),
        "examples": list(tag.examples),
        "aliases": list(tag.aliases),
        "active": tag.active,
    }


def taxonomy_to_row(taxonomy: Taxonomy) -> TaxonomyRow:
    return TaxonomyRow(
        id=taxonomy.id,
        version=taxonomy.version,
        status=taxonomy.status.value,
        content_md=taxonomy.content_md,
        tags_json=[tag_snapshot(tag) for tag in taxonomy.tags],
        confirmed_at=taxonomy.confirmed_at,
        tags=[
            tag_to_row(tag, taxonomy_id=taxonomy.id, taxonomy_version=taxonomy.version)
            for tag in taxonomy.tags
        ],
    )


def taxonomy_from_row(row: TaxonomyRow) -> Taxonomy:
    return Taxonomy(
        id=row.id,
        version=row.version,
        status=DocumentStatus(row.status),
        content_md=row.content_md,
        tags=tuple(
            tag_from_row(tag)
            for tag in sorted(
                row.tags,
                key=lambda tag: (0 if tag.level == "primary" else 1, tag.code),
            )
        ),
        confirmed_at=optional_utc(row.confirmed_at),
    )


def bucket_to_row(bucket: Bucket) -> BucketRow:
    return BucketRow(
        id=bucket.id,
        name=bucket.name,
        bucket_type=bucket.bucket_type.value,
        tag_code=bucket.tag_code,
        special_kind=bucket.special_kind.value if bucket.special_kind is not None else None,
        quota=bucket.quota,
        active=bucket.active,
    )


def bucket_from_row(row: BucketRow) -> Bucket:
    return Bucket(
        id=row.id,
        name=row.name,
        bucket_type=BucketType(row.bucket_type),
        tag_code=row.tag_code,
        special_kind=(
            SpecialBucketKind(row.special_kind) if row.special_kind is not None else None
        ),
        quota=row.quota,
        active=row.active,
    )


__all__ = [
    "archive_snapshot_from_row",
    "archive_snapshot_to_row",
    "as_utc",
    "bucket_from_row",
    "bucket_to_row",
    "config_from_row",
    "config_to_row",
    "event_from_row",
    "event_to_row",
    "optional_utc",
    "paper_bucket_from_row",
    "paper_bucket_to_row",
    "paper_from_row",
    "paper_tag_from_row",
    "paper_tag_to_row",
    "paper_to_row",
    "profile_from_row",
    "profile_to_row",
    "rating_from_row",
    "rating_to_row",
    "run_from_row",
    "run_to_row",
    "score_snapshot_from_row",
    "score_snapshot_to_row",
    "special_nomination_from_row",
    "special_nomination_to_row",
    "special_tier_from_row",
    "special_tier_to_row",
    "summary_from_row",
    "summary_to_row",
    "tag_from_row",
    "tag_snapshot",
    "tag_to_row",
    "taxonomy_from_row",
    "taxonomy_to_row",
    "view_config_from_row",
    "view_config_to_row",
]
