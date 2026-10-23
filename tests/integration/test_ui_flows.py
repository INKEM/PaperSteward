from __future__ import annotations

import time

from conftest import activate
from papersteward.config import get_settings
from papersteward.core.entities import Paper, Run, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.errors import InvalidRequestError
from papersteward.jobs.ui_flow import UIFlowManager
from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.repositories import PaperRepository, RunRepository

HEADERS = {"origin": "http://testserver", "content-type": "application/x-www-form-urlencoded"}


def ready(client):
    # 先激活再保存模型配置：配置审计事件使用真实时钟，必须晚于固定 NOW 的激活事件。
    activate(client)
    engine = client.app.state.engine
    with session_scope(create_session_factory(engine)) as session:
        save_llm_config(
            session,
            LLMSettingsPayload(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model="test-model",
                consent=True,
            ),
            get_settings(),
        )
    manager = client.app.state.ui_flows
    manager.settings = get_settings()
    return manager


def wait(manager, identity):
    for _ in range(300):
        with session_scope(manager.factory) as session:
            report = RunRepository(session).get(identity)
        if report is not None and report.status is not RunStatus.RUNNING:
            return report
        time.sleep(0.01)
    raise AssertionError("UI run did not finish")


def request(identity, kind="sync", **overrides):
    payload = {
        "run_id": identity,
        "kind": kind,
        "source": "arxiv",
        "query": "fixture",
        "limit": "1",
        **overrides,
    }
    return payload


def post(client, path, data):
    return client.post(path, data=data, headers=HEADERS)


def test_ui_sync_requires_active_workspace(client):
    response = post(client, "/ui/runs", request("before-setup"))
    assert response.status_code == 409
    assert "初始化确认" in response.text


def test_ui_sync_progress_idempotency_and_profile_default_query(client, monkeypatch):
    manager = ready(client)
    calls = []
    monkeypatch.setattr(manager, "collect", lambda spec: calls.append("collect"))
    monkeypatch.setattr(manager, "process", lambda spec: calls.append("process"))
    response = post(client, "/ui/runs", request("ui-once"))
    assert response.status_code == 200, response.text
    assert wait(manager, "ui-once").status is RunStatus.SUCCEEDED
    assert calls == ["collect", "process"]
    assert post(client, "/ui/runs", request("ui-once")).status_code == 200
    assert post(client, "/ui/runs", request("ui-once", query="changed")).status_code == 409
    terminal = client.get("/ui/runs/ui-once?fragment=true")
    assert 'hx-trigger="every' not in terminal.text
    assert "已完成" in terminal.text
    today = client.get("/ui/today")
    assert "同步 / 处理" in today.text
    assert 'value="long-document retrieval"' in today.text


def test_ui_failed_step_preserves_data_and_shows_retry(client, monkeypatch):
    manager = ready(client)

    def collect(spec):
        with session_scope(manager.factory) as session:
            if PaperRepository(session).get("collected") is None:
                PaperRepository(session).add(
                    Paper(
                        "collected",
                        "Collected fixture",
                        status=PaperStatus.SCREENED,
                        first_seen_at=utc_now(),
                    )
                )

    monkeypatch.setattr(manager, "collect", collect)
    monkeypatch.setattr(
        manager,
        "process",
        lambda spec: (_ for _ in ()).throw(InvalidRequestError("model failure")),
    )
    assert post(client, "/ui/runs", request("ui-failed")).status_code == 200
    failed = wait(manager, "ui-failed")
    assert failed.status is RunStatus.FAILED and failed.stats_json["completed"] == ["collecting"]
    with session_scope(manager.factory) as session:
        assert PaperRepository(session).get("collected") is not None
    retry_page = client.get("/ui/runs/ui-failed")
    assert "显式重试" in retry_page.text
    assert "model failure" in retry_page.text


def test_restart_marks_unfinished_ui_runs_failed(client):
    manager = ready(client)
    with session_scope(manager.factory) as session:
        RunRepository(session).add(
            Run(
                "interrupted-extract",
                "ui.extract",
                started_at=utc_now(),
                stats_json={"completed": []},
            )
        )
        RunRepository(session).add(Run("cli-unrelated", "ingest", started_at=utc_now()))
    restarted = UIFlowManager(manager.factory, manager.settings)
    restarted.close()
    with session_scope(manager.factory) as session:
        run = RunRepository(session).get("interrupted-extract")
        assert run.status is RunStatus.FAILED
        assert "interrupted" in str(run.errors_json)
        assert RunRepository(session).get("cli-unrelated").status is RunStatus.RUNNING


def test_extract_run_page_links_back_to_setup(client):
    manager = ready(client)
    with session_scope(manager.factory) as session:
        RunRepository(session).add(
            Run(
                "ext-link",
                "ui.extract",
                started_at=utc_now(),
                status=RunStatus.SUCCEEDED,
                finished_at=utc_now(),
                stats_json={"completed": ["extracting"]},
            )
        )
    page = client.get("/ui/runs/ext-link")
    assert "回到草案审阅" in page.text
    assert "生成研究草案" in page.text
