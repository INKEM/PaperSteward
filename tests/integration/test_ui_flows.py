from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from conftest import activate
from papersteward.config import get_settings
from papersteward.core.entities import Paper, Run, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.errors import InvalidRequestError
from papersteward.jobs.ui_flow import UIFlowManager
from papersteward.llm import FakeLLM
from papersteward.llm.provider import LLMCompletion
from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.planner import PlannedQuery, QueryPlan
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.repositories import PaperRepository, RunRepository

HEADERS = {"origin": "http://testserver", "content-type": "application/x-www-form-urlencoded"}

PROCESS_FIXTURES = json.loads(Path("tests/fixtures/process/valid.json").read_text())
PLANNING_FIXTURE = json.loads(Path("tests/fixtures/llm/query_planning.valid.json").read_text())


class UniformEmbedding:
    """所有文本同一向量的离线 embedding；语义分量恒为 1，相关度完全由 rubric 决定。"""

    model_id = "uniform-test"

    def embed(self, texts):
        return tuple((1.0,) for _ in texts)


class OrthogonalEmbedding:
    """按文本哈希的正交 one-hot embedding；跨文本余弦≈0，语义分量恒为 0。"""

    model_id = "orthogonal-test"
    DIMS = 65536

    def embed(self, texts):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.DIMS
            vector = [0.0] * self.DIMS
            vector[index] = 1.0
            vectors.append(tuple(vector))
        return tuple(vectors)


ABSTRACT = (
    "We study long-document retrieval with sparse retrieval and reranking. "
    "We release the benchmark dataset and baseline code for evaluation."
)


def relevance_payload(score: int) -> dict:
    return {
        "problem": score,
        "method": score,
        "domain": score,
        "actionability": score,
        "evidence": [
            {"dimension": "problem", "quote": "long-document retrieval"},
            {"dimension": "method", "quote": "sparse retrieval and reranking"},
            {"dimension": "domain", "quote": "long-document retrieval with sparse retrieval"},
            {"dimension": "actionability", "quote": "benchmark dataset and baseline code"},
        ],
        "confidence": 0.9,
        "boundary_note": None,
    }


class RoutingFakeLLM(FakeLLM):
    """按 prompt 中的 Agent 角色分发 fixture 响应；并行调用的到达顺序不影响分发。"""

    MARKERS = (
        ("检索规划", "query_planning"),
        ("相关度 rubric", "relevance"),
        ("文献概括", "summary"),
        ("分类 Agent", "classification"),
        ("特殊桶提名", "special_nomination"),
    )

    def __init__(self, payloads: dict):
        super().__init__(responses=("pong",))
        self._payloads = payloads

    def complete(self, prompt: str, *, timeout_s: float) -> LLMCompletion:
        for marker, key in self.MARKERS:
            if marker in prompt:
                text = json.dumps(self._payloads[key], ensure_ascii=False)
                return LLMCompletion(
                    text=text, prompt_tokens=1, completion_tokens=1, cost_usd=0.0, latency_ms=0.0
                )
        raise AssertionError(f"未知 prompt 角色：{prompt[:60]}")


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
        "focus": "",
        "target": "5",
        **overrides,
    }
    return payload


def post(client, path, data):
    return client.post(path, data=data, headers=HEADERS)


def canned_plan(queries=("long-document retrieval",)):
    """替换检索规划：返回固定 QueryPlan，不让测试触碰真实模型。"""

    def _plan(request, gateway, target, *, round_index=1, used_keywords="", feedback=""):
        return QueryPlan(
            queries=tuple(
                PlannedQuery(spec=QuerySpec(keywords=(q,), max_results=5), rationale="测试检索式")
                for q in queries
            ),
            strategy_note="测试计划",
            origin="agent",
            round_index=round_index,
        )

    return _plan


def seed_screened(manager, *paper_ids):
    def _collect(request, plan):
        with session_scope(manager.factory) as session:
            for paper_id in paper_ids:
                if PaperRepository(session).get(paper_id) is None:
                    PaperRepository(session).add(
                        Paper(
                            paper_id,
                            f"Paper {paper_id}",
                            abstract=ABSTRACT,
                            status=PaperStatus.SCREENED,
                            first_seen_at=utc_now(),
                        )
                    )

    return _collect


