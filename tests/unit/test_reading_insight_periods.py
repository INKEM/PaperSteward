"""周期边界、知识截止与活动归属的纯逻辑验证。"""

from datetime import UTC, date, datetime

from papersteward.reading.insights.contracts import (
    InsightPeriodRequest,
    PeriodKind,
    PeriodMembership,
    ReadingTime,
    ReadingTimeBasis,
    ResolvedPeriod,
    ReviewMode,
    TimePrecision,
)
from papersteward.reading.insights.periods import (
    classify_membership,
    covers_whole_month,
    knowledge_cutoff,
    local_date,
    recorded_in_period,
    resolve_period,
)

TZ = "Asia/Shanghai"
COLLECTED = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)


def period_request(kind: str, **fields: object) -> InsightPeriodRequest:
    return InsightPeriodRequest.model_validate({"kind": kind, "timezone": TZ, **fields})


def week() -> ResolvedPeriod:
    return resolve_period(period_request("week", anchor_date="2026-10-22"))


def day_reading(day: str) -> ReadingTime:
    return ReadingTime(
        precision=TimePrecision.DAY,
        basis=ReadingTimeBasis.DECLARED,
        day=date.fromisoformat(day),
    )


def month_reading(month: str) -> ReadingTime:
    return ReadingTime(precision=TimePrecision.MONTH, basis=ReadingTimeBasis.DECLARED, month=month)


UNKNOWN_READING = ReadingTime(precision=TimePrecision.UNKNOWN, basis=ReadingTimeBasis.UNKNOWN)


def test_week_boundaries_are_monday_start_and_converted_to_utc():
    period = week()
    assert period.label == "2026-W43"
    assert period.start_date == date(2026, 10, 19)
    assert period.end_date_inclusive == date(2026, 10, 25)
    assert period.start_utc == datetime(2026, 10, 18, 16, tzinfo=UTC)
    assert period.end_utc_exclusive == datetime(2026, 10, 25, 16, tzinfo=UTC)


def test_week_across_month_and_year_boundaries():
    across_month = resolve_period(period_request("week", anchor_date="2026-11-01"))
    assert (across_month.start_date, across_month.end_date_inclusive) == (
        date(2026, 10, 26),
        date(2026, 11, 1),
    )
    assert across_month.label == "2026-W44"
    across_year = resolve_period(period_request("week", anchor_date="2027-01-01"))
    assert (across_year.start_date, across_year.end_date_inclusive) == (
        date(2026, 12, 28),
        date(2027, 1, 3),
    )
    assert across_year.label == "2026-W53"


def test_month_boundaries_include_leap_year_and_year_rollover():
    leap = resolve_period(period_request("month", anchor_date="2028-02-15"))
    assert leap.label == "2028-02"
    assert (leap.start_date, leap.end_date_inclusive) == (date(2028, 2, 1), date(2028, 2, 29))
    assert leap.end_utc_exclusive == datetime(2028, 2, 29, 16, tzinfo=UTC)
    december = resolve_period(period_request("month", anchor_date="2026-12-31"))
    assert december.label == "2026-12"
    assert december.end_utc_exclusive == datetime(2026, 12, 31, 16, tzinfo=UTC)


def test_custom_period_is_local_half_open_range():
    period = resolve_period(
        period_request("custom", start_date="2026-03-05", end_date="2026-03-05")
    )
    assert period.label == "2026-03-05..2026-03-05"
    assert period.start_utc == datetime(2026, 3, 4, 16, tzinfo=UTC)
    assert period.end_utc_exclusive == datetime(2026, 3, 5, 16, tzinfo=UTC)


def test_all_history_period_has_no_bounds():
    period = resolve_period(period_request("all"))
    assert period.kind is PeriodKind.ALL
    assert period.start_date is None
    assert period.end_date_inclusive is None
    assert period.start_utc is None
    assert period.end_utc_exclusive is None


def test_knowledge_cutoff_for_closed_period_uses_period_end_when_as_recorded():
    assert knowledge_cutoff(week(), ReviewMode.AS_RECORDED, COLLECTED) == datetime(
        2026, 10, 25, 16, tzinfo=UTC
    )


