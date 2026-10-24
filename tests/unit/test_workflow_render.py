"""run_body 与来源错误文案的渲染单测（纯字符串构造，不落库）。"""

from __future__ import annotations

from datetime import UTC, datetime

from papersteward.api.workflow_pages import run_body
from papersteward.core.entities import Run
from papersteward.core.enums import RunStatus
from papersteward.sources.contracts import describe_source_error, display_source_name

NOW = datetime(2026, 10, 24, 8, tzinfo=UTC)


def _run(
    *,
    status: RunStatus,
    run_type: str = "ui.sync",
    stats: dict | None = None,
    errors: dict | None = None,
) -> Run:
    return Run(
        id="r1",
        run_type=run_type,
        status=status,
        started_at=NOW,
        finished_at=None if status is RunStatus.RUNNING else NOW,
        stats_json=stats or {},
        errors_json=errors or {},
    )


def test_source_error_labels_cover_all_codes() -> None:
    assert describe_source_error("rate_limited") == "来源限流（请求太频繁）"
    assert describe_source_error("timeout") == "来源响应超时"
    assert describe_source_error("unknown_code") == "unknown_code"  # 未知值兜底原文
    assert display_source_name("arxiv") == "arXiv"
    assert display_source_name("semantic_scholar") == "Semantic Scholar"
    assert display_source_name("other") == "other"


def test_running_details_are_preserved_and_closed() -> None:
    stats = {
        "stage": "processing",
        "processing_total": 5,
        "processed": 1,
        "plans": [{"queries": [{"keywords": ["k"], "rationale": "r"}], "origin": "agent"}],
        "quality_flags": {"p1": ["low_confidence"]},
    }
    html = run_body(_run(status=RunStatus.RUNNING, stats=stats))
    # 运行中：两个 details 在轮询替换范围内，hx-preserve 保住用户展开状态，默认不展开。
    assert 'id="run-query-plans" hx-preserve>' in html
    assert 'id="run-quality-flags" hx-preserve>' in html
    assert "<details" in html and " open>" not in html
    assert 'hx-trigger="every 2s"' in html


def test_terminal_details_are_open_without_preserve() -> None:
    stats = {
        "plans": [{"queries": [{"keywords": ["k"], "rationale": "r"}], "origin": "agent"}],
        "quality_flags": {"p1": ["low_confidence"]},
        "ingest_run_id": "ui-ingest-abc",
        "request": {"kind": "sync", "source": "arxiv"},
    }
    html = run_body(_run(status=RunStatus.SUCCEEDED, stats=stats))
    # 终态：不再轮询，无需 preserve；默认展开让报告直接可读。
    assert 'id="run-query-plans" open>' in html
    assert 'id="run-quality-flags" open>' in html
    assert "hx-preserve" not in html
    # 来源运行报告可达（修复 1：报告不再无处可看）。
    assert 'href="/ui/runs/ui-ingest-abc">查看来源运行报告' in html


def test_ingest_run_renders_report_and_source_failures() -> None:
    stats = {
        "candidates": 12,
        "new": 9,
        "existing": 3,
        "deduplicated": 2,
        "filtered": [],
        "source_versions_added": 11,
        "sources": [
            {
                "source": "arxiv",
                "queries_succeeded": 2,
                "candidates": 12,
                "requests": 3,
                "cache_hits": 1,
            }
        ],
    }
    errors = {
        "sources": [
            {"source": "arxiv", "query_index": 1, "code": "rate_limited", "retryable": True}
        ]
    }
    html = run_body(_run(status=RunStatus.FAILED, run_type="ingest", stats=stats, errors=errors))
    assert "来源采集" in html  # LABELS 不再裸显英文 run_type
    assert "排队" in html and "采集" in html and "完成" in html  # 三步
    assert "候选 12 篇" in html and "arXiv：检索式成功 2 条" in html
    assert "来源失败明细" in html
    assert "arXiv 第 2 条检索式：来源限流（请求太频繁）（rate_limited）" in html


def test_collecting_phase_titles_are_honest() -> None:
    # queued 仅覆盖 worker 接手瞬间；规划与抓取各有如实标题。
    queued = run_body(_run(status=RunStatus.RUNNING, stats={"stage": "queued"}))
    assert "正在开始…" in queued
    planning = run_body(
        _run(
            status=RunStatus.RUNNING,
            stats={"stage": "collecting", "collecting_phase": "planning"},
        )
    )
    assert "正在规划检索式" in planning and "正在采集论文" not in planning
    fetching = run_body(
        _run(
            status=RunStatus.RUNNING,
            stats={"stage": "collecting", "collecting_phase": "fetching"},
        )
    )
    assert "正在采集论文" in fetching
    legacy = run_body(_run(status=RunStatus.RUNNING, stats={"stage": "collecting"}))
    assert "正在采集论文" in legacy  # 无相位的旧运行记录保持默认标题


def test_ingest_run_id_is_deterministic_and_unique_per_round() -> None:
    from papersteward.jobs.ui_flow import ingest_run_id

    first = ingest_run_id("run-a", 1)
    assert first.startswith("ui-ingest-") and len(first) == len("ui-ingest-") + 40
    assert ingest_run_id("run-a", 1) == first  # 同轮确定
    assert ingest_run_id("run-a", 2) != first  # 跨轮唯一
    assert ingest_run_id("run-b", 1) != first  # 跨挖掘唯一
