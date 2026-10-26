"""回顾统计的纯计算层：指标口径、去重、分组与反馈变化清单。

``build_overview`` 只依赖投影输出，不触碰存储；固定样例可直接单测。
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from papersteward.reading.insights.contracts import (
    CountEntry,
    CoverageStats,
    FeedbackChange,
    FeedbackChangeStats,
    FeedbackVersion,
    InsightMetrics,
    InsightOverview,
    InsightsProjection,
    NoteFeedbackChange,
    PaperProjection,
    PeriodKind,
    PeriodMembership,
    ReadingSourceKind,
    ResolvedPeriod,
    ScoreDistribution,
    ScoreFeedbackChange,
    TimePrecision,
)
from papersteward.reading.insights.periods import local_date

OVERVIEW_DETAIL_DEFAULT_LIMIT = 200
OVERVIEW_DETAIL_MAX_LIMIT = 1000


def feedback_changes(
    paper_id: str, versions: Sequence[FeedbackVersion]
) -> list[ScoreFeedbackChange | NoteFeedbackChange]:
    """从相邻反馈版本得到实际认知变化。

    首个版本是初始反馈，不算修订；备注变更只算备注、评分变更只算评分；
    清除为 null 也是有效变化；值未变的重复写入不制造变化事件。
    同一版本同时改两字段时，先评分后备注，各记一条。
    """
    changes: list[ScoreFeedbackChange | NoteFeedbackChange] = []
    for prev, curr in zip(versions, versions[1:], strict=False):
        if prev.score != curr.score:
            changes.append(
                ScoreFeedbackChange(
                    kind="score",
                    paper_id=paper_id,
                    recorded_at=curr.recorded_at,
                    old=prev.score,
                    new=curr.score,
                )
            )
        if prev.note != curr.note:
            changes.append(
                NoteFeedbackChange(
                    kind="note",
                    paper_id=paper_id,
                    recorded_at=curr.recorded_at,
                    old=prev.note,
                    new=curr.note,
                )
            )
    return changes


def build_overview(
    projection: InsightsProjection,
    *,
    tag: str | None = None,
    asset_events: Sequence[tuple[str, datetime]] = (),
) -> InsightOverview:
    """从投影计算事实概览；tag 按冻结主标签收窄，所有分母与明细同一范围。

    口径（§3.3）：本期阅读 = 日期明确属于本期（未知日期不进阅读数，只进
    全部历史与未定库存）；本期补录 = 本期进入归档的外部已读论文，与阅读
    量可能重叠；覆盖率/评分分布以截止前最后有效反馈为准。资产整理活动
    按版本记录时间归入周期（操作数=版本数，不同资产数=去重 asset_id），
    不会增加已读篇数。
    """
    papers = [p for p in projection.papers if tag is None or p.primary_tag == tag]
    period = projection.period
    readings = [
        p
        for p in papers
        if p.membership is PeriodMembership.IN_PERIOD
        and p.reading.precision is not TimePrecision.UNKNOWN
    ]
    backfills = [
        p for p in papers if p.source is ReadingSourceKind.IMPORTED and p.recorded_in_period
    ]
    system_completions = [
        p for p in papers if p.source is ReadingSourceKind.SYSTEM and p.recorded_in_period
    ]
    # 未定库存以阅读精度为主要判据：未知日期无论周期类型都单列；
    # 月精度部分覆盖由 membership 标记。
    unassigned_unknown = [p for p in papers if p.reading.precision is TimePrecision.UNKNOWN]
    partial_month = [p for p in papers if p.membership is PeriodMembership.UNASSIGNED_PARTIAL_MONTH]
    unassigned = unassigned_unknown + partial_month
    period_changes = _period_changes(papers, period)
    period_assets = _period_asset_events(asset_events, period)
    asset_ids = {asset_id for asset_id, _ in period_assets}

    return InsightOverview(
        metrics=InsightMetrics(
            period=period,
            mode=projection.mode,
            knowledge_cutoff=projection.knowledge_cutoff,
            collected_at=projection.collected_at,
            readings_total=len(readings),
            readings_system=sum(1 for p in readings if p.source is ReadingSourceKind.SYSTEM),
            readings_imported=sum(1 for p in readings if p.source is ReadingSourceKind.IMPORTED),
            readings_backfilled=sum(1 for p in readings if not p.recorded_in_period),
            system_completions=len(system_completions),
            backfills_total=len(backfills),
            unassigned_total=len(unassigned_unknown),
            partial_month_total=len(partial_month),
            coverage=_coverage(readings),
            backfill_coverage=_coverage(backfills),
            score_distribution=_score_distribution(readings),
            feedback_changes=_change_stats(period_changes),
            asset_events=len(period_assets),
            asset_assets=len(asset_ids),
            reading_modes=_reading_modes(readings),
            primary_tags=_primary_tags(readings),
            published_decades=_published_decades(readings),
            all_history_total=len(papers),
        ),
        readings=readings,
        backfills=backfills,
        changes=period_changes,
        unassigned=unassigned,
    )


def _coverage(papers: Sequence[PaperProjection]) -> CoverageStats:
    scored = noted = 0
    for paper in papers:
        latest = paper.feedback[-1] if paper.feedback else None
        if latest is None:
            continue
        if latest.score is not None:
            scored += 1
        if latest.note is not None:
            noted += 1
    return CoverageStats(scored=scored, noted=noted, total=len(papers))


def _score_distribution(papers: Sequence[PaperProjection]) -> ScoreDistribution:
    buckets = {f"score_{value}": 0 for value in range(1, 6)}
    unrated = 0
    for paper in papers:
        latest = paper.feedback[-1] if paper.feedback else None
        score = None if latest is None else latest.score
        if score is None:
            unrated += 1
        else:
            buckets[f"score_{score}"] += 1
    return ScoreDistribution(unrated=unrated, **buckets)


def _period_changes(
    papers: Sequence[PaperProjection], period: ResolvedPeriod
) -> list[FeedbackChange]:
    """周期口径的变化事件：recorded_at 本地日落在周期内（ALL 周期即全部）。"""
    if period.kind is PeriodKind.ALL:
        return [change for paper in papers for change in paper.changes]
    start = period.start_date
    end = period.end_date_inclusive
    scoped: list[FeedbackChange] = []
    for paper in papers:
        for change in paper.changes:
            day = local_date(change.recorded_at, period.timezone)
            if start is not None and end is not None and start <= day <= end:
                scoped.append(change)
    return scoped


def _period_asset_events(
    asset_events: Sequence[tuple[str, datetime]], period: ResolvedPeriod
) -> list[tuple[str, datetime]]:
    if period.kind is PeriodKind.ALL:
        return list(asset_events)
    start = period.start_date
    end = period.end_date_inclusive
    scoped = []
    for asset_id, recorded_at in asset_events:
        day = local_date(recorded_at, period.timezone)
        if start is not None and end is not None and start <= day <= end:
            scoped.append((asset_id, recorded_at))
    return scoped


def _change_stats(changes: Sequence[FeedbackChange]) -> FeedbackChangeStats:
    score_events = sum(1 for change in changes if change.kind == "score")
    note_events = sum(1 for change in changes if change.kind == "note")
    papers_affected = {change.paper_id for change in changes}
    return FeedbackChangeStats(
        score_events=score_events,
        note_events=note_events,
        papers_affected=len(papers_affected),
    )


def _reading_modes(readings: Sequence[PaperProjection]) -> list[CountEntry]:
    counts: dict[str, int] = defaultdict(int)
    for paper in readings:
        key = paper.reading_mode if paper.reading_mode else "unknown"
        counts[key] += 1
    return _entries(counts)


def _primary_tags(readings: Sequence[PaperProjection]) -> list[CountEntry]:
    counts: dict[tuple[str, str | None], int] = defaultdict(int)
    for paper in readings:
        key = (paper.primary_tag or "", paper.taxonomy_version)
        counts[key] += 1
    entries = [
        CountEntry(
            key=code,
            label=code if code else "缺标签",
            # 缺标签不属于任何标签版本组，版本列留空。
            taxonomy_version=version if code else None,
            count=count,
        )
        for (code, version), count in counts.items()
    ]
    return _sorted_tag_entries(entries)


def _published_decades(readings: Sequence[PaperProjection]) -> list[CountEntry]:
    counts: dict[str, int] = defaultdict(int)
    for paper in readings:
        if paper.published_on is None:
            counts["unknown"] += 1
        else:
            decade_start = paper.published_on.year // 10 * 10
            counts[f"{decade_start}s"] += 1
    return _entries(counts)


def _entries(counts: dict[str, int]) -> list[CountEntry]:
    entries = [CountEntry(key=key, label=key, count=count) for key, count in counts.items()]
    return _sorted_entries(entries)


def _sorted_entries(entries: list[CountEntry]) -> list[CountEntry]:
    """按数量降序、key 升序（版本次之）排序，保证输出确定。"""
    return sorted(
        entries,
        key=lambda entry: (-entry.count, entry.key, entry.taxonomy_version or ""),
    )


def _sorted_tag_entries(entries: list[CountEntry]) -> list[CountEntry]:
    """主题分布排序：数量降序；缺标签（key 为空）固定排在最后。"""
    return sorted(
        entries,
        key=lambda entry: (entry.key == "", -entry.count, entry.key, entry.taxonomy_version or ""),
    )