def test_knowledge_cutoff_for_open_period_falls_back_to_collection_time():
    early = datetime(2026, 10, 20, 0, tzinfo=UTC)
    assert knowledge_cutoff(week(), ReviewMode.AS_RECORDED, early) == early


def test_knowledge_cutoff_revised_now_always_uses_collection_time():
    assert knowledge_cutoff(week(), ReviewMode.REVISED_NOW, COLLECTED) == COLLECTED


def test_knowledge_cutoff_for_all_history_is_collection_time():
    assert knowledge_cutoff(
        resolve_period(period_request("all")), ReviewMode.AS_RECORDED, COLLECTED
    ) == (COLLECTED)


def test_local_date_assigns_utc_instant_to_report_timezone_day():
    instant = datetime(2026, 10, 25, 16, 30, tzinfo=UTC)
    assert local_date(instant, "Asia/Shanghai") == date(2026, 10, 26)
    assert local_date(instant, "America/New_York") == date(2026, 10, 25)


def test_day_membership_boundaries_are_inclusive():
    period = week()
    assert classify_membership(day_reading("2026-10-19"), period) is PeriodMembership.IN_PERIOD
    assert classify_membership(day_reading("2026-10-25"), period) is PeriodMembership.IN_PERIOD
    assert classify_membership(day_reading("2026-10-18"), period) is (
        PeriodMembership.OUTSIDE_PERIOD
    )
    assert classify_membership(day_reading("2026-10-26"), period) is (
        PeriodMembership.OUTSIDE_PERIOD
    )


def test_month_membership_requires_whole_month_coverage():
    october = month_reading("2026-10")
    assert classify_membership(
        october, resolve_period(period_request("month", anchor_date="2026-10-15"))
    ) is (PeriodMembership.IN_PERIOD)
    assert classify_membership(october, week()) is PeriodMembership.UNASSIGNED_PARTIAL_MONTH
    full_custom = resolve_period(
        period_request("custom", start_date="2026-10-01", end_date="2026-10-31")
    )
    assert classify_membership(october, full_custom) is PeriodMembership.IN_PERIOD
    partial_custom = resolve_period(
        period_request("custom", start_date="2026-10-15", end_date="2026-11-15")
    )
    assert classify_membership(october, partial_custom) is (
        PeriodMembership.UNASSIGNED_PARTIAL_MONTH
    )


def test_unknown_reading_time_is_listed_separately():
    period = week()
    assert classify_membership(UNKNOWN_READING, period) is PeriodMembership.UNASSIGNED_UNKNOWN


def test_all_history_membership_includes_every_precision():
    period = resolve_period(period_request("all"))
    assert classify_membership(day_reading("1999-01-01"), period) is PeriodMembership.IN_PERIOD
    assert classify_membership(month_reading("1999-01"), period) is PeriodMembership.IN_PERIOD
    assert classify_membership(UNKNOWN_READING, period) is PeriodMembership.IN_PERIOD


def test_covers_whole_month_distinguishes_partial_and_exact():
    exact = resolve_period(period_request("month", anchor_date="2026-10-02"))
    partial = week()
    assert covers_whole_month(exact, 2026, 10)
    assert not covers_whole_month(partial, 2026, 10)
    assert covers_whole_month(resolve_period(period_request("all")), 2026, 10) is False


def test_recorded_in_period_uses_local_archive_day():
    period = week()
    assert recorded_in_period(datetime(2026, 10, 20, 2, tzinfo=UTC), period)
    assert recorded_in_period(datetime(2026, 10, 25, 15, 0, tzinfo=UTC), period)
    # 上海时区已过午夜即算次日，落出周期。
    assert not recorded_in_period(datetime(2026, 10, 25, 16, 30, tzinfo=UTC), period)
    assert recorded_in_period(
        datetime(2026, 10, 25, 16, 30, tzinfo=UTC), resolve_period(period_request("all"))
    )
