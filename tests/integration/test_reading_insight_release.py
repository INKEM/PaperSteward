"""R1-06 发布验收：全历史、导出边界、有效输入比较与恢复。"""

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_reading_insight_projection import (
    CUTOFF,
    import_content,
    seed_archive,
    seed_paper,
    system_content,
    week_request,
)
from test_reading_reports import seed_week, submit_review
from test_ui_flows import HEADERS, wait

from conftest import activate
from papersteward.core.entities import utc_now
from papersteward.delivery.contracts import Action, ImportReadPaperInput
from papersteward.jobs.ui_flow import ReviewParams
from papersteward.reading.actions import apply_action
from papersteward.reading.import_service import import_read_paper
from papersteward.reading.insights.assets import change_status, create_asset
from papersteward.reading.insights.contracts import AssetCreateInput, ReadTimeDeclaration
from papersteward.reading.insights.projection import build_projection
from papersteward.reading.insights.reports import prepare_report
from papersteward.storage import create_db_engine, create_session_factory, session_scope
from papersteward.storage.models import (
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingAssetSourceRow,
    ReadingReportRow,
    ReadingTimeRevisionRow,
)
from papersteward.storage.workspace import backup_workspace, clear_workspace, workspace_counts

PAST = datetime(2026, 10, 22, 2, tzinfo=UTC)
INSIGHT_TABLES = (
    "reading_time_revision",
    "reading_report",
    "reading_asset",
    "reading_asset_revision",
    "reading_asset_source",
    "reading_write_receipt",
)


def release_data(client):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        paper_id = import_read_paper(
            session,
            ImportReadPaperInput(
                title="发布样例",
                score=4,
                note="私人反馈正文",
                read_time=ReadTimeDeclaration(precision="month", month="2026-09"),
            ),
            now=PAST,
        )
        asset = create_asset(
            session,
            AssetCreateInput(
                kind="problem",
                title="私人问题标题",
                body="私人问题正文 <script>不可执行</script>",
                applicability="私人适用说明",
                sources=[{"paper_id": paper_id}],
                idempotency_key="release-asset",
            ),
            now=PAST,
        )
        change_status(
            session,
            asset.asset_id,
            "resolved",
            now=PAST + timedelta(hours=1),
            expected_revision=1,
            idempotency_key="release-status",
        )
    submit_review(client, "release-report", period="all", anchor_date=None)
    run = wait(client.app.state.ui_flows, "release-report")
    assert run.status.value == "succeeded"
    return factory, paper_id, asset.asset_id, run.stats_json["generated"]["report_id"]


def test_all_history_has_all_archive_facts_and_current_assets(client):
    _, _, _, _ = release_data(client)
    page = client.get("/ui/reading-insights?kind=all")
    assert page.status_code == 200
    for label in (
        "全部历史",
        "本周",
        "本月",
        "已保存回顾",
        "手工资产",
        "返回归档",
        "全历史数据完整性与来源",
        "仅知月份",
        "外部导入",
        "当前手工资产（1）",
        "私人问题标题",
        "已解决",
        "按阅读时期找回记录",
    ):
        assert label in page.text
    api = client.get("/reading-insights/overview?kind=all").json()
    assert api["metrics"]["asset_events"] == 2
    assert api["metrics"]["all_history_total"] == 1
    assert "2 次操作" in page.text


def test_exports_default_to_facts_and_full_copy_keeps_revisions(client):
    _, _, asset_id, report_id = release_data(client)
    page = client.get("/ui/reading-insights/export")
    assert "个人完整副本包含私人原文" in page.text
    assert 'value="facts" selected' in page.text
    for url in (
        "/ui/reading-insights/export/download",
        f"/reading-insights/reports/{report_id}/export?format=json",
        f"/ui/reading-insights/reports/{report_id}?format=markdown",
    ):
        exported = client.get(url)
        assert exported.status_code == 200
        assert "私人反馈正文" not in exported.text
        assert "私人问题正文" not in exported.text
        assert "私人问题标题" not in exported.text
    denied = client.get("/ui/reading-insights/export/download?scope=personal")
    assert denied.status_code == 400 and "勾选说明" in denied.text
    full = client.get("/ui/reading-insights/export/download?scope=personal&confirm_private=yes")
    assert full.status_code == 200 and "attachment" in full.headers["content-disposition"]
    payload = full.json()
    assert payload["assets"][0]["asset_id"] == asset_id
    assert [r["status"] for r in payload["assets"][0]["revisions"]] == ["open", "resolved"]
    assert len(payload["reading_time_revisions"]) == 1
    assert "私人反馈正文" in full.text and "私人适用说明" in full.text


