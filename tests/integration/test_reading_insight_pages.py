"""阅读回顾页面集成测试：详情页阅读时间、补记表单与事实概览页（R1-02/03）。"""

from datetime import UTC, date, datetime, timedelta

from test_reading_insight_projection import (
    import_content,
    seed_archive,
    seed_paper,
    system_content,
)
from test_ui_flows import HEADERS

from conftest import activate
from papersteward.core.entities import ReadingTimeRevision, utc_now
from papersteward.core.enums import ReadingTimeBasis, TimePrecision
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.reading.import_service import import_read_paper
from papersteward.reading.insights.contracts import ReadTimeDeclaration
from papersteward.reading.insights.timeline import record_reading_time
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.insights import ReadingTimeRevisionRepository

NOW2 = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def _import_paper(client, title: str) -> str:
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        return import_read_paper(
            session,
            ImportReadPaperInput.model_validate({"title": title, "score": 4}),
            now=NOW2,
        )


def _post_read_time(client, paper_id: str, data: dict[str, str]):
    return client.post(
        f"/ui/reading-insights/papers/{paper_id}/read-time",
        data={
            "expected_revision": "0",
            "idempotency_key": "page-key-1",
            **data,
        },
        headers=HEADERS,
    )


def test_import_detail_shows_archive_and_unknown_reading_time(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "页面导入论文")
    detail = client.get(f"/ui/papers/{paper_id}")
    assert detail.status_code == 200
    # 归档时间与阅读时间分开呈现；导入未声明时如实显示未记录（文案修正）。
    assert "归档于" in detail.text and "阅读时间" in detail.text
    assert "未记录" in detail.text
    assert "完成于" not in detail.text
    assert "记录阅读时间" in detail.text


def test_read_time_form_success_updates_detail(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "表单导入论文")
    response = _post_read_time(
        client,
        paper_id,
        {"precision": "day", "day": "2026-10-20", "month": ""},
    )
    assert response.status_code == 200
    assert "已记录阅读时间" in response.text
    assert "2026-10-20" in response.text
    assert "阅读时间修订记录" in response.text

    detail = client.get(f"/ui/papers/{paper_id}")
    assert "2026-10-20" in detail.text
    assert "（系统记录）" not in detail.text


def test_read_time_form_conflict_preserves_input(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "冲突导入论文")
    first = _post_read_time(
        client, paper_id, {"precision": "day", "day": "2026-10-20", "month": ""}
    )
    assert first.status_code == 200
    stale = _post_read_time(
        client, paper_id, {"precision": "day", "day": "2026-10-21", "month": ""}
    )
    assert stale.status_code == 409
    assert "已保留你的输入" in stale.text
    # 保留用户已填输入（X-01）。
    assert 'name="day" value="2026-10-21"' in stale.text


def test_read_time_form_rejects_cross_site(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "跨站导入论文")
    response = client.post(
        f"/ui/reading-insights/papers/{paper_id}/read-time",
        data={"precision": "unknown", "expected_revision": "0"},
    )
    assert response.status_code == 409


def test_read_time_form_rejects_future_date(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "未来日期论文")
    response = _post_read_time(
        client,
        paper_id,
        {"precision": "day", "day": "2027-01-01", "month": ""},
    )
    assert response.status_code == 400
    assert "阅读日期不能晚于今天" in response.text


def test_read_time_form_unknown_clears_label(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "清除导入论文")
    _post_read_time(client, paper_id, {"precision": "day", "day": "2026-10-20", "month": ""})
    cleared = _post_read_time(
        client,
        paper_id,
        {
            "precision": "unknown",
            "day": "",
            "month": "",
            "expected_revision": "1",
            "idempotency_key": "page-key-2",
        },
    )
    assert cleared.status_code == 200
    assert "未记录" in cleared.text