def install_fake_llm(monkeypatch, payloads, embedding=UniformEmbedding):
    monkeypatch.setattr(
        "papersteward.jobs.ui_flow.build_llm_provider",
        lambda config: RoutingFakeLLM(payloads),
    )
    monkeypatch.setattr("papersteward.jobs.ui_flow.HashingEmbeddingProvider", embedding)


def test_ui_sync_requires_active_workspace(client):
    response = post(client, "/ui/runs", request("before-setup"))
    assert response.status_code == 409
    assert "初始化确认" in response.text


def test_ui_sync_idempotency_and_form_has_no_query_field(client, monkeypatch):
    from test_priority_queue import seed

    manager = ready(client)
    calls = []

    def collect(request, plan):
        calls.append(plan.queries[0].spec.keywords)
        with session_scope(manager.factory) as session:
            if PaperRepository(session).get("seed-classified") is None:
                # 完整可入队的 CLASSIFIED 论文（标签/桶/摘要/静态分齐全）。
                seed(session, "seed-classified")

    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", collect)
    response = post(client, "/ui/runs", request("ui-once", target="1"))
    assert response.status_code == 200, response.text
    assert wait(manager, "ui-once").status is RunStatus.SUCCEEDED
    assert calls == [("long-document retrieval",)]  # 第 1 轮达标即停，不再追加规划
    assert post(client, "/ui/runs", request("ui-once", target="1")).status_code == 200
    assert post(client, "/ui/runs", request("ui-once", target="3")).status_code == 409
    terminal = client.get("/ui/runs/ui-once?fragment=true")
    assert 'hx-trigger="every' not in terminal.text
    assert "已完成" in terminal.text
    today = client.get("/ui/today")
    assert "挖掘新论文" in today.text
    # 检索词输入已被 Agent 规划取代；表单只有可选关注点与期望入队篇数。
    assert 'name="query"' not in today.text
    assert 'name="focus"' in today.text
    assert 'name="target"' in today.text
    assert "自动生成多条检索式" in today.text


