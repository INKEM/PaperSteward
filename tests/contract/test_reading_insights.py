"""阅读回顾契约冻结：输入严格性与最小固定投影结果。

不冻结随机 ID 与生成时间；只冻结输入规则、语义版本与输出形状。
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from papersteward.reading.insights.contracts import (
    PROJECTION_VERSION,
    FeedbackVersion,
    InsightPeriodRequest,
    InsightQueryRequest,
    InsightsProjection,
    NoteFeedbackChange,
    PaperProjection,
    PeriodKind,
    PeriodMembership,
    ReadingSourceKind,
    ReadingTime,
    ReadingTimeBasis,
    ResolvedPeriod,
    ReviewMode,
    ScoreFeedbackChange,
    TimePrecision,
)

pytestmark = pytest.mark.contract

WEEK_REQUEST = {"kind": "week", "timezone": "Asia/Shanghai", "anchor_date": "2026-10-22"}


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "week", "timezone": "Asia/Shanghai"},
        {**WEEK_REQUEST, "start_date": "2026-10-19"},
        {"kind": "month", "timezone": "Asia/Shanghai"},
        {"kind": "custom", "timezone": "Asia/Shanghai", "start_date": "2026-03-05"},
        {
            "kind": "custom",
            "timezone": "Asia/Shanghai",
            "start_date": "2026-03-05",
            "end_date": "2026-03-01",
        },
        {
            "kind": "custom",
            "timezone": "Asia/Shanghai",
            "start_date": "2026-03-05",
            "end_date": "2026-03-06",
            "anchor_date": "2026-03-05",
        },
        {"kind": "all", "timezone": "Asia/Shanghai", "anchor_date": "2026-10-22"},
        {"kind": "week", "timezone": "Mars/Olympus", "anchor_date": "2026-10-22"},
    ],
)
def test_period_request_rejects_inconsistent_input(payload: dict):
    with pytest.raises(ValidationError):
        InsightPeriodRequest.model_validate(payload)


def test_period_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        InsightPeriodRequest.model_validate({**WEEK_REQUEST, "surprise": 1})


def test_query_request_defaults_to_as_recorded_and_is_strict():
    request = InsightQueryRequest(period=InsightPeriodRequest.model_validate(WEEK_REQUEST))
    assert request.mode is ReviewMode.AS_RECORDED
    with pytest.raises(ValidationError):
        InsightQueryRequest.model_validate({**WEEK_REQUEST, "mode": "bogus"})


@pytest.mark.parametrize(
    "payload",
    [
        {"precision": "day", "basis": "system_recorded"},
        {"precision": "day", "basis": "system_recorded", "day": "2026-10-20", "month": "2026-10"},
        {"precision": "month", "basis": "declared", "day": "2026-10-20", "month": "2026-10"},
        {"precision": "month", "basis": "declared", "month": "2026-3"},
        {"precision": "unknown", "basis": "unknown", "month": "2026-10"},
    ],
)
def test_reading_time_precisions_are_mutually_exclusive(payload: dict):
    with pytest.raises(ValidationError):
        ReadingTime.model_validate(payload)


def test_feedback_change_union_is_strictly_typed():
    at = datetime(2026, 10, 22, 3, tzinfo=UTC)
    change = ScoreFeedbackChange(kind="score", paper_id="p", recorded_at=at, old=5, new=None)
    assert change.model_dump(mode="json") == {
        "kind": "score",
        "paper_id": "p",
        "recorded_at": "2026-10-22T03:00:00Z",
        "old": 5,
        "new": None,
    }
    with pytest.raises(ValidationError):
        ScoreFeedbackChange(kind="score", paper_id="p", recorded_at=at, old=0, new=5)
    with pytest.raises(ValidationError):
        NoteFeedbackChange.model_validate(
            {"kind": "note", "paper_id": "p", "recorded_at": at, "old": 5, "new": None}
        )


def test_projection_version_is_frozen():
    assert PROJECTION_VERSION == "reading-insights.projection/1"
    with pytest.raises(ValidationError):
        InsightsProjection(
            projection_version="reading-insights.projection/2",
            mode=ReviewMode.AS_RECORDED,
            period=ResolvedPeriod(
                kind=PeriodKind.ALL, timezone="Asia/Shanghai", label="all-history"
            ),
            knowledge_cutoff=datetime(2026, 3, 5, 16, tzinfo=UTC),
            collected_at=datetime(2026, 3, 5, 16, tzinfo=UTC),
        )


def test_minimal_projection_json_is_frozen():
    period = ResolvedPeriod(
        kind=PeriodKind.CUSTOM,
        timezone="Asia/Shanghai",
        label="2026-03-05..2026-03-05",
        start_date=date(2026, 3, 5),
        end_date_inclusive=date(2026, 3, 5),
        start_utc=datetime(2026, 3, 4, 16, tzinfo=UTC),
        end_utc_exclusive=datetime(2026, 3, 5, 16, tzinfo=UTC),
    )
    projection = InsightsProjection(
        projection_version=PROJECTION_VERSION,
        mode=ReviewMode.AS_RECORDED,
        period=period,
        knowledge_cutoff=datetime(2026, 3, 5, 16, tzinfo=UTC),
        collected_at=datetime(2026, 3, 5, 16, tzinfo=UTC),
        papers=[
            PaperProjection(
                paper_id="p1",
                archived_at=datetime(2026, 3, 5, 2, tzinfo=UTC),
                source=ReadingSourceKind.SYSTEM,
                content_available=True,
                reading=ReadingTime(
                    precision=TimePrecision.DAY,
                    basis=ReadingTimeBasis.SYSTEM_RECORDED,
                    day=date(2026, 3, 5),
                    recorded_at_utc=datetime(2026, 3, 5, 2, tzinfo=UTC),
                ),
                membership=PeriodMembership.IN_PERIOD,
                recorded_in_period=True,
                title="冻结标题",
                primary_tag="T01",
                feedback=[
                    FeedbackVersion(
                        recorded_at=datetime(2026, 3, 5, 2, tzinfo=UTC),
                        score=4,
                        note="初始",
                        is_initial=True,
                    )
                ],
            )
        ],
    )
    assert projection.model_dump(mode="json") == {
        "projection_version": "reading-insights.projection/1",
        "mode": "as_recorded",
        "period": {
            "kind": "custom",
            "timezone": "Asia/Shanghai",
            "label": "2026-03-05..2026-03-05",
            "start_date": "2026-03-05",
            "end_date_inclusive": "2026-03-05",
            "start_utc": "2026-03-04T16:00:00Z",
            "end_utc_exclusive": "2026-03-05T16:00:00Z",
        },
        "knowledge_cutoff": "2026-03-05T16:00:00Z",
        "collected_at": "2026-03-05T16:00:00Z",
        "papers": [
            {
                "paper_id": "p1",
                "archived_at": "2026-03-05T02:00:00Z",
                "source": "system",
                "content_available": True,
                "reading": {
                    "precision": "day",
                    "basis": "system_recorded",
                    "day": "2026-03-05",
                    "month": None,
                    "recorded_at_utc": "2026-03-05T02:00:00Z",
                },
                "membership": "in_period",
                "recorded_in_period": True,
                "title": "冻结标题",
                "reading_mode": None,
                "published_on": None,
                "primary_tag": "T01",
                "secondary_tags": [],
                "taxonomy_version": None,
                "feedback": [
                    {
                        "recorded_at": "2026-03-05T02:00:00Z",
                        "score": 4,
                        "note": "初始",
                        "is_initial": True,
                    }
                ],
                "changes": [],
            }
        ],
    }
