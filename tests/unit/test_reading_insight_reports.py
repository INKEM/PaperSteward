"""阅读回顾报告的纯逻辑验证：输入指纹、正文去隐私与导出。"""

import json
from datetime import UTC, date, datetime

from test_reading_insight_aggregate import (
    ARCHIVED,
    COLLECTED,
    CUTOFF,
    TZ,
    UNKNOWN_READING,
    WEEK,
    paper,
    projection,
)

from papersteward.reading.insights.aggregate import build_overview
from papersteward.reading.insights.contracts import (
    FeedbackVersion,
    InsightPeriodRequest,
    InsightQueryRequest,
    NoteFeedbackChange,
    ReportDetail,
    ReportSummary,
    ReviewMode,
    ScoreFeedbackChange,
)
from papersteward.reading.insights.export import export_report
from papersteward.reading.insights.reports import build_report_content, report_fingerprint


def _query() -> InsightQueryRequest:
    return InsightQueryRequest(
        period=InsightPeriodRequest.model_validate(
            {"kind": "week", "timezone": TZ, "anchor_date": "2026-10-22"}
        ),
        mode=ReviewMode.AS_RECORDED,
    )


def test_fingerprint_is_deterministic_for_same_inputs():
    proj = projection([paper("a"), paper("b")])
    assert report_fingerprint(_query(), WEEK, CUTOFF, proj) == report_fingerprint(
        _query(), WEEK, CUTOFF, proj
    )


def test_fingerprint_changes_with_cutoff_period_state_and_data():
    proj = projection([paper("a")])
    base = report_fingerprint(_query(), WEEK, CUTOFF, proj)
    # 截止变化（如未结束周期以采集时刻为截止）→ 时间语义变化。
    earlier = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
    assert report_fingerprint(_query(), WEEK, earlier, proj) != base
    # 数据变化。
    grown = projection([paper("a"), paper("b")])
    assert report_fingerprint(_query(), WEEK, CUTOFF, grown) != base


def test_report_content_strips_private_notes_but_keeps_flags():
    overview = build_overview(
        projection(
            [
                paper(
                    "a",
                    score=3,
                    note="秘密备注",
                    changes=[
                        ScoreFeedbackChange(
                            kind="score",
                            paper_id="a",
                            recorded_at=datetime(2026, 10, 21, 3, tzinfo=UTC),
                            old=5,
                            new=3,
                        ),
                        NoteFeedbackChange(
                            kind="note",
                            paper_id="a",
                            recorded_at=datetime(2026, 10, 21, 3, tzinfo=UTC),
                            old="旧备注",
                            new="秘密备注",
                        ),
                    ],
                    feedback=[
                        FeedbackVersion(
                            recorded_at=ARCHIVED, score=5, note="旧备注", is_initial=True
                        ),
                        FeedbackVersion(
                            recorded_at=datetime(2026, 10, 21, 3, tzinfo=UTC),
                            score=3,
                            note="秘密备注",
                        ),
                    ],
                ),
                paper("imp", source="imported", reading=UNKNOWN_READING),
            ]
        )
    )
    content = build_report_content(overview)
    dumped = json.dumps(content.model_dump(mode="json"), ensure_ascii=False)
    assert "秘密备注" not in dumped
    assert "旧备注" not in dumped
    target = next(p for p in content.readings if p.paper_id == "a")
    assert target.feedback[0].has_note and target.feedback[-1].has_note
    assert [change.kind for change in content.changes] == ["score", "note"]
    score_change = content.changes[0]
    assert (score_change.old_score, score_change.new_score) == (5, 3)
    note_change = content.changes[1]
    assert note_change.old_score is None and note_change.new_score is None


def _detail() -> ReportDetail:
    overview = build_overview(projection([paper("a", score=4, note="n")]))
    content = build_report_content(overview)
    summary = ReportSummary(
        report_id="rep-1",
        report_kind="week",
        period_label="2026-W43",
        period_start=date(2026, 10, 19),
        period_end=date(2026, 10, 25),
        timezone=TZ,
        mode=content.metrics.mode,
        knowledge_cutoff=CUTOFF,
        generated_at=COLLECTED,
        contract=content.contract,
        run_id="run-1",
    )
    return ReportDetail(content=content, **summary.model_dump())


def test_export_markdown_contains_metrics_without_notes():
    markdown = export_report(_detail(), "markdown")
    assert markdown.startswith("# 阅读回顾报告 · 2026-W43")
    assert "| 本期阅读（独立文献） | 1 |" in markdown
    assert "已评分覆盖 | 1/1" in markdown
    assert "不含备注原文" in markdown


def test_export_json_round_trips_to_detail():
    detail = _detail()
    payload = json.loads(export_report(detail, "json"))
    assert ReportDetail.model_validate(payload).model_dump(mode="json") == detail.model_dump(
        mode="json"
    )