def test_historical_report_status_ignores_future_but_revised_mode_sees_changes(client):
    activate(client)
    seed_week(client, imports=0)
    submit_review(client, "status-historical")
    historical = wait(client.app.state.ui_flows, "status-historical").stats_json["generated"][
        "report_id"
    ]
    submit_review(client, "status-revised", mode="revised_now")
    revised = wait(client.app.state.ui_flows, "status-revised").stats_json["generated"]["report_id"]
    factory = create_session_factory(client.app.state.engine)
    for report_id in (historical, revised):
        assert (
            client.get(f"/reading-insights/reports/{report_id}/input-status").json()["changed"]
            is False
        )
    # 当前新读发生在旧周期之外：两种模式均不把无关记录判成相关变化。
    with session_scope(factory) as session:
        current = utc_now() - timedelta(seconds=1)
        seed_paper(session, "unrelated-now", first_seen_at=current)
        seed_archive(
            session,
            "unrelated-now",
            archived_at=current,
            content=system_content(title="无关新阅读", completed_at=current),
        )
    for report_id in (historical, revised):
        assert (
            client.get(f"/reading-insights/reports/{report_id}/input-status").json()["changed"]
            is False
        )
    with session_scope(factory) as session:
        apply_action(
            session,
            "rp-sys-0",
            Action(request_id="release-score", kind="score", score=2),
            now=utc_now() - timedelta(seconds=1),
        )
    assert (
        client.get(f"/reading-insights/reports/{historical}/input-status").json()["changed"]
        is False
    )
    assert client.get(f"/reading-insights/reports/{revised}/input-status").json()["changed"] is True
    old = client.get(f"/reading-insights/reports/{historical}?format=json").json()
    assert old["content"]["readings"][0]["feedback"][-1]["score"] == 4
    # 无关的新系统阅读不属于旧周期，不使固定截止的旧报告失效。
    with session_scope(factory) as session:
        import_read_paper(
            session, ImportReadPaperInput(title="后来补录，无关", score=3), now=utc_now()
        )
    assert (
        client.get(f"/reading-insights/reports/{historical}/input-status").json()["changed"]
        is False
    )


def test_projection_cutoff_is_exclusive_and_report_uses_exact_collection_time(insights_factory):
    with session_scope(insights_factory) as session:
        for name, at in (("before", CUTOFF - timedelta(microseconds=1)), ("equal", CUTOFF)):
            seed_paper(session, name, first_seen_at=at)
            seed_archive(session, name, archived_at=at, content=import_content(title=name))
    with session_scope(insights_factory) as session:
        proj = build_projection(session, week_request(), collected_at=CUTOFF)
        assert [p.paper_id for p in proj.papers] == ["before"]
    with session_scope(insights_factory) as session:
        prepared = prepare_report(
            session,
            ReviewParams(period="week", anchor_date=date(2026, 10, 22)),
            collected_at=CUTOFF,
        )
        assert prepared.content.metrics.collected_at == CUTOFF
        assert prepared.content.metrics.all_history_total == 1


def test_create_error_keeps_full_form_and_keyboard_submission(client):
    activate(client)
    long_body = "<b>草稿</b>" * 100
    response = client.post(
        "/ui/reading-insights/assets",
        headers=HEADERS,
        data={
            "kind": "problem",
            "title": "保留标题",
            "body": long_body,
            "applicability": "适用草稿",
            "sources": "missing",
            "idempotency_key": "draft-keep",
        },
    )
    assert response.status_code == 409
    assert "&lt;b&gt;草稿&lt;/b&gt;" * 100 in response.text
    assert 'value="保留标题"' in response.text
    assert "适用草稿" in response.text and "<details open>" in response.text
    assert "<button>保存资产</button>" in response.text


@pytest.mark.parametrize("keep_model", [False, True])
def test_reset_backup_restores_all_insight_tables(client, tmp_path, keep_model):
    factory, _, _, _ = release_data(client)
    preview = client.get("/ui/settings/reset")
    assert "回顾报告 1 份" in preview.text and "资产修订 2 条" in preview.text
    with session_scope(factory) as session:
        before = workspace_counts(session)
    backup = backup_workspace(client.app.state.engine, tmp_path / "backups", now=utc_now())
    with session_scope(factory, immediate=True) as session:
        clear_workspace(session, keep_model=keep_model)
        counts = workspace_counts(session)
        assert all(counts[table] == 0 for table in INSIGHT_TABLES)
    restored = create_db_engine(f"sqlite+pysqlite:///{backup}")
    try:
        with session_scope(create_session_factory(restored)) as session:
            counts = workspace_counts(session)
            assert all(counts[table] == before[table] > 0 for table in INSIGHT_TABLES)
            assert session.scalar(select(func.count()).select_from(ReadingAssetRow)) == 1
            assert session.scalar(select(func.count()).select_from(ReadingAssetRevisionRow)) == 2
            assert session.scalar(select(func.count()).select_from(ReadingAssetSourceRow)) == 2
            assert session.scalar(select(func.count()).select_from(ReadingReportRow)) == 1
            assert session.scalar(select(func.count()).select_from(ReadingTimeRevisionRow)) == 1
    finally:
        restored.dispose()
    with sqlite3.connect(backup) as connection:
        body = connection.execute("SELECT body FROM reading_asset_revision LIMIT 1").fetchone()[0]
        assert "私人问题正文" in body


