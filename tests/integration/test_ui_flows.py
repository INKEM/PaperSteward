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
    assert "挖掘新论文" in today.text
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
    assert "用原参数重试" in retry_page.text
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


def test_extract_success_redirects_forward_to_setup(client):
    """首次设置未完成时，草案生成成功直接进入审阅，不停留在进度页。"""
    with session_scope(client.app.state.ui_flows.factory) as session:
        RunRepository(session).add(
            Run(
                "ext-forward",
                "ui.extract",
                started_at=utc_now(),
                status=RunStatus.SUCCEEDED,
                finished_at=utc_now(),
                stats_json={"completed": ["extracting"]},
            )
        )
    response = client.get("/ui/runs/ext-forward", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/setup"


def test_extract_run_page_after_active_keeps_queue_link(client):
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
    assert "返回队列" in page.text
    assert "生成研究草案" in page.text


def test_running_task_blocks_new_submission_and_today_refreshes(client, monkeypatch):
    from threading import Event

    manager = ready(client)
    entered, release = Event(), Event()

    def collect(spec):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "collect", collect)
    monkeypatch.setattr(manager, "process", lambda spec: None)
    try:
        assert post(client, "/ui/runs", request("busy")).status_code == 200
        assert entered.wait(2)
        conflict = client.post(
            "/ui/runs",
            data=request("blocked", kind="refresh"),
            headers=HEADERS,
            follow_redirects=False,
        )
        assert conflict.status_code == 303
        assert conflict.headers["location"] == "/ui/runs/busy"
        assert post(client, "/ui/digests", {}).status_code == 404
        assert post(client, "/ui/runs", request("busy")).status_code == 200
        page = client.get("/ui/today").text
        assert 'id="current-run"' in page
        assert 'hx-trigger="every 2s"' in page
        assert page.count("<button disabled>") == 2
        assert "队列共" in page and "刷新排序" in page and "待审核" not in page
        with session_scope(manager.factory) as session:
            assert RunRepository(session).get("blocked") is None
            assert len(RunRepository(session).running_ui_runs()) == 1
    finally:
        release.set()
    assert wait(manager, "busy").status is RunStatus.SUCCEEDED
    fragment = client.get("/ui/current-run?identity=busy")
    assert fragment.headers["HX-Refresh"] == "true"
    assert "<button disabled>" not in client.get("/ui/today").text


def test_progress_states_and_legacy_stats(client):
    from datetime import timedelta

    from papersteward.api.workflow_pages import run_body

    now = utc_now()
    active = Run(
        "progress",
        "ui.sync",
        started_at=now - timedelta(minutes=7),
        stats_json={
            "stage": "processing",
            "processing_total": 9,
            "processed": 5,
            "current_index": 6,
            "current_paper_title": "<Paper & title>",
            "updated_at": (now - timedelta(minutes=6)).isoformat(),
        },
    )
    html = run_body(active)
    assert "正在处理论文 6 / 9" in html
    assert "已处理 5 / 9 篇（56%）" in html
    assert '<progress max="9" value="5"' in html
    assert "&lt;Paper &amp; title&gt;" in html
    assert "超过 5 分钟没有新进展" in html
    assert "最近更新" not in html
    assert 'aria-current="step"' in html
    assert "停止任务" in html
    queued = Run("queued", "ui.refresh", started_at=now)
    html = run_body(queued, current=active)
    assert "正在开始" in html
    assert "任务已排队" not in html
    assert "/ui/runs/progress" not in html
    assert "等待运行 · 运行中" not in html
    for stage, title in (
        ("collecting", "正在采集论文"),
        ("refreshing", "正在刷新阅读队列"),
        ("extracting", "正在生成研究草案"),
    ):
        html = run_body(Run("stage", "ui.sync", started_at=now, stats_json={"stage": stage}))
        assert title in html and '<progress aria-label="任务进行中">' in html
    legacy = Run("old", "ui.sync", started_at=now, stats_json={"processed": 2})
    assert "正在开始" in run_body(legacy)
    terminal = Run(
        "success", "ui.sync", started_at=now, finished_at=now, status=RunStatus.SUCCEEDED
    )
    assert "挖掘新论文完成" in run_body(terminal)
    assert "hx-trigger" not in run_body(terminal)
    assert "返回队列" in run_body(terminal, home=("/ui/today", "返回队列"))
    running_with_home = Run("live", "ui.sync", started_at=now, stats_json={"stage": "queued"})
    assert "返回队列" not in run_body(running_with_home, home=("/ui/today", "返回队列"))


