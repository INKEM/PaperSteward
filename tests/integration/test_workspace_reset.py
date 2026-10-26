import sqlite3

import pytest
from sqlalchemy import func, select
from test_priority_queue import seed
from test_ui_flows import post, ready

from papersteward.core.entities import Run, utc_now
from papersteward.storage import session_scope
from papersteward.storage.models import ConfigRow, PaperRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import RunRepository


def test_confirmed_results_are_visible_in_setup_and_settings(client):
    ready(client)
    for url in ("/ui/setup", "/ui/settings"):
        page = client.get(url)
        assert page.status_code == 200
        for expected in ("已确认的研究方向档案", "核心问题", "子方向", "已确认的分类标签", "纳入"):
            assert expected in page.text
        assert "sk-test" not in page.text
    assert "重置工作区（清空数据）" in client.get("/ui/settings").text


@pytest.mark.parametrize("keep_model", [True, False])
def test_reset_backs_up_and_restarts_setup(client, tmp_path, keep_model):
    manager = ready(client)
    runtime = client.app.state.workspaces.default()
    runtime.settings = runtime.settings.model_copy(update={"data_dir": tmp_path})
    with session_scope(manager.factory) as session:
        seed(session)
    invalid = post(client, "/ui/settings/reset", {"confirmation": "wrong"})
    assert invalid.status_code == 400
    assert "输入“重置工作区”" in client.get("/ui/settings/reset").text
    values = {"confirmation": "重置工作区"}
    if keep_model:
        values["keep_model"] = "yes"
    response = post(client, "/ui/settings/reset", values)
    assert response.status_code == 200
    assert str(response.url).endswith("/ui/setup?reset=1")
    with session_scope(manager.factory) as session:
        assert get_onboarding(session).stage.value == "setup"
        assert session.scalar(select(func.count()).select_from(PaperRow)) == 0
        rows = list(session.scalars(select(ConfigRow)))
        assert len(rows) == (1 if keep_model else 0)
        assert all(row.key == "llm_config" for row in rows)
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT count(*) FROM paper").fetchone()[0] == 1
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0024"
    # The reset workspace can be activated and used again without version conflicts.
    from conftest import activate

    activate(client)
    assert client.get("/ui/queue").status_code == 200


def test_reset_is_blocked_during_task_and_csrf_guarded(client, tmp_path):
    manager = ready(client)
    runtime = client.app.state.workspaces.default()
    runtime.settings = runtime.settings.model_copy(update={"data_dir": tmp_path})
    with session_scope(manager.factory) as session:
        RunRepository(session).add(Run("busy-reset", "ui.sync", started_at=utc_now()))
    response = post(client, "/ui/settings/reset", {"confirmation": "重置工作区"})
    assert response.status_code == 409
    assert "有任务正在运行" in response.text
    assert not (tmp_path / "backups").exists()
    assert (
        client.post(
            "/ui/settings/reset",
            data={"confirmation": "重置工作区"},
            headers={"origin": "https://evil.example"},
        ).status_code
        == 409
    )


def test_backup_failure_keeps_original_data(client, tmp_path, monkeypatch):
    manager = ready(client)
    runtime = client.app.state.workspaces.default()
    runtime.settings = runtime.settings.model_copy(update={"data_dir": tmp_path})
    with session_scope(manager.factory) as session:
        seed(session)

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("papersteward.storage.workspace.backup_workspace", fail)
    assert post(client, "/ui/settings/reset", {"confirmation": "重置工作区"}).status_code == 400
    with session_scope(manager.factory) as session:
        assert get_onboarding(session).stage.value == "active"
        assert session.get(PaperRow, "p") is not None
