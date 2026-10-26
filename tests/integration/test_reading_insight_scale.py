"""全历史规模观察；不以执行时间设脆弱断言，固定查询形态与停止边界。"""

import time
import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert

from papersteward.jobs.ui_flow import ReviewParams
from papersteward.reading.insights.aggregate import build_overview
from papersteward.reading.insights.contracts import InsightPeriodRequest, InsightQueryRequest
from papersteward.reading.insights.projection import build_projection
from papersteward.reading.insights.reports import ReportPreparationCancelled, prepare_report
from papersteward.storage import session_scope
from papersteward.storage.delivery import content_hash
from papersteward.storage.models import ArchiveSnapshotRow, PaperRow, RatingRow

AT = datetime(2026, 10, 20, 2, tzinfo=UTC)


def seed_scale(factory, size):
    with session_scope(factory) as session:
        session.execute(
            insert(PaperRow),
            [
                {
                    "id": f"scale-{i:05d}",
                    "title": f"规模样例{i}",
                    "status": "archived",
                    "first_seen_at": AT,
                }
                for i in range(size)
            ],
        )
        content = {
            "metadata": {"title": "规模冻结标题"},
            "reading": {"completed_at": AT.isoformat()},
        }
        session.execute(
            insert(ArchiveSnapshotRow),
            [
                {
                    "paper_id": f"scale-{i:05d}",
                    "archived_at": AT,
                    "profile_version": "p1",
                    "taxonomy_version": "t1",
                    "config_version": "c1",
                    "content_json": content,
                    "content_hash": content_hash(content),
                }
                for i in range(size)
            ],
        )
        session.execute(
            insert(RatingRow),
            [
                {
                    "paper_id": f"scale-{i:05d}",
                    "created_at": AT + timedelta(hours=version),
                    "score": 5 - version,
                    "note": "规模反馈",
                }
                for i in range(size)
                for version in range(3)
            ],
        )


@pytest.mark.parametrize("size", [1000, 10000])
def test_full_history_scale_and_batch_cancellation(insights_factory, size, client):
    seed_scale(insights_factory, size)
    engine = insights_factory.kw["bind"]
    queries = []

    def count_query(*args):
        queries.append(args[2])

    query = InsightQueryRequest(period=InsightPeriodRequest(kind="all", timezone="Asia/Shanghai"))
    event.listen(engine, "before_cursor_execute", count_query)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with session_scope(insights_factory) as session:
            projection = build_projection(session, query, collected_at=AT + timedelta(days=10))
        overview = build_overview(projection)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        event.remove(engine, "before_cursor_execute", count_query)
    assert overview.metrics.readings_total == overview.metrics.all_history_total == size
    assert overview.metrics.feedback_changes.score_events == 2 * size
    assert len(projection.papers) == size
    assert len(queries) <= 4 * (size // 500) + 3
    page = client.get(f"/ui/reading-insights?kind=all&offset={size - 50}")
    assert page.status_code == 200
    assert f"阅读明细（{size}）" in page.text
    assert f"scale-{size - 1:05d}" in page.text
    assert "上一页" in page.text
    # 第二批开始前停止：验证 prepare 的回滚与无半份报告，不需读完万篇。
    checkpoints = []

    def stop():
        checkpoints.append(time.perf_counter())
        return len(checkpoints) >= 2

    stopped = time.perf_counter()
    with session_scope(insights_factory) as session:
        with pytest.raises(ReportPreparationCancelled):
            prepare_report(
                session,
                ReviewParams(period="all"),
                collected_at=AT + timedelta(days=10),
                should_abort=stop,
            )
        assert not session.in_transaction()
    stop_elapsed = time.perf_counter() - stopped
    assert len(checkpoints) == 2
    print(
        f"scale={size} queries={len(queries)} seconds={elapsed:.3f} "
        f"peak_mib={peak / 1024 / 1024:.2f} "
        f"stop_after_first_batch_seconds={stop_elapsed:.3f}"
    )