def test_processing_statistics_and_legacy_reconciliation(client, monkeypatch):
    import json
    from dataclasses import replace
    from pathlib import Path

    from test_process_review import PAPER

    from papersteward.jobs.ui_flow import FlowRequest
    from papersteward.llm import FakeLLM
    from papersteward.storage.repositories import ReviewRepository

    manager = ready(client)
    fixture = json.loads(Path("tests/fixtures/process/valid.json").read_text())
    responses = [
        json.dumps(fixture[key], ensure_ascii=False)
        for key in (
            "relevance",
            "summary",
            "classification",
            "special_nomination",
            "relevance",
            "classification",
            "special_nomination",
        )
    ]
    monkeypatch.setattr(
        "papersteward.jobs.ui_flow.build_llm_provider", lambda config: FakeLLM(responses)
    )
    with session_scope(manager.factory) as session:
        PaperRepository(session).add(replace(PAPER, id="a-summary"))
        PaperRepository(session).add(replace(PAPER, id="b-no-summary", abstract=None))
        PaperRepository(session).add(replace(PAPER, id="legacy", status=PaperStatus.NEEDS_REVIEW))
        ReviewRepository(session).open("legacy", ["legacy"], {}, now=utc_now())
        RunRepository(session).add(Run("stats", "ui.sync", started_at=utc_now()))
    manager.process(FlowRequest(run_id="stats", kind="sync", limit=10))
    with session_scope(manager.factory) as session:
        stats = RunRepository(session).get("stats").stats_json
        assert stats["processing_total"] == stats["processed"] == stats["auto_classified"] == 2
        assert stats["skipped_no_summary"] == 1
        assert stats["reconciled"] == 1
        assert stats["current_paper_title"] is None
        assert stats["updated_at"]
        assert "b-no-summary" in stats["quality_flags"]
        assert PaperRepository(session).get("legacy").status is PaperStatus.CLASSIFIED
        assert ReviewRepository(session).list_open()[1] == 0


def _draft_ready(client):
    """保存模型配置并上传材料，进入可生成草案的 draft 阶段；返回任务管理器。"""
    assert (
        client.post(
            "/ui/settings/llm",
            data={
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "test-model",
                "timeout_s": "30",
                "consent": "yes",
                "next": "/ui/setup",
            },
            headers=HEADERS,
        ).status_code
        == 200
    )
    material = client.post(
        "/ui/setup/materials",
        data={"name": "研究简介", "content": "长文档检索与忠实性评测。", "from_file": "0"},
        headers=HEADERS,
    )
    assert material.status_code == 200
    manager = client.app.state.ui_flows
    manager.settings = get_settings()
    return manager


def test_cancel_stops_generation_and_returns_to_materials(client, monkeypatch):
    import json
    import threading
    from pathlib import Path

    from papersteward.llm import FakeLLM
    from papersteward.onboarding.render import PROFILE_ID
    from papersteward.storage.repositories import ProfileRepository

    manager = _draft_ready(client)
    extraction = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/llm/profile_extraction.valid.json"
        ).read_text()
    )
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeLLM):
        def complete(self, prompt: str, *, timeout_s: float):
            entered.set()
            assert release.wait(5)
            return super().complete(prompt, timeout_s=timeout_s)

    monkeypatch.setattr(
        "papersteward.onboarding.intake.build_llm_provider",
        lambda config: Blocking([json.dumps(extraction, ensure_ascii=False)]),
    )
    try:
        assert (
            client.post(
                "/ui/setup/generate", data={"run_id": "ext-cancel"}, headers=HEADERS
            ).status_code
            == 200
        )
        assert entered.wait(2)
        page = client.get("/ui/setup")
        assert 'id="run-status"' in page.text
        assert "停止任务" in page.text
        assert "正在调用模型生成草案" in page.text
        assert 'action="/ui/setup/generate"' not in page.text
        assert "提交意见并重新生成" not in page.text
        conflict = client.post(
            "/ui/setup/feedback",
            data={"run_id": "ext-fb", "comment": "还没有到这一步"},
            headers=HEADERS,
            follow_redirects=False,
        )
        assert conflict.status_code == 303
        assert conflict.headers["location"] == "/ui/runs/ext-cancel"
        cancelled = client.post(
            "/ui/runs/ext-cancel/cancel", data={}, headers=HEADERS, follow_redirects=False
        )
        assert cancelled.status_code == 303
        assert cancelled.headers["location"] == "/ui/setup"
        with session_scope(manager.factory) as session:
            assert RunRepository(session).get("ext-cancel").status is RunStatus.CANCELLED
    finally:
        release.set()
    assert wait(manager, "ext-cancel").status is RunStatus.CANCELLED
    with session_scope(manager.factory) as session:
        assert ProfileRepository(session).list(PROFILE_ID) == []
    stopped = client.get("/ui/setup")
    assert "上一次生成已停止" in stopped.text
    assert 'action="/ui/setup/materials"' in stopped.text
    assert "还没有到这一步" not in stopped.text
    # 停止释放预约后可以立即重新生成；此前的意见没有被落库。
    assert (
        client.post("/ui/setup/generate", data={"run_id": "ext-again"}, headers=HEADERS).status_code
        == 200
    )
    assert wait(manager, "ext-again").status is RunStatus.SUCCEEDED
    assert "渲染视图" in client.get("/ui/setup").text


