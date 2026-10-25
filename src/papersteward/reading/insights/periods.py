"""周期解析、知识截止与活动归属的纯逻辑；不依赖 ORM 与存储。

周期统一表示为本地 ``[开始日, 结束日次日)``，换算 UTC 查询边界；
本地自然日按报告时区归日，声明类日期切换时区不移动。
"""

import calendar
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from papersteward.reading.insights.contracts import (
    InsightPeriodRequest,
    PeriodKind,
    PeriodMembership,
    ReadingTime,
    ResolvedPeriod,
    ReviewMode,
    TimePrecision,
)
from papersteward.storage.mappers import as_utc


def resolve_period(request: InsightPeriodRequest) -> ResolvedPeriod:
    """把周期请求解析为确定边界；周一起始，与 weekly_cutoff 的周日截止一致。"""
    if request.kind is PeriodKind.ALL:
        return ResolvedPeriod(kind=request.kind, timezone=request.timezone, label="all-history")
    if request.kind is PeriodKind.WEEK:
        anchor = request.anchor_date
        assert anchor is not None
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        iso = start.isocalendar()
        label = f"{iso.year:04d}-W{iso.week:02d}"
    elif request.kind is PeriodKind.MONTH:
        anchor = request.anchor_date
        assert anchor is not None
        start = anchor.replace(day=1)
        end = date(anchor.year, anchor.month, calendar.monthrange(anchor.year, anchor.month)[1])
        label = f"{start.year:04d}-{start.month:02d}"
    else:
        assert request.start_date is not None and request.end_date is not None
        start = request.start_date
        end = request.end_date
        label = f"{start.isoformat()}..{end.isoformat()}"
    zone = ZoneInfo(request.timezone)
    start_utc = datetime(start.year, start.month, start.day, tzinfo=zone).astimezone(UTC)
    end_utc = datetime(end.year, end.month, end.day, tzinfo=zone).astimezone(UTC) + timedelta(
        days=1
    )
    return ResolvedPeriod(
        kind=request.kind,
        timezone=request.timezone,
        label=label,
        start_date=start,
        end_date_inclusive=end,
        start_utc=start_utc,
        end_utc_exclusive=end_utc,
    )


def knowledge_cutoff(period: ResolvedPeriod, mode: ReviewMode, collected_at: datetime) -> datetime:
    """知识截止：当时记录用已结束周期的结束时刻，否则用采集时刻（排他上界）。"""
    collected = as_utc(collected_at)
    if ReviewMode(mode) is ReviewMode.REVISED_NOW or period.end_utc_exclusive is None:
        return collected
    end = as_utc(period.end_utc_exclusive)
    return end if end <= collected else collected


def local_date(value: datetime, timezone: str) -> date:
    """把 UTC 时刻归入报告时区的自然日。"""
    return as_utc(value).astimezone(ZoneInfo(timezone)).date()


def covers_whole_month(period: ResolvedPeriod, year: int, month: int) -> bool:
    """周期是否完整覆盖指定日历月；月精度只有整月覆盖才计入。"""
    if period.start_date is None or period.end_date_inclusive is None:
        return False
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    return period.start_date <= month_start and period.end_date_inclusive >= month_end


def classify_membership(reading: ReadingTime, period: ResolvedPeriod) -> PeriodMembership:
    """按阅读时间精度把归档归属到活动周期；不伪造具体日期。"""
    if period.kind is PeriodKind.ALL:
        return PeriodMembership.IN_PERIOD
    if reading.precision is TimePrecision.UNKNOWN:
        return PeriodMembership.UNASSIGNED_UNKNOWN
    if reading.precision is TimePrecision.MONTH:
        year, month = (int(part) for part in (reading.month or "").split("-"))
        if covers_whole_month(period, year, month):
            return PeriodMembership.IN_PERIOD
        return PeriodMembership.UNASSIGNED_PARTIAL_MONTH
    assert period.start_date is not None and period.end_date_inclusive is not None
    assert reading.day is not None
    if period.start_date <= reading.day <= period.end_date_inclusive:
        return PeriodMembership.IN_PERIOD
    return PeriodMembership.OUTSIDE_PERIOD


def recorded_in_period(archived_at: datetime, period: ResolvedPeriod) -> bool:
    """归档记录时间是否落在活动周期内；区分新读/补录与后补记录。"""
    if period.kind is PeriodKind.ALL:
        return True
    assert period.start_date is not None and period.end_date_inclusive is not None
    day = local_date(archived_at, period.timezone)
    return period.start_date <= day <= period.end_date_inclusive