def test_private_report_export_and_page_delete_copies(client):
    _, _, asset_id, default_report = release_data(client)
    submit_review(client, "private-body-report", period="all", anchor_date=None, asset_bodies=True)
    report_id = wait(client.app.state.ui_flows, "private-body-report").stats_json["generated"][
        "report_id"
    ]
    report_page = client.get(f"/ui/reading-insights/reports/{report_id}")
    assert "保存时的私人正文副本" in report_page.text
    assert "&lt;script&gt;不可执行&lt;/script&gt;" in report_page.text
    assert (
        "私人问题正文"
        not in client.get(f"/reading-insights/reports/{report_id}/export?format=json").text
    )
    private = client.get(
        f"/ui/reading-insights/export/download?report_id={report_id}"
        "&scope=personal&confirm_private=yes"
    )
    assert private.status_code == 200 and "私人问题正文" in private.text
    # 选入正文的报告复选框必须属于删除表单，不能放在表单外无效。
    asset_page = client.get(f"/ui/reading-insights/assets/{asset_id}").text
    delete_form = asset_page.split(f"/assets/{asset_id}/delete", 1)[1].split("</form>", 1)[0]
    assert f'name="also_{report_id}"' in delete_form
    deleted = client.post(
        f"/ui/reading-insights/assets/{asset_id}/delete",
        headers=HEADERS,
        data={"confirmation": "删除资产", f"also_{report_id}": "1"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert client.get(f"/reading-insights/reports/{report_id}").status_code == 404
    assert client.get(f"/reading-insights/reports/{default_report}").status_code == 200
    assert "来源已移除" in client.get(f"/ui/reading-insights/reports/{default_report}").text


def test_sanitized_copy_strips_private_fields_and_keeps_original(client):
    """F06/R2-00C：另存脱敏版本清除私人标题与正文；原报告与哈希不动，幂等且同系列。"""
    from papersteward.reading.insights.reports import (
        list_series_reports,
        read_report,
        save_sanitized_report,
    )
    from papersteward.storage.insights import ReadingDerivedCopyRepository

    _, _, asset_id, default_report = release_data(client)
    with session_scope(create_session_factory(client.app.state.engine)) as session:
        original_before = session.get(ReadingReportRow, default_report)
        original_json = original_before.content_json
        original_hash = original_before.content_hash

        sanitized = save_sanitized_report(session, default_report, now=PAST + timedelta(hours=2))
        sanitized_id = sanitized.report_id

        # 原报告行逐字段不变。
        original_after = session.get(ReadingReportRow, default_report)
        assert original_after.content_json == original_json
        assert original_after.content_hash == original_hash

        # 脱敏副本无私人标题与正文；事实数字保持一致。
        assert [ref.title for ref in sanitized.content.assets] == [""]
        assert [ref.body for ref in sanitized.content.assets] == [None]
        assert sanitized.content.metrics.model_dump() == (
            read_report(session, default_report).content.metrics.model_dump()
        )

        # 幂等：再次另存沿用同一份副本。
        again = save_sanitized_report(session, default_report, now=PAST + timedelta(hours=3))
        assert again.report_id == sanitized_id

        # 派生谱系登记；脱敏副本自身没有私人字段登记。
        lineage = ReadingDerivedCopyRepository(session).lineage_for_report(sanitized_id)
        assert [(item.source_kind, item.source_id, item.field_kind) for item in lineage] == [
            ("report", default_report, "sanitized")
        ]

        # 同系列：脱敏版本与原版本互相可见、可比较。
        base = read_report(session, default_report)
        series_ids = {item.report_id for item in list_series_reports(session, base, limit=50)}
        assert {default_report, sanitized_id} <= series_ids

    # 页面：原报告提供「另存脱敏版本」；脱敏副本显示来源提示与占位标题。
    original_page = client.get(f"/ui/reading-insights/reports/{default_report}")
    assert "另存脱敏版本" in original_page.text
    assert "私人问题标题" in original_page.text
    sanitized_page = client.get(f"/ui/reading-insights/reports/{sanitized_id}")
    assert "的脱敏版本" in sanitized_page.text
    assert "私人标题已移除" in sanitized_page.text
    assert "私人问题标题" not in sanitized_page.text
    assert "另存脱敏版本" not in sanitized_page.text
    # 原 run 存在渲染链接；脱敏副本的合成 run 无记录时以纯文本说明。
    assert 'href="/ui/runs/release-run"' in original_page.text or "/ui/runs/" in original_page.text
    assert "无运行记录" in sanitized_page.text

    # JSON 入口幂等返回同一份副本；对脱敏副本再次另存被拒绝。
    api = client.post(f"/reading-insights/reports/{default_report}/sanitized")
    assert api.status_code == 201
    assert api.json()["report_id"] == sanitized_id
    refused = client.post(f"/reading-insights/reports/{sanitized_id}/sanitized")
    assert refused.status_code in (400, 409, 422)

    # 事实导出口径一致：两个副本都不含私人标题。
    for report_id in (default_report, sanitized_id):
        facts = client.get(f"/reading-insights/reports/{report_id}/export?format=json")
        assert facts.status_code == 200
        assert "私人问题标题" not in facts.text