def test_cancel_running_sync_run_and_resubmit(client, monkeypatch):
    import threading

    manager = ready(client)
    entered, release = threading.Event(), threading.Event()

    def collect(spec):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "collect", collect)
    monkeypatch.setattr(manager, "process", lambda spec: None)
    try:
        assert post(client, "/ui/runs", request("busy-sync")).status_code == 200
        assert entered.wait(2)
        cancelled = client.post(
            "/ui/runs/busy-sync/cancel", data={}, headers=HEADERS, follow_redirects=False
        )
        assert cancelled.status_code == 303
        assert cancelled.headers["location"] == "/ui/today?cancelled=1"
    finally:
        release.set()
    assert wait(manager, "busy-sync").status is RunStatus.CANCELLED
    assert "已取消" in client.get("/ui/runs/busy-sync").text
    # 取消释放预约后可以立即开始新任务；对已结束任务再次取消是幂等回退。
    assert post(client, "/ui/runs", request("after-cancel")).status_code == 200
    assert wait(manager, "after-cancel").status is RunStatus.SUCCEEDED
    again = client.post(
        "/ui/runs/busy-sync/cancel", data={}, headers=HEADERS, follow_redirects=False
    )
    assert again.status_code == 303


def test_resubmit_after_cancel_explains_waiting(client, monkeypatch):
    import threading

    manager = ready(client)
    entered, release = threading.Event(), threading.Event()

    def collect(spec):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "collect", collect)
    monkeypatch.setattr(manager, "process", lambda spec: None)
    try:
        assert post(client, "/ui/runs", request("first-run")).status_code == 200
        assert entered.wait(2)
        assert (
            client.post(
                "/ui/runs/first-run/cancel", data={}, headers=HEADERS, follow_redirects=False
            ).status_code
            == 303
        )
        # worker 仍在退出中：立即再提交，页面如实说明等待原因而不是"排队"。
        assert post(client, "/ui/runs", request("second-run")).status_code == 200
        page = client.get("/ui/runs/second-run?fragment=true")
        assert "结束后自动开始" in page.text
    finally:
        release.set()
    assert wait(manager, "second-run").status is RunStatus.SUCCEEDED


def test_cancel_survives_locked_db_and_still_stops(client, monkeypatch):
    """落库被 worker 写事务占用时：端点不 500，worker 在检查点补写取消。"""
    from threading import Event

    from sqlalchemy.exc import OperationalError

    manager = ready(client)
    entered, release = Event(), Event()

    def collect(spec):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "collect", collect)
    monkeypatch.setattr(manager, "process", lambda spec: None)
    original = RunRepository.transition
    state = {"tried": False}

    def locked_transition(self, run_id, target, **kwargs):
        if target is RunStatus.CANCELLED and not state["tried"]:
            state["tried"] = True
            raise OperationalError("UPDATE run", {}, Exception("database is locked"))
        return original(self, run_id, target, **kwargs)

    monkeypatch.setattr(RunRepository, "transition", locked_transition)
    try:
        assert post(client, "/ui/runs", request("locked-run")).status_code == 200
        assert entered.wait(2)
        cancelled = client.post(
            "/ui/runs/locked-run/cancel", data={}, headers=HEADERS, follow_redirects=False
        )
        assert cancelled.status_code == 303
        assert "locked-run" in manager._cancel_flags
        with session_scope(manager.factory) as session:
            assert RunRepository(session).get("locked-run").status is RunStatus.RUNNING
    finally:
        release.set()
    assert wait(manager, "locked-run").status is RunStatus.CANCELLED
