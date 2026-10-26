"""有效范围与输入清单：概览、报告、去重和比较共用，不依赖运行记录。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session, load_only

from papersteward.core.entities import utc_now
from papersteward.errors import InvalidRequestError
from papersteward.reading.insights.aggregate import build_overview, matches_tag
from papersteward.reading.insights.contracts import (
    AnalysisScope,
    AssetSourceRef,
    EffectiveAssetInput,
    EffectiveInputManifest,
    EffectivePaperInput,
    InsightOverview,
    InsightQueryRequest,
    InsightsProjection,
    PaperProjection,
    PeriodKind,
    PeriodMembership,
    ReadingSourceKind,
    ReportAssetRef,
    ReportContext,
    ScopeTopic,
)
from papersteward.reading.insights.periods import recorded_in_period, resolve_period
from papersteward.reading.insights.projection import BATCH_SIZE, build_projection
from papersteward.storage.delivery import content_hash
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingAssetSourceRow,
)


@dataclass(frozen=True)
class ScopedInputs:
    context: ReportContext
    projection: InsightsProjection
    overview: InsightOverview


def scope_topic(tag: str | None) -> ScopeTopic | None:
    if not tag:
        return None
    if "::" in tag:
        version, code = tag.split("::", 1)
        return ScopeTopic(code=code, taxonomy_version=version)
    return ScopeTopic(code=tag)


def normalized_query(query: InsightQueryRequest) -> InsightQueryRequest:
    try:
        topic = scope_topic(query.tag)
    except ValueError:
        raise InvalidRequestError("主题筛选无效，请重新选择分类版本与主标签") from None
    tag = (
        None
        if topic is None
        else (f"{topic.taxonomy_version}::{topic.code}" if topic.taxonomy_version else topic.code)
    )
    period = resolve_period(query.period)
    values = query.period.model_dump()
    if period.kind in {PeriodKind.WEEK, PeriodKind.MONTH}:
        values["anchor_date"] = period.start_date
    return query.model_copy(update={"period": query.period.model_validate(values), "tag": tag})


def effective_papers(projection: InsightsProjection) -> list[PaperProjection]:
    """未定日期和全库来源结构只是库存说明，除非参与本期活动或全部历史。"""
    period = projection.period
    if period.kind is PeriodKind.ALL:
        return list(projection.papers)
    return [
        paper
        for paper in projection.papers
        if (
            paper.membership
            in {PeriodMembership.IN_PERIOD, PeriodMembership.UNASSIGNED_PARTIAL_MONTH}
            or (
                paper.recorded_in_period
                and paper.source in {ReadingSourceKind.SYSTEM, ReadingSourceKind.IMPORTED}
            )
            or any(recorded_in_period(change.recorded_at, period) for change in paper.changes)
        )
    ]


def paper_manifest(papers: list[PaperProjection]) -> list[EffectivePaperInput]:
    return [
        EffectivePaperInput(
            paper_id=paper.paper_id,
            archive_hash=paper.archive_hash,
            input_hash=content_hash(paper.model_dump(mode="json")),
        )
        for paper in papers
    ]


def series_key(context: ReportContext) -> str | None:
    if (
        context.status != "complete"
        or not context.scope.topic_known
        or context.scope.algorithm_version is None
    ):
        return None
    return content_hash(
        {
            "scope": context.scope.model_dump(mode="json", exclude={"knowledge_cutoff"}),
            "include_asset_bodies": context.include_asset_bodies,
        }
    )


def input_fingerprint(context: ReportContext) -> str:
    if series_key(context) is None or context.manifest is None:
        raise InvalidRequestError("报告范围或统计口径未知，请选择明确范围重新保存回顾")
    end = context.scope.period.end_utc_exclusive
    return content_hash(
        {
            "series": series_key(context),
            "period_closed": end is not None and end <= context.scope.knowledge_cutoff,
            "manifest": context.manifest.model_dump(mode="json") if context.manifest else None,
        }
    )


def collect_scope(
    session: Session,
    query: InsightQueryRequest,
    *,
    collected_at: datetime | None = None,
    include_asset_bodies: bool = False,
    on_batch: Callable[[], None] | None = None,
) -> ScopedInputs:
    """所有源版本共享显式读事务；本方法结束后调用方只能短写，不能升级该读事务。"""
    query = normalized_query(query)
    session.execute(text("BEGIN DEFERRED"))
    try:
        projection = build_projection(
            session,
            query,
            collected_at=collected_at or utc_now(),
            shared_snapshot=True,
            on_batch=on_batch,
        )
        projection = projection.model_copy(
            update={
                "papers": [paper for paper in projection.papers if matches_tag(paper, query.tag)]
            }
        )
        scope = AnalysisScope(
            period=projection.period,
            mode=query.mode,
            knowledge_cutoff=projection.knowledge_cutoff,
            topic=scope_topic(query.tag),
        )
        refs, versions = _select_assets(
            session, scope, projection.papers, include_bodies=include_asset_bodies
        )
    finally:
        session.rollback()
    context = ReportContext(
        status="complete",
        scope=scope,
        normalized_request=query,
        include_asset_bodies=include_asset_bodies,
        manifest=EffectiveInputManifest(
            papers=paper_manifest(effective_papers(projection)), asset_versions=versions
        ),
    )
    overview = build_overview(
        projection, asset_events=[(v.asset_id, v.recorded_at) for v in versions]
    )
    overview = overview.model_copy(update={"assets": refs})
    return ScopedInputs(context=context, projection=projection, overview=overview)


def _select_assets(
    session: Session,
    scope: AnalysisScope,
    papers: list[PaperProjection],
    *,
    include_bodies: bool,
) -> tuple[list[ReportAssetRef], list[EffectiveAssetInput]]:
    """先按每个事件版本的来源选入；改来源不使过去的事件或引用漂移到最新版本。"""
    matching_ids = {paper.paper_id for paper in papers}
    rows = session.execute(
        select(ReadingAssetRevisionRow, ReadingAssetRow.kind)
        .join(ReadingAssetRow, ReadingAssetRow.id == ReadingAssetRevisionRow.asset_id)
        .options(
            load_only(
                ReadingAssetRevisionRow.asset_id,
                ReadingAssetRevisionRow.revision,
                ReadingAssetRevisionRow.recorded_at,
                ReadingAssetRevisionRow.title,
                ReadingAssetRevisionRow.status,
            )
        )
        .where(ReadingAssetRevisionRow.recorded_at < scope.knowledge_cutoff)
        .order_by(ReadingAssetRevisionRow.asset_id, ReadingAssetRevisionRow.revision)
    ).all()
    sources_by_version: dict[tuple[str, int], list[AssetSourceRef]] = {}
    for source in session.scalars(
        select(ReadingAssetSourceRow)
        .join(
            ReadingAssetRevisionRow,
            (ReadingAssetSourceRow.asset_id == ReadingAssetRevisionRow.asset_id)
            & (ReadingAssetSourceRow.revision == ReadingAssetRevisionRow.revision),
        )
        .where(ReadingAssetRevisionRow.recorded_at < scope.knowledge_cutoff)
        .order_by(
            ReadingAssetSourceRow.asset_id,
            ReadingAssetSourceRow.revision,
            ReadingAssetSourceRow.paper_id,
        )
    ):
        sources_by_version.setdefault((source.asset_id, source.revision), []).append(
            AssetSourceRef(
                paper_id=source.paper_id,
                archive_hash=source.archive_hash,
                rating_at=as_utc(source.rating_at) if source.rating_at else None,
            )
        )
    versions = []
    latest: dict[str, ReportAssetRef] = {}
    for row, kind in rows:
        sources = sources_by_version.get((row.asset_id, row.revision), [])
        if not any(source.paper_id in matching_ids for source in sources):
            continue
        at = as_utc(row.recorded_at)
        if not recorded_in_period(at, scope.period):
            continue
        versions.append(
            EffectiveAssetInput(
                asset_id=row.asset_id,
                revision=row.revision,
                recorded_at=at,
                sources=sources,
                input_hash=content_hash({"title": row.title, "status": row.status, "kind": kind}),
            )
        )
        latest[row.asset_id] = ReportAssetRef(
            asset_id=row.asset_id,
            revision=row.revision,
            title=row.title,
            kind=kind,
            status=row.status,
            source_paper_ids=[source.paper_id for source in sources],
            cross_topic=scope.topic is not None
            and any(source.paper_id not in matching_ids for source in sources),
        )
    refs = list(latest.values())
    if include_bodies:
        keys = [(ref.asset_id, ref.revision) for ref in refs]
        bodies = {}
        for offset in range(0, len(keys), BATCH_SIZE):
            bodies.update(
                {
                    (asset_id, revision): body
                    for asset_id, revision, body in session.execute(
                        select(
                            ReadingAssetRevisionRow.asset_id,
                            ReadingAssetRevisionRow.revision,
                            ReadingAssetRevisionRow.body,
                        ).where(
                            tuple_(
                                ReadingAssetRevisionRow.asset_id, ReadingAssetRevisionRow.revision
                            ).in_(keys[offset : offset + BATCH_SIZE])
                        )
                    )
                }
            )
        refs = [
            ref.model_copy(update={"body": bodies[(ref.asset_id, ref.revision or 0)]})
            for ref in refs
        ]
    return refs, versions