def test_ui_sync_failed_step_preserves_data_and_shows_retry(client, monkeypatch):
    manager = ready(client)
    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", seed_screened(manager, "collected"))
    monkeypatch.setattr(
        manager,
        "_triage_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(InvalidRequestError("model failure")),
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

    def collect(request, plan):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", collect)
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
    assert "正在深处理论文 6 / 9" in html
    assert "已处理 5 / 9 篇（56%）" in html
    assert '<progress max="9" value="5"' in html
    assert "&lt;Paper &amp; title&gt;" in html
    assert "超过 5 分钟没有新进展" in html
    assert "最近更新" not in html
    assert 'aria-current="step"' in html
    assert "停止任务" in html
    triage = Run(
        "triage",
        "ui.sync",
        started_at=now,
        stats_json={
            "stage": "processing",
            "processing_phase": "triage",
            "processing_total": 9,
            "processed": 5,
            "current_index": 6,
            "updated_at": now.isoformat(),
        },
    )
    assert "正在初筛排序论文 6 / 9" in run_body(triage)
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


def test_sync_funnel_reports_planned_queries_and_meets_target(client, monkeypatch):
    """漏斗全链路：规划进报告、初筛排序、配额达标即停；相关度过门槛的论文正常入队。"""
    manager = ready(client)
    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", seed_screened(manager, "p-a", "p-b", "p-c"))
    install_fake_llm(
        monkeypatch,
        {**PROCESS_FIXTURES, "relevance": relevance_payload(4), "query_planning": PLANNING_FIXTURE},
    )
    assert post(client, "/ui/runs", request("funnel", target="2")).status_code == 200
    report = wait(manager, "funnel")
    assert report.status is RunStatus.SUCCEEDED
    stats = dict(report.stats_json)
    assert stats["queued_this_run"] >= 2
    assert stats["shortfall"] == 0
    assert stats["quota_filled"] == 0  # 相关度过门槛，无需保底填充
    assert stats["target"] == 2
    assert stats["round"] == 1  # 第 1 轮即达标，不再追加规划
    assert stats["plans"][0]["queries"][0]["keywords"] == ["long-document retrieval"]
    with session_scope(manager.factory) as session:
        assert PaperRepository(session).get("p-a").status is PaperStatus.QUEUED
    page = client.get("/ui/runs/funnel")
    assert "本次使用的检索式" in page.text
    assert "long-document retrieval" in page.text


def test_sync_quota_fill_admits_below_gate_with_marker(client, monkeypatch):
    """相关度不足门槛但高于下限：循环扩查后仍不足，保底填充入队并标注 quota_fill。"""
    from sqlalchemy import select

    from papersteward.storage.models import ScoreSnapshotRow

    manager = ready(client)
    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", seed_screened(manager, "q-a", "q-b"))
    install_fake_llm(
        monkeypatch,
        {**PROCESS_FIXTURES, "relevance": relevance_payload(1), "query_planning": PLANNING_FIXTURE},
    )
    assert post(client, "/ui/runs", request("fill", target="2")).status_code == 200
    report = wait(manager, "fill")
    assert report.status is RunStatus.SUCCEEDED
    stats = dict(report.stats_json)
    # 四维 rubric=1、语义恒 1 → R=52：低于门槛 55、高于下限 40，保底填充补足。
    assert stats["queued_this_run"] == 2
    assert stats["quota_filled"] == 2
    assert stats["shortfall"] == 0
    with session_scope(manager.factory) as session:
        for paper_id in ("q-a", "q-b"):
            assert PaperRepository(session).get(paper_id).status is PaperStatus.QUEUED
            row = session.scalar(
                select(ScoreSnapshotRow).where(ScoreSnapshotRow.paper_id == paper_id)
            )
            assert "quota_fill" in row.breakdown_json["reasons"]
    page = client.get("/ui/runs/fill")
    assert "为保底填充" in page.text


def test_sync_reports_shortfall_when_candidates_exhausted(client, monkeypatch):
    """全部候选相关度低于下限：不硬塞，如实报告缺口。"""
    manager = ready(client)
    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", seed_screened(manager, "low-a"))
    install_fake_llm(
        monkeypatch,
        {
            **PROCESS_FIXTURES,
            "relevance": relevance_payload(0),
            "query_planning": PLANNING_FIXTURE,
        },
        embedding=OrthogonalEmbedding,  # 语义分量≈0：rubric=0 → R≈0，低于保底下限 40
    )
    assert post(client, "/ui/runs", request("short", target="2")).status_code == 200
    report = wait(manager, "short")
    assert report.status is RunStatus.SUCCEEDED
    stats = dict(report.stats_json)
    assert stats["queued_this_run"] == 0
    assert stats["shortfall"] == 2
    assert stats["target"] == 2
    assert stats["round"] == 3  # 三轮规划都尝试过


def test_processing_statistics_and_legacy_reconciliation(client, monkeypatch):
    from dataclasses import replace

    from test_process_review import PAPER

    from papersteward.storage.repositories import ReviewRepository

    manager = ready(client)
    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", lambda request, plan: None)
    install_fake_llm(monkeypatch, {**PROCESS_FIXTURES, "relevance": relevance_payload(4)})
    with session_scope(manager.factory) as session:
        PaperRepository(session).add(replace(PAPER, id="a-summary"))
        PaperRepository(session).add(replace(PAPER, id="b-no-summary", abstract=None))
        PaperRepository(session).add(replace(PAPER, id="legacy", status=PaperStatus.NEEDS_REVIEW))
        ReviewRepository(session).open("legacy", ["legacy"], {}, now=utc_now())
    identity = "stats"
    assert post(client, "/ui/runs", request(identity, target="1")).status_code == 200
    report = wait(manager, identity)
    assert report.status is RunStatus.SUCCEEDED
    with session_scope(manager.factory) as session:
        stats = RunRepository(session).get(identity).stats_json
        # 无摘要论文在初筛前就被跳过，不烧任何模型调用；legacy 由对账直接归类。
        assert stats["processed"] == stats["auto_classified"] == 1
        assert stats["skipped_no_abstract"] == 1
        assert stats["reconciled"] == 1
        assert stats["current_paper_title"] is None
        assert stats["updated_at"]
        assert PaperRepository(session).get("legacy").status is PaperStatus.CLASSIFIED
        assert ReviewRepository(session).list_open()[1] == 0
        assert PaperRepository(session).get("a-summary").status is PaperStatus.QUEUED


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

    def collect(request, plan):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", collect)
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

    def collect(request, plan):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", collect)
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

    def collect(request, plan):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(manager, "_plan", canned_plan())
    monkeypatch.setattr(manager, "collect", collect)
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
