"""多工作区页面集成测试：创建、切换、删除、每工作区隔离与运行保护。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ui_flows import HEADERS, post, ready

from conftest import activate
from papersteward.config import Settings
from papersteward.core.entities import Run, utc_now
from papersteward.llm.runtime import CONFIG_KEY
from papersteward.storage import session_scope
from papersteward.storage.repositories import ConfigRepository, RunRepository


@pytest.fixture
def ws_client(migrated_engine, tmp_path: Path):
    """显式临时 data_dir 的客户端：新工作区目录不会落进仓库 var/。"""
    from papersteward.api import create_app

    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings=settings, engine=migrated_engine)) as test_client:
        yield test_client


def sub_workspace_id(client: TestClient) -> str:
    hub = client.app.state.workspaces
    return next(info.id for info in hub.workspaces() if info.id != "default")


def test_create_switches_and_gates_to_setup(ws_client):
    ready(ws_client)
    response = post(ws_client, "/ui/workspaces", {"name": "第二方向", "copy_model": "yes"})
    assert response.status_code == 200
    assert str(response.url).endswith("/ui/setup?created=1")
    assert "新工作区已创建并切换" in response.text
    ws_id = sub_workspace_id(ws_client)
    assert ws_client.cookies.get("ps_workspace") == ws_id

    # 新工作区未激活：读路径被门禁送去做首次设置（每工作区独立的 onboarding 生效）。
    queue = ws_client.get("/ui/queue", follow_redirects=False)
    assert queue.status_code == 303
    assert queue.headers["location"] == "/ui/setup"


def test_switch_back_restores_default_workspace(ws_client):
    ready(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "第二方向"})
    assert ws_client.get("/ui/queue", follow_redirects=False).status_code == 303

    back = post(ws_client, "/ui/workspaces/switch", {"workspace_id": "default"})
    assert back.status_code == 200
    assert str(back.url).endswith("/ui/today")
    assert ws_client.cookies.get("ps_workspace") == "default"
    assert ws_client.get("/ui/queue").status_code == 200

    page = ws_client.get("/ui/workspaces")
    assert "第二方向" in page.text
    assert "默认工作区" in page.text
    assert "创建于" in page.text


def test_duplicate_name_rejected_and_input_kept(ws_client):
    ready(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "同名"})
    post(ws_client, "/ui/workspaces/switch", {"workspace_id": "default"})
    invalid = post(ws_client, "/ui/workspaces", {"name": "同名"})
    assert invalid.status_code == 400
    assert "已有同名工作区" in invalid.text
    assert 'value="同名"' in invalid.text


def test_create_copies_model_connection(ws_client):
    ready(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "复制配置", "copy_model": "yes"})
    hub = ws_client.app.state.workspaces
    copied_id = sub_workspace_id(ws_client)
    with session_scope(hub.open(copied_id).session_factory) as session:
        assert ConfigRepository(session).latest(CONFIG_KEY) is not None

    post(ws_client, "/ui/workspaces/switch", {"workspace_id": "default"})
    post(ws_client, "/ui/workspaces", {"name": "不复制"})
    fresh_id = next(info.id for info in hub.workspaces() if info.id not in {"default", copied_id})
    with session_scope(hub.open(fresh_id).session_factory) as session:
        assert ConfigRepository(session).latest(CONFIG_KEY) is None


def test_switcher_fragment_and_shell(ws_client):
    ready(ws_client)
    shell = ws_client.get("/ui/today")
    assert 'id="workspace-switcher"' in shell.text
    assert "/ui/workspaces/switcher?fragment=1" in shell.text
    assert "工作区管理" in shell.text

    bare = ws_client.get("/ui/workspaces/switcher", follow_redirects=False)
    assert bare.status_code == 303

    fragment = ws_client.get("/ui/workspaces/switcher?fragment=1", headers={"hx-request": "true"})
    assert fragment.status_code == 200
    assert 'id="workspace-switcher"' in fragment.text
    assert "默认工作区" in fragment.text
    assert "/ui/workspaces/switch" in fragment.text


def test_delete_moves_directory_to_backups(ws_client, tmp_path):
    ready(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "待删除"})
    ws_id = sub_workspace_id(ws_client)

    preview = ws_client.get(f"/ui/workspaces/{ws_id}/delete")
    assert preview.status_code == 200
    assert "删除工作区" in preview.text
    assert "0 篇论文" in preview.text

    wrong = post(ws_client, f"/ui/workspaces/{ws_id}/delete", {"confirmation": "确认删除"})
    assert wrong.status_code == 400
    assert "请输入“删除工作区”" in wrong.text

    done = post(ws_client, f"/ui/workspaces/{ws_id}/delete", {"confirmation": "删除工作区"})
    assert done.status_code == 200
    assert "工作区已删除" in done.text
    hub = ws_client.app.state.workspaces
    assert ws_id not in {info.id for info in hub.workspaces()}
    backups = list((tmp_path / "backups").glob(f"deleted-*-{ws_id}"))
    assert len(backups) == 1 and backups[0].is_dir()
    # 删除的是当前工作区：身份落回默认，队列可用。
    assert ws_client.cookies.get("ps_workspace") == "default"
    assert ws_client.get("/ui/queue").status_code == 200


def test_delete_default_workspace_forbidden(ws_client):
    ready(ws_client)
    preview = ws_client.get("/ui/workspaces/default/delete", follow_redirects=False)
    assert preview.status_code == 400
    assert "重置工作区" in preview.text
    done = post(ws_client, "/ui/workspaces/default/delete", {"confirmation": "删除工作区"})
    assert done.status_code == 400


def test_delete_blocked_while_task_running(ws_client):
    ready(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "忙碌"})
    ws_id = sub_workspace_id(ws_client)
    hub = ws_client.app.state.workspaces
    with session_scope(hub.open(ws_id).session_factory) as session:
        RunRepository(session).add(Run("busy-ws", "ui.sync", started_at=utc_now()))
    blocked = post(ws_client, f"/ui/workspaces/{ws_id}/delete", {"confirmation": "删除工作区"})
    assert blocked.status_code == 409
    assert "有任务正在运行" in blocked.text
    assert ws_id in {info.id for info in hub.workspaces()}


def test_create_is_csrf_guarded(ws_client):
    activate(ws_client)
    response = ws_client.post(
        "/ui/workspaces",
        data={"name": "跨站"},
        headers={"origin": "https://evil.example", "content-type": HEADERS["content-type"]},
    )
    assert response.status_code == 409


def test_deleted_workspace_restores_insight_data_and_isolation(ws_client, tmp_path):
    from test_reading_reports import review_request
    from test_ui_flows import wait

    from conftest import activate_workspace
    from papersteward.delivery.contracts import ImportReadPaperInput
    from papersteward.reading.import_service import import_read_paper
    from papersteward.reading.insights.assets import create_asset
    from papersteward.reading.insights.contracts import AssetCreateInput, ReadTimeDeclaration
    from papersteward.storage.workspace import workspace_counts

    activate(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "阅读回顾恢复样例"})
    ws_id = sub_workspace_id(ws_client)
    hub = ws_client.app.state.workspaces
    runtime = hub.open(ws_id)
    activate_workspace(runtime.engine)
    directory = runtime.info.directory
    with session_scope(runtime.session_factory) as session:
        paper_id = import_read_paper(
            session,
            ImportReadPaperInput(
                title="仅在子工作区",
                score=5,
                read_time=ReadTimeDeclaration(precision="month", month="2026-09"),
            ),
            now=utc_now(),
        )
        asset = create_asset(
            session,
            AssetCreateInput(
                kind="problem",
                title="需恢复的问题",
                body="私人完整版本",
                sources=[{"paper_id": paper_id}],
                idempotency_key="restore-sub-asset",
            ),
            now=utc_now(),
        )
    runtime.ui_flows.submit(review_request("sub-review", period="all", anchor_date=None))
    run = wait(runtime.ui_flows, "sub-review")
    report_id = run.stats_json["generated"]["report_id"]
    preview = ws_client.get(f"/ui/workspaces/{ws_id}/delete")
    assert "回顾报告 1 份" in preview.text and "资产修订 1 条" in preview.text
    with session_scope(runtime.session_factory) as session:
        before = workspace_counts(session)
    removed = post(ws_client, f"/ui/workspaces/{ws_id}/delete", {"confirmation": "删除工作区"})
    assert removed.status_code == 200 and not directory.exists()
    # 当前落回默认：所有来源、报告和私人副本均不可串读。
    assert ws_client.get(f"/reading-insights/assets/{asset.asset_id}").status_code == 404
    assert ws_client.get(f"/reading-insights/reports/{report_id}/export").status_code == 404
    default_copy = ws_client.get(
        "/ui/reading-insights/export/download?scope=personal&confirm_private=yes"
    )
    assert "私人完整版本" not in default_copy.text
    backup = next((tmp_path / "backups").glob(f"deleted-*-{ws_id}"))
    backup.rename(directory)
    hub.registry.reconcile()
    restored = hub.open(ws_id)
    with session_scope(restored.session_factory) as session:
        after = workspace_counts(session)
        assert all(after[key] == value for key, value in before.items())
    post(ws_client, "/ui/workspaces/switch", {"workspace_id": ws_id})
    assert "需恢复的问题" in ws_client.get("/ui/reading-insights?kind=all").text
    assert (
        "私人完整版本"
        in ws_client.get(
            "/ui/reading-insights/export/download?scope=personal&confirm_private=yes"
        ).text
    )


def test_insights_same_ids_use_current_workspace(ws_client):
    from datetime import timedelta

    from test_reading_insight_projection import import_content, seed_archive, seed_paper

    from conftest import activate_workspace
    from papersteward.core.enums import RunStatus
    from papersteward.jobs.ui_flow import ReviewParams
    from papersteward.reading.insights.reports import prepare_report, save_report
    from papersteward.storage.delivery import content_hash
    from papersteward.storage.insights import ReadingAssetRepository, ReadingReportRepository
    from papersteward.storage.models import (
        ReadingAssetRevisionRow,
        ReadingAssetRow,
        ReadingAssetSourceRow,
    )

    activate(ws_client)
    post(ws_client, "/ui/workspaces", {"name": "相同ID的隔离工作区"})
    hub = ws_client.app.state.workspaces
    ws_id = sub_workspace_id(ws_client)
    second = hub.open(ws_id)
    activate_workspace(second.engine)
    at = utc_now() - timedelta(seconds=1)
    for runtime, label in ((hub.default(), "默认库"), (second, "第二库")):
        content = import_content(title=label + "归档")
        with session_scope(runtime.session_factory) as session:
            seed_paper(session, "same-paper", first_seen_at=at)
            seed_archive(session, "same-paper", archived_at=at, content=content)
            ReadingAssetRepository(session).create(
                ReadingAssetRow(
                    id="same-asset", kind="problem", idempotency_key="same-key", created_at=at
                ),
                ReadingAssetRevisionRow(
                    asset_id="same-asset",
                    revision=1,
                    recorded_at=at,
                    title=label + "问题",
                    body=label + "私人正文",
                    status="open",
                ),
                [
                    ReadingAssetSourceRow(
                        id="same-source",
                        asset_id="same-asset",
                        revision=1,
                        paper_id="same-paper",
                        archive_hash=content_hash(content),
                    )
                ],
            )
        with session_scope(runtime.session_factory) as session:
            prepared = prepare_report(session, ReviewParams(period="all"), collected_at=utc_now())
        with session_scope(runtime.session_factory, immediate=True) as session:
            RunRepository(session).add(
                Run(
                    "same-run",
                    "ui.review",
                    started_at=at,
                    stats_json={"generated": {"report_id": "same-report"}},
                )
            )
            row = save_report(session, prepared, run_id="same-run", now=utc_now())
            # 报告身份在生产中不可变；样例改名时显式重建相应上下文与副本登记以保持外键有效。
            from uuid import uuid4

            from sqlalchemy import select

            from papersteward.storage.models import (
                ReadingDerivedCopyRow,
                ReadingReportContextRow,
            )

            context_row = session.get(ReadingReportContextRow, row.id)
            context_payload, context_series = context_row.context_json, context_row.series_key
            session.delete(context_row)
            copy_rows = session.scalars(
                select(ReadingDerivedCopyRow).where(ReadingDerivedCopyRow.derived_id == row.id)
            ).all()
            copy_payloads = [
                {
                    "source_kind": item.source_kind,
                    "source_id": item.source_id,
                    "source_revision": item.source_revision,
                    "field_kind": item.field_kind,
                    "created_at": item.created_at,
                }
                for item in copy_rows
            ]
            for item in copy_rows:
                session.delete(item)
            session.flush()
            row.id = "same-report"
            session.flush()
            ReadingReportRepository(session).save_context(row.id, context_payload, context_series)
            for payload in copy_payloads:
                session.add(
                    ReadingDerivedCopyRow(
                        id=uuid4().hex,
                        derived_kind="report",
                        derived_id=row.id,
                        **payload,
                    )
                )
            RunRepository(session).transition(
                "same-run", RunStatus.SUCCEEDED, finished_at=utc_now()
            )
    for target, label, other in (("default", "默认库", "第二库"), (ws_id, "第二库", "默认库")):
        post(ws_client, "/ui/workspaces/switch", {"workspace_id": target})
        overview = ws_client.get("/reading-insights/overview?kind=all")
        assert label + "归档" in overview.text and other + "归档" not in overview.text
        asset = ws_client.get("/reading-insights/assets/same-asset").json()
        assert asset["revision"]["body"] == label + "私人正文"
        assert asset["revision"]["sources"][0]["archive_hash"] == content_hash(
            import_content(title=label + "归档")
        )
        exported = ws_client.get("/reading-insights/reports/same-report/export?format=json")
        assert label + "归档" in exported.text and other + "归档" not in exported.text
        assert (
            ws_client.get("/ui/runs/same-run", follow_redirects=False)
            .headers["location"]
            .endswith("/same-report")
        )
        personal = ws_client.get(
            "/ui/reading-insights/export/download?scope=personal&confirm_private=yes"
        )
        assert label + "私人正文" in personal.text and other + "私人正文" not in personal.text