def test_archive_list_labels_import_copy(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "列表导入论文")
    archive = client.get("/ui/archive")
    assert archive.status_code == 200
    assert "导入于" in archive.text
    assert f'href="/ui/papers/{paper_id}"' in archive.text


# —— R1-03：事实概览页 ————————————————————————————————————————

WEEK_ANCHOR = "2026-10-22"  # 上海时区 2026-10-19..25
NOW3 = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)  # 声明记录于周期内，as_recorded 可见


def seed_week_fixtures(client, *, imports: int = 50, system: int = 2) -> None:
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        for index in range(system):
            done = datetime(2026, 10, 20 + index, 2, tzinfo=UTC)
            paper_id = f"ov-sys-{index}"
            seed_paper(session, paper_id, first_seen_at=done)
            seed_archive(
                session,
                paper_id,
                archived_at=done,
                content=system_content(title=f"系统阅读{index}", completed_at=done),
                rating_col=4,
                note_col="初始",
                ratings=[(done, 4, "初始")],
            )
        for index in range(imports):
            archived = datetime(2026, 10, 19, 0, tzinfo=UTC) + timedelta(hours=index)
            paper_id = f"ov-imp-{index:02d}"
            seed_paper(session, paper_id, first_seen_at=archived)
            seed_archive(
                session,
                paper_id,
                archived_at=archived,
                content=import_content(title=f"补录{index}"),
                rating_col=3,
                ratings=[(archived, 3, None)],
            )


def test_week_overview_counts_readings_and_backfills_separately(client, migrated_engine):
    activate(client)
    seed_week_fixtures(client)
    page = client.get(f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}")
    assert page.status_code == 200
    assert "实时概览，尚未保存" in page.text
    assert "阅读明细（2）" in page.text
    assert "补录明细（50）" in page.text
    assert "未定阅读日期（50）" in page.text
    # 不把 52 当阅读量；全部历史为 52。
    assert "阅读明细（52）" not in page.text
    assert "全部历史（当前范围）" in page.text
    assert '<td data-label="数值">52</td>' in page.text
    # 知识截止与采集时刻明示。
    assert "知识截止" in page.text and "采集时刻" in page.text


def test_overview_json_matches_page_counts(client, migrated_engine):
    activate(client)
    seed_week_fixtures(client, imports=3, system=2)
    params = f"kind=week&anchor_date={WEEK_ANCHOR}"
    api = client.get(f"/reading-insights/overview?{params}")
    assert api.status_code == 200
    body = api.json()
    assert body["metrics"]["readings_total"] == 2
    assert body["metrics"]["backfills_total"] == 3
    assert body["metrics"]["unassigned_total"] == 3
    assert body["metrics"]["all_history_total"] == 5
    page = client.get(f"/ui/reading-insights?{params}")
    assert "阅读明细（2）" in page.text and "补录明细（3）" in page.text


def test_overview_json_requires_activation(client, migrated_engine):
    response = client.get("/reading-insights/overview")
    assert response.status_code == 409


