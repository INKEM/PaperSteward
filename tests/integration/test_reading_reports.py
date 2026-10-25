"""阅读回顾报告的任务、快照、导出与删除集成验证（R1-04）。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_reading_insight_projection import (
    import_content,
    seed_archive,
    seed_paper,
    system_content,
)
from test_ui_flows import wait

from conftest import activate
from papersteward.core.enums import RunStatus
from papersteward.delivery.contracts import Action
from papersteward.errors import ConflictError
from papersteward.jobs.ui_flow import FlowRequest, ReviewParams, UIFlowManager
from papersteward.reading.actions import apply_action
from papersteward.reading.insights.reports import list_reports, read_report
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.models import PaperRow, RunRow
from papersteward.storage.repositories import RunRepository

NOW4 = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
ANCHOR = "2026-10-22"


def review_params(**overrides: object) -> ReviewParams:
    payload: dict[str, object] = {"period": "week", "anchor_date": ANCHOR}
    payload.update(overrides)
    return ReviewParams.model_validate(payload)


def review_request(run_id: str, **overrides: object) -> FlowRequest:
    return FlowRequest(run_id=run_id, kind="review", review=review_params(**overrides))


def seed_week(client, *, system: int = 1, imports: int = 1) -> None:
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        for index in range(system):
            done = datetime(2026, 10, 20 + index, 2, tzinfo=UTC)
            paper_id = f"rp-sys-{index}"
            seed_paper(session, paper_id, first_seen_at=done)
            seed_archive(
                session,
                paper_id,
                archived_at=done,
                content=system_content(title=f"系统阅读{index}", completed_at=done),
                rating_col=4,
                note_col="初始备注",
                ratings=[(done, 4, "初始备注")],
            )
        for index in range(imports):
            archived = datetime(2026, 10, 21, 1, tzinfo=UTC) + timedelta(hours=index)
            paper_id = f"rp-imp-{index}"
            seed_paper(session, paper_id, first_seen_at=archived)
            seed_archive(
                session,
                paper_id,
                archived_at=archived,
                content=import_content(title=f"补录{index}"),
                rating_col=3,
                ratings=[(archived, 3, None)],
            )


def submit_review(client, run_id: str, **overrides: object) -> str:
    manager: UIFlowManager = client.app.state.ui_flows
    manager.submit(review_request(run_id, **overrides))
    return run_id


def test_report_generation_snapshot_export_and_run_jump(client, migrated_engine):
    activate(client)
    seed_week(client)
    submit_review(client, "rp-gen-1")
    report = wait(client.app.state.ui_flows, "rp-gen-1")
    assert report.status is RunStatus.SUCCEEDED
    report_id = report.stats_json["generated"]["report_id"]

    detail = client.get("/ui/runs/rp-gen-1", follow_redirects=False)
    assert detail.status_code == 303
    assert detail.headers["location"] == f"/ui/reading-insights/reports/{report_id}"

    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        saved = read_report(session, report_id)
    assert saved.content.contract == "reading-insights.report/1"
    assert saved.content.metrics.readings_total == 1
    assert saved.content.metrics.backfills_total == 1
    # 冻结正文不含备注原文。
    assert "初始备注" not in json.dumps(saved.content.model_dump(mode="json"), ensure_ascii=False)

    markdown = client.get(f"/reading-insights/reports/{report_id}/export?format=markdown")
    assert markdown.status_code == 200
    assert "阅读回顾报告 · 2026-W43" in markdown.text
    assert "| 本期阅读（独立文献） | 1 |" in markdown.text
    payload = client.get(f"/reading-insights/reports/{report_id}/export?format=json")
    assert json.loads(payload.text)["content"]["metrics"]["readings_total"] == 1

    # 报告列表页与详情页（冻结声明 + 指标来自快照）。
    listing = client.get("/ui/reading-insights/reports")
    assert listing.status_code == 200 and "2026-W43" in listing.text
    page = client.get(f"/ui/reading-insights/reports/{report_id}")
    assert "已保存的报告快照" in page.text
    assert '<td data-label="数值">1</td>' in page.text


def test_old_report_and_export_unchanged_after_mutation(client, migrated_engine):
    activate(client)
    seed_week(client)
    submit_review(client, "rp-old")
    wait(client.app.state.ui_flows, "rp-old")
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    old_id = listing["items"][0].report_id
    old_export = client.get(f"/reading-insights/reports/{old_id}/export?format=markdown").text
    old_detail = client.get(f"/reading-insights/reports/{old_id}?format=json").text

    with session_scope(factory) as session:
        apply_action(
            session,
            "rp-sys-0",
            Action(request_id="rp-rev-1", kind="score", score=2),
            now=datetime(2026, 10, 27, 3, tzinfo=UTC),
        )
        session.get(PaperRow, "rp-sys-0").title = "被修改的当前标题"
    from papersteward.reading.insights.contracts import ReadTimeDeclaration
    from papersteward.reading.insights.timeline import record_reading_time

    with session_scope(factory) as session:
        # 声明记录于周期内（截止前可见）：旧报告不含它，新报告体现变化。
        record_reading_time(
            session,
            "rp-imp-0",
            ReadTimeDeclaration.model_validate({"precision": "day", "day": "2026-10-21"}),
            now=datetime(2026, 10, 23, 3, tzinfo=UTC),
            expected_revision=0,
        )

    assert (
        client.get(f"/reading-insights/reports/{old_id}/export?format=markdown").text == old_export
    )
    assert client.get(f"/reading-insights/reports/{old_id}?format=json").text == old_detail

    submit_review(client, "rp-new")
    wait(client.app.state.ui_flows, "rp-new")
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    assert listing["total"] == 2
    new_id = next(item.report_id for item in listing["items"] if item.report_id != old_id)
    new_payload = json.loads(client.get(f"/reading-insights/reports/{new_id}?format=json").text)
    # 新报告体现变化：声明日期让补录进入本期阅读（旧报告里它是未定库存）。
    assert new_payload["content"]["metrics"]["readings_total"] == 2
    assert new_payload["content"]["metrics"]["unassigned_total"] == 0


def test_same_run_id_retry_creates_single_report(client, migrated_engine):
    activate(client)
    seed_week(client)
    manager = client.app.state.ui_flows
    manager.submit(review_request("rp-retry"))
    wait(manager, "rp-retry")
    manager.submit(review_request("rp-retry"))
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    assert listing["total"] == 1


def test_fingerprint_dedupe_reuses_snapshot_with_original_cutoff(client, migrated_engine):
    activate(client)
    seed_week(client)
    manager = client.app.state.ui_flows
    submit_review(client, "rp-first")
    first = wait(manager, "rp-first")
    first_id = first.stats_json["generated"]["report_id"]
    submit_review(client, "rp-second")
    second = wait(manager, "rp-second")
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    assert listing["total"] == 1
    assert second.stats_json["generated"]["report_id"] == first_id
    assert second.stats_json["deduplicated"] is True
    with session_scope(factory) as session:
        saved = read_report(session, first_id)
        assert saved.run_id == "rp-first"  # 保留原截止与原归属运行


def test_cancel_during_preparation_leaves_no_report(client, migrated_engine, monkeypatch):
    import threading

    from papersteward.reading.insights import reports as reports_module

    activate(client)
    seed_week(client)
    manager = client.app.state.ui_flows
    entered, release = threading.Event(), threading.Event()
    original = reports_module.prepare_report

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr("papersteward.jobs.ui_flow.prepare_report", blocked)
    try:
        manager.submit(review_request("rp-cancel"))
        assert entered.wait(2)
        cancelled = client.post(
            "/ui/runs/rp-cancel/cancel",
            data={},
            headers={
                "origin": "http://testserver",
                "content-type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        assert cancelled.status_code == 303
    finally:
        release.set()
    report = wait(manager, "rp-cancel")
    assert report.status is RunStatus.CANCELLED
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    assert listing["total"] == 0  # 无半成品报告


def test_restart_recovers_running_review_as_failed(client, migrated_engine):
    from papersteward.core.entities import Run, utc_now

    activate(client)
    seed_week(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        RunRepository(session).add(
            Run(
                id="rp-dead",
                run_type="ui.review",
                started_at=utc_now(),
                stats_json={
                    "request": {
                        "run_id": "rp-dead",
                        "kind": "review",
                        "review": {"period": "week", "anchor_date": ANCHOR},
                    }
                },
            )
        )
    # 重建 manager 触发 recover()：running 的 ui.review 标记为失败且可重试。
    UIFlowManager(factory, client.app.state.ui_flows.settings)
    with session_scope(factory) as session:
        row = session.get(RunRow, "rp-dead")
        assert row is not None and row.status == "failed"
        assert row.errors_json["errors"][0]["code"] == "interrupted"


def test_delete_report_only_removes_snapshot(client, migrated_engine):
    activate(client)
    seed_week(client)
    submit_review(client, "rp-del")
    wait(client.app.state.ui_flows, "rp-del")
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    report_id = listing["items"][0].report_id

    deleted = client.delete(f"/reading-insights/reports/{report_id}")
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    missing = client.delete(f"/reading-insights/reports/{report_id}")
    assert missing.status_code == 404
    assert client.get(f"/reading-insights/reports/{report_id}").status_code == 404
    with session_scope(factory) as session:
        from papersteward.storage.models import ArchiveSnapshotRow, RatingRow

        assert session.scalars(select(ArchiveSnapshotRow.paper_id)).all() != []
        assert session.scalars(select(RatingRow.paper_id)).all() != []


def test_report_generation_without_llm_config(client, migrated_engine):
    # 仅激活工作区（不配置模型）：回顾报告不依赖任何模型调用。
    activate(client)
    seed_week(client)
    submit_review(client, "rp-nollm")
    report = wait(client.app.state.ui_flows, "rp-nollm")
    assert report.status is RunStatus.SUCCEEDED


def test_review_blocked_by_running_task(client, migrated_engine):
    from papersteward.core.entities import Run, utc_now

    activate(client)
    seed_week(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        RunRepository(session).add(Run(id="rp-busy", run_type="ui.sync", started_at=utc_now()))
    manager = client.app.state.ui_flows
    with pytest.raises(ConflictError):
        manager.submit(review_request("rp-blocked"))


def test_overview_page_offers_report_entry(client, migrated_engine):
    activate(client)
    seed_week(client)
    page = client.get("/ui/reading-insights?kind=week&anchor_date=" + ANCHOR)
    assert "把当前筛选保存为回顾报告" in page.text
    assert 'action="/ui/reading-insights/reports"' in page.text


def test_report_detail_compare_and_delete_page(client, migrated_engine):
    activate(client)
    seed_week(client)
    manager = client.app.state.ui_flows
    submit_review(client, "rp-cmp-1")
    wait(manager, "rp-cmp-1")
    from papersteward.reading.insights.contracts import ReadTimeDeclaration
    from papersteward.reading.insights.timeline import record_reading_time

    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        record_reading_time(
            session,
            "rp-imp-0",
            ReadTimeDeclaration.model_validate({"precision": "day", "day": "2026-10-21"}),
            now=datetime(2026, 10, 23, 3, tzinfo=UTC),
            expected_revision=0,
        )
    submit_review(client, "rp-cmp-2")
    wait(manager, "rp-cmp-2")
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    ids = {item.run_id: item.report_id for item in listing["items"]}
    older_id = ids["rp-cmp-1"]
    newer_id = ids["rp-cmp-2"]
    page = client.get(f"/ui/reading-insights/reports/{older_id}?compare={newer_id}")
    assert "事实差异" in page.text
    assert "同系列版本" in page.text
    assert "删除此报告" in page.text

    # 页面删除：确认短语错误保留在错误页；正确短语删除成功。
    wrong = client.post(
        f"/ui/reading-insights/reports/{older_id}/delete",
        data={"confirmation": "删掉"},
        headers={
            "origin": "http://testserver",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert wrong.status_code == 400
    gone = client.post(
        f"/ui/reading-insights/reports/{older_id}/delete",
        data={"confirmation": "删除报告"},
        headers={
            "origin": "http://testserver",
            "content-type": "application/x-www-form-urlencoded",
        },
        follow_redirects=False,
    )
    assert gone.status_code == 303
    assert gone.headers["location"] == "/ui/reading-insights/reports"
