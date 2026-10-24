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