def test_overview_page_requires_activation(client, migrated_engine):
    response = client.get("/ui/reading-insights", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/setup"


def test_overview_empty_state(client, migrated_engine):
    activate(client)
    page = client.get("/ui/reading-insights")
    assert page.status_code == 200
    assert "还没有可回顾的归档" in page.text
    assert "暂无" in page.text


def test_overview_tag_filter_narrows(client, migrated_engine):
    activate(client)
    seed_week_fixtures(client, imports=2, system=1)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        archived = datetime(2026, 10, 21, 3, tzinfo=UTC)
        seed_paper(session, "ov-other-tag", first_seen_at=archived)
        seed_archive(
            session,
            "ov-other-tag",
            archived_at=archived,
            content=system_content(title="其他标签", completed_at=archived),
            rating_col=3,
            primary_tag="T09",
            ratings=[(archived, 3, None)],
        )
    base = client.get(f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}")
    assert "全部历史（当前范围）" in base.text
    assert '<td data-label="数值">4</td>' in base.text
    filtered = client.get(f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}&tag=T09")
    assert '<td data-label="数值">1</td>' in filtered.text
    assert (
        "当前筛选范围内没有归档记录"
        in client.get(f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}&tag=NOPE").text
    )


def test_overview_declared_import_overlaps_reading_and_backfill(client, migrated_engine):
    activate(client)
    seed_week_fixtures(client, imports=1, system=1)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        record_reading_time(
            session,
            "ov-imp-00",
            ReadTimeDeclaration.model_validate({"precision": "day", "day": "2026-10-20"}),
            now=NOW3,
            expected_revision=0,
        )
    page = client.get(f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}")
    # 声明本周读完的导入：阅读 +1，补录仍计 1，二者可能重叠。
    assert "阅读明细（2）" in page.text
    assert "补录明细（1）" in page.text


def test_overview_revised_now_shows_backfill_marker(client, migrated_engine):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    # 目标周取真实时钟的过去周（2026-09-21..27）；「现在」归档必然晚于周期结束。
    past_anchor = "2026-09-22"
    late = utc_now()
    with session_scope(factory) as session:
        seed_paper(session, "ov-late", first_seen_at=late)
        seed_archive(
            session,
            "ov-late",
            archived_at=late,
            content=import_content(title="后补导入"),
            rating_col=4,
            ratings=[(late, 4, None)],
        )
        ReadingTimeRevisionRepository(session).add(
            ReadingTimeRevision(
                paper_id="ov-late",
                revision=1,
                recorded_at=late,
                precision=TimePrecision.DAY,
                basis=ReadingTimeBasis.DECLARED,
                day=date(2026, 9, 24),
                idempotency_key="late-1",
            )
        )
    as_recorded = client.get(
        f"/ui/reading-insights?kind=week&anchor_date={past_anchor}&mode=as_recorded"
    )
    assert "阅读明细（0）" in as_recorded.text
    revised = client.get(
        f"/ui/reading-insights?kind=week&anchor_date={past_anchor}&mode=revised_now"
    )
    assert "阅读明细（1）" in revised.text
    assert "含后补 1" in revised.text
    assert "现在重新回顾" in revised.text


def test_overview_pagination_keeps_metrics_and_filters(client, migrated_engine):
    activate(client)
    seed_week_fixtures(client, imports=51, system=1)
    first = client.get(
        f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}&section=backfills"
    )
    assert "补录明细（51）" in first.text
    assert "上一页" not in first.text and "下一页" in first.text
    second = client.get(
        f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}&section=backfills&offset=50"
    )
    assert "补录明细（51）" in second.text  # 分页不改变指标总数
    assert "上一页" in second.text
    assert "kind=week" in second.text and "anchor_date=" in second.text


def test_overview_feedback_changes_section(client, migrated_engine):
    activate(client)
    seed_week_fixtures(client, imports=0, system=1)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        from papersteward.delivery.contracts import Action
        from papersteward.reading.actions import apply_action

        apply_action(
            session,
            "ov-sys-0",
            Action(request_id="rev-1", kind="score", score=2),
            now=datetime(2026, 10, 22, 3, tzinfo=UTC),
        )
    page = client.get(f"/ui/reading-insights?kind=week&anchor_date={WEEK_ANCHOR}&section=changes")
    assert "反馈修订（1）" in page.text
    assert "4 → 2" in page.text
    assert "评分 1 次" in page.text
    assert "涉及 1 篇" in page.text


def test_overview_invalid_params_show_error(client, migrated_engine):
    activate(client)
    page = client.get("/ui/reading-insights?kind=bogus")
    assert page.status_code == 400
    assert "周期类型无效" in page.text


def test_archive_page_links_to_overview(client, migrated_engine):
    activate(client)
    archive = client.get("/ui/archive")
    assert 'href="/ui/reading-insights"' in archive.text
    assert "阅读回顾" in archive.text
