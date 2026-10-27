"""单篇分析运行编排集成验证（R2-03）。

覆盖：提交门、happy path（候选落库＋派生副本＋成本记账）、证据验证
（伪引文剔除/越界来源）、材料不足合法、单篇失败保留其他、取消在篇目
边界生效、端点变更停止发送、重启恢复与原参数重试跳过已完成、源漂移
跳过、预算停止、同清单幂等跳过。
"""

import json
import re
from datetime import UTC, datetime

import pytest
from test_reading_insight_projection import T_DONE, seed_archive, seed_paper, system_content
from test_ui_flows import HEADERS

from conftest import activate, activate_workspace
from papersteward.config import Settings
from papersteward.core.entities import Run
from papersteward.errors import InvalidRequestError, NotAuthorizedError
from papersteward.jobs.ui_flow import FlowRequest
from papersteward.llm import LLMCompletion
from papersteward.llm.provider import FakeLLM
from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
from papersteward.reading.insights.analysis import (
    AnalysisRunParamsView,
    run_insight_analysis,
)
from papersteward.reading.insights.authorization import (
    grant_analysis_authorization,
    revoke_analysis_authorization,
)
from papersteward.reading.insights.materials import (
    create_manifest,
    create_preview_token,
)
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.insights import (
    ReadingAnalysisCandidateRepository,
    ReadingDerivedCopyRepository,
)
from papersteward.storage.repositories import RunRepository

NOW2 = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
SETTINGS = Settings()

NOTE_A = "实验设计部分值得再读：双盲流程完整"
QUOTE_A = "双盲流程完整"


def connect_llm(
    factory, *, base_url: str = "http://127.0.0.1:9/v1", model: str = "run-model"
) -> str:
    with session_scope(factory) as session:
        save_llm_config(
            session,
            LLMSettingsPayload.model_validate(
                {"base_url": base_url, "model": model, "consent": True}
            ),
            SETTINGS,
        )
        from papersteward.storage.repositories import ConfigRepository

        row = ConfigRepository(session).latest("llm_config")
        assert row is not None
        return row.version


def seed_note_paper(factory, paper_id: str, note: str) -> None:
    with session_scope(factory) as session:
        seed_paper(session, paper_id, first_seen_at=T_DONE)
        seed_archive(
            session,
            paper_id,
            archived_at=T_DONE,
            content=system_content(title=f"分析-{paper_id}", completed_at=T_DONE),
            rating_col=4,
            note_col=note,
            ratings=[(T_DONE, 4, note)],
        )


def setup_run(
    factory,
    paper_notes: dict[str, str],
    *,
    kinds=None,
    now: datetime | None = None,
    seed_papers: bool = True,
):
    """连模型、种数据、建清单与授权；返回 (params, manifest_row, grant_view)。

    ``now`` 默认固定 NOW2；多次 setup_run 时传入递增时间，保证授权按
    created_at 稳定排序（发送边界取最新 workspace 授权）。
    """
    at = now or NOW2
    connect_llm(factory)
    if seed_papers:
        for paper_id, note in paper_notes.items():
            seed_note_paper(factory, paper_id, note)
    with session_scope(factory) as session:
        row, _ = create_manifest(
            session,
            paper_ids=list(paper_notes),
            kinds=kinds or ["user_notes"],
            now=at,
        )
        grant = grant_analysis_authorization(
            session,
            scope_mode="workspace",
            material_classes=kinds or ["user_notes"],
            settings=SETTINGS,
            now=at,
        )
        manifest_id, manifest_hash, grant_id = row.id, row.manifest_hash, grant.grant_id
    params = AnalysisRunParamsView(
        manifest_id=manifest_id, manifest_hash=manifest_hash, grant_id=grant_id
    )
    return params, manifest_id, manifest_hash, grant_id


class ReflectionProvider(FakeLLM):
    """按 prompt 中的 paper_id 分发反思输出；calls 记录每次 prompt。"""

    def __init__(self, payloads: dict[str, dict], failures: dict[str, Exception] | None = None):
        super().__init__([json.dumps(next(iter(payloads.values()), {}), ensure_ascii=False)])
        self.payloads = payloads
        self.failures = failures or {}
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_s: float) -> LLMCompletion:
        self.prompts.append(prompt)
        match = re.search(r"（ID：(\S+)）", prompt)
        paper_id = match.group(1) if match else ""
        if paper_id in self.failures:
            raise self.failures[paper_id]
        payload = self.payloads.get(paper_id)
        if payload is None:
            raise AssertionError(f"未准备 {paper_id} 的输出")
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.001,
            latency_ms=5,
        )


def reflection_payload(
    paper_id: str, *, quote: str | None = QUOTE_A, source_index: int = 0
) -> dict:
    candidate = {
        "kind": "harvest",
        "nature": "user_expression",
        "statement": "用户认为双盲流程值得复用",
        "conditions": None,
        "limitations": None,
        "quote": quote,
        "source_indexes": [source_index],
        "quote_source_index": source_index if quote is not None else None,
    }
    return {
        "paper_id": paper_id,
        "candidates": [candidate],
        "insufficient_material": False,
        "note": None,
    }


def seed_run_row(factory, run_id: str) -> None:
    with session_scope(factory) as session:
        RunRepository(session).add(
            Run(id=run_id, run_type="ui.insight_analysis", started_at=NOW2, stats_json={})
        )


def run_runner(factory, params, run_id: str, monkeypatch, provider) -> dict:
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: provider,
    )
    seed_run_row(factory, run_id)
    return run_insight_analysis(
        factory,
        SETTINGS,
        params=params,
        run_id=run_id,
        should_abort=lambda: False,
        progress=lambda **values: None,
    )


# —— happy path ————————————————————————————————————————————————


def test_runner_happy_path_persists_candidates_and_copies(insights_factory, monkeypatch):
    factory = insights_factory
    params, manifest_id, manifest_hash, grant_id = setup_run(
        factory, {"paper-a": NOTE_A, "paper-b": "另一篇的备注"}
    )
    provider = ReflectionProvider(
        {
            "paper-a": reflection_payload("paper-a"),
            "paper-b": reflection_payload("paper-b", quote=None),
        }
    )
    generated = run_runner(factory, params, "run-happy", monkeypatch, provider)
    assert generated["papers_total"] == 2
    assert generated["papers_completed"] == 2
    assert generated["candidate_count"] == 2
    assert generated["papers_unfinished"] == 0

    with session_scope(factory) as session:
        rows = ReadingAnalysisCandidateRepository(session).by_run("run-happy")
        assert [row.paper_id for row in rows] == ["paper-a", "paper-b"]
        first = rows[0]
        assert first.verification == "pending_user"
        assert first.quote == QUOTE_A
        assert first.quote_source_id == "paper-a"
        assert first.source_refs_json[0]["kind"] == "user_notes"
        assert first.model_config_version != ""
        copies = ReadingDerivedCopyRepository(session).for_source("paper", "paper-a")
        assert {copy.field_kind for copy in copies} == {"statement", "quote"}
        assert all(copy.derived_kind == "candidate" for copy in copies)
        # 引文类候选只登记来源一次/字段一种，无私人正文进入登记表
        run = RunRepository(session).get("run-happy")
        assert run is not None
        assert run.cost_json["llm_call_count"] == 2
        assert run.cost_json["llm_prompt_tokens"] == 20
    # prompt 只含最小必要字段：备注原文出现，但无归档全库拼接
    assert provider.prompts and "双盲流程完整" in provider.prompts[0]


def test_runner_send_gate_uses_authorized_gateway(insights_factory, monkeypatch):
    """gateway 由 build_analysis_gateway 构造：cache 恒为 None。"""
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-c": NOTE_A})
    captured: dict = {}

    class GatewaySpy:
        def __init__(self, provider, **kwargs):
            captured.update(kwargs, provider=provider)

        def complete_json(self, name, variables):
            from papersteward.schemas import ReadingReflectionOutput

            data = ReadingReflectionOutput.model_validate(reflection_payload("paper-c"))
            from papersteward.core.entities import Provenance
            from papersteward.llm.gateway import LLMResult, LLMUsage

            return LLMResult(
                data=data,
                raw_text="{}",
                usage=LLMUsage(model_id="m"),
                provenance=Provenance(
                    model="m",
                    prompt_version="reading_reflection/v1",
                    generated_at=NOW2,
                    input_hash="0" * 64,
                ),
                prompt_name=name,
                prompt_version="reading_reflection/v1",
                repair_used=False,
                attempts=1,
            )

    from papersteward.reading.insights import analysis as analysis_module

    original_gateway = analysis_module.build_analysis_gateway

    def spy_gateway(resolved):
        built = original_gateway(resolved)
        captured["cache"] = built._cache  # noqa: SLF001
        return GatewaySpy(built._provider, cache=built._cache)  # noqa: SLF001

    monkeypatch.setattr(analysis_module, "build_analysis_gateway", spy_gateway)
    run_runner(factory, params, "run-gate", monkeypatch, ReflectionProvider({}))
    assert captured["cache"] is None


# —— 提交门 ————————————————————————————————————————————————


def test_runner_rejects_without_grant(insights_factory, monkeypatch):
    factory = insights_factory
    connect_llm(factory)
    seed_note_paper(factory, "paper-x", NOTE_A)
    with session_scope(factory) as session:
        row, _ = create_manifest(session, paper_ids=["paper-x"], kinds=["user_notes"], now=NOW2)
        manifest_id, manifest_hash = row.id, row.manifest_hash
    params = AnalysisRunParamsView(
        manifest_id=manifest_id, manifest_hash=manifest_hash, grant_id="ghost"
    )
    with pytest.raises(NotAuthorizedError):
        run_runner(factory, params, "run-nogrant", monkeypatch, ReflectionProvider({}))


def test_runner_rejects_manifest_mismatch(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-y": NOTE_A})
    bad = AnalysisRunParamsView(
        manifest_id=params.manifest_id,
        manifest_hash="f" * 64,
        grant_id=params.grant_id,
    )
    with pytest.raises(NotAuthorizedError, match="清单"):
        run_runner(factory, bad, "run-mismatch", monkeypatch, ReflectionProvider({}))


def test_runner_rejects_revoked_grant(insights_factory, monkeypatch):
    factory = insights_factory
    params, _, _, grant_id = setup_run(factory, {"paper-z": NOTE_A})
    with session_scope(factory) as session:
        revoke_analysis_authorization(session, grant_id=grant_id, now=NOW2)
    with pytest.raises(NotAuthorizedError, match="撤回"):
        run_runner(factory, params, "run-revoked", monkeypatch, ReflectionProvider({}))


# —— 证据验证与材料不足 ————————————————————————————————————————


def test_runner_rejects_fake_quote_but_keeps_valid(insights_factory, monkeypatch):
    """伪引文候选逐条剔除；同一篇的有效候选保留。"""
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-q": NOTE_A})
    payload = reflection_payload("paper-q")
    payload["candidates"].append(
        {
            "kind": "harvest",
            "nature": "model_inference",
            "statement": "引文不存在于材料",
            "quote": "材料里根本没有这句话",
            "source_indexes": [0],
            "quote_source_index": 0,
        }
    )
    payload["candidates"].append(
        {
            "kind": "question",
            "nature": "model_inference",
            "statement": "来源越界",
            "source_indexes": [7],
        }
    )
    generated = run_runner(
        factory, params, "run-fake", monkeypatch, ReflectionProvider({"paper-q": payload})
    )
    assert generated["candidate_count"] == 1
    assert generated["rejected_count"] == 2
    with session_scope(factory) as session:
        rows = ReadingAnalysisCandidateRepository(session).by_run("run-fake")
        assert [row.statement for row in rows] == ["用户认为双盲流程值得复用"]


def test_runner_insufficient_material_is_legal(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-i": NOTE_A})
    payload = {
        "paper_id": "paper-i",
        "candidates": [],
        "insufficient_material": True,
        "note": "材料中缺少方法细节",
    }
    generated = run_runner(
        factory, params, "run-insuf", monkeypatch, ReflectionProvider({"paper-i": payload})
    )
    assert generated["papers_insufficient"] == 1
    assert generated["candidate_count"] == 0
    with session_scope(factory) as session:
        assert ReadingAnalysisCandidateRepository(session).by_run("run-insuf") == []


# —— 部分失败与恢复 ——————————————————————————————————————————————


def test_runner_paper_failure_keeps_other_results(insights_factory, monkeypatch):
    from papersteward.llm.errors import LLMProviderError

    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-f1": NOTE_A, "paper-f2": "第二篇备注"})
    provider = ReflectionProvider(
        {
            "paper-f1": reflection_payload("paper-f1"),
            "paper-f2": reflection_payload("paper-f2", quote=None),
        },
        failures={"paper-f1": LLMProviderError("网络请求失败", retryable=False)},
    )
    generated = run_runner(factory, params, "run-partial", monkeypatch, provider)
    assert generated["papers_completed"] == 1
    assert generated["papers_failed"] == 1
    assert generated["failed_paper_ids"] == ["paper-f1"]
    assert "网络" in generated["failed_reasons"]["paper-f1"]
    with session_scope(factory) as session:
        rows = ReadingAnalysisCandidateRepository(session).by_run("run-partial")
        assert [row.paper_id for row in rows] == ["paper-f2"]


def test_runner_endpoint_change_stops_sending(insights_factory, monkeypatch):
    """授权检查失败即停止新发送：第一篇完成、第二篇抛授权异常。"""
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-e1": NOTE_A, "paper-e2": "第二篇备注"})
    state = {"calls": 0}
    original_complete = ReflectionProvider.complete

    def complete_and_switch(self, prompt, *, timeout_s):
        state["calls"] += 1
        if state["calls"] == 2:
            connect_llm(factory, model="半路换型号")  # 配置版本变化 → 发送边界 403
        return original_complete(self, prompt, timeout_s=timeout_s)

    monkeypatch.setattr(ReflectionProvider, "complete", complete_and_switch)
    provider = ReflectionProvider(
        {"paper-e1": reflection_payload("paper-e1"), "paper-e2": reflection_payload("paper-e2")}
    )
    with pytest.raises(NotAuthorizedError):
        run_runner(factory, params, "run-switch", monkeypatch, provider)
    with session_scope(factory) as session:
        rows = ReadingAnalysisCandidateRepository(session).by_run("run-switch")
        assert [row.paper_id for row in rows] == ["paper-e1"], "已完成篇目保留"


def test_runner_abort_stops_at_paper_boundary(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-b1": NOTE_A, "paper-b2": "第二篇备注"})
    seen = {"count": 0}

    def abort_after_first() -> bool:
        # 检查点顺序：第一篇循环前(1)、第一篇落库前(2)、第二篇循环前(3)→取消。
        seen["count"] += 1
        return seen["count"] > 2

    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: ReflectionProvider(
            {
                "paper-b1": reflection_payload("paper-b1"),
                "paper-b2": reflection_payload("paper-b2", quote=None),
            }
        ),
    )
    seed_run_row(factory, "run-abort")
    generated = run_insight_analysis(
        factory,
        SETTINGS,
        params=params,
        run_id="run-abort",
        should_abort=abort_after_first,
        progress=lambda **values: None,
    )
    assert generated["papers_completed"] == 1
    assert generated["papers_unfinished"] == 1


def test_runner_stale_material_skips_paper(insights_factory, monkeypatch):
    """清单钉住的评分版本消失（被删除）→ 该篇标 stale，不落候选。

    追加新评分不算漂移：清单绑定具体版本锚点，锚点仍在即与清单一致。
    """
    factory = insights_factory
    params, manifest_id, _, _ = setup_run(factory, {"paper-s1": NOTE_A})
    with session_scope(factory) as session:
        from sqlalchemy import delete as sa_delete

        from papersteward.storage.models import RatingRow

        session.execute(
            sa_delete(RatingRow).where(
                RatingRow.paper_id == "paper-s1", RatingRow.created_at == T_DONE
            )
        )
    generated = run_runner(factory, params, "run-stale", monkeypatch, ReflectionProvider({}))
    assert generated["papers_stale"] == 1
    assert generated["papers_completed"] == 0
    with session_scope(factory) as session:
        assert ReadingAnalysisCandidateRepository(session).by_run("run-stale") == []


def test_runner_budget_stops_at_boundary(insights_factory, monkeypatch, tmp_path):
    factory = insights_factory
    notes = {f"paper-p{i}": f"第{i}篇备注" for i in range(3)}
    params, *_ = setup_run(factory, notes)
    provider = ReflectionProvider({paper: reflection_payload(paper, quote=None) for paper in notes})
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: provider,
    )
    # 笔记约 5 字符：上限 7 只够第一篇，第二篇在篇目边界停止。
    monkeypatch.setattr(
        "papersteward.reading.insights.analysis.ANALYSIS_BUDGET_MAX_INPUT_CHARS",
        7,
    )
    seed_run_row(factory, "run-budget")
    generated = run_insight_analysis(
        factory,
        SETTINGS,
        params=params,
        run_id="run-budget",
        should_abort=lambda: False,
        progress=lambda **values: None,
    )
    assert generated["papers_completed"] == 1
    assert generated["papers_unfinished"] == 2


def test_repeat_run_skips_already_done(insights_factory, monkeypatch):
    """原参数重试跳过已完成篇目（未完成部分重试语义）。"""
    factory = insights_factory
    params, *_ = setup_run(factory, {"paper-r1": NOTE_A, "paper-r2": "第二篇备注"})
    from papersteward.llm.errors import LLMProviderError

    provider = ReflectionProvider(
        {
            "paper-r1": reflection_payload("paper-r1"),
            "paper-r2": reflection_payload("paper-r2", quote=None),
        },
        failures={"paper-r2": LLMProviderError("网络请求失败", retryable=False)},
    )
    first = run_runner(factory, params, "run-retry", monkeypatch, provider)
    assert first["papers_completed"] == 1 and first["papers_failed"] == 1
    second = run_runner(
        factory,
        params,
        "run-retry-2",
        monkeypatch,
        ReflectionProvider({"paper-r2": reflection_payload("paper-r2", quote=None)}),
    )
    assert second["papers_already_done"] == 1
    assert second["papers_completed"] == 1
    with session_scope(factory) as session:
        rows = ReadingAnalysisCandidateRepository(session).by_manifest_paper(
            params.manifest_hash, "paper-r1"
        )
        assert len(rows) == 1, "已完成篇目不重复生成候选"


# —— 任务级接线（manager.submit 校验 + 恢复）———————————————————


def test_manager_submit_and_recover_for_insight_analysis(client, migrated_engine, monkeypatch):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    params, manifest_id, manifest_hash, grant_id = setup_run(factory, {"paper-m1": NOTE_A})
    manager = client.app.state.ui_flows

    # 提交门：清单不符直接拒绝
    with pytest.raises(InvalidRequestError):
        manager.submit(
            FlowRequest(
                run_id="run-bad",
                kind="insight_analysis",
                analysis={
                    "manifest_id": manifest_id,
                    "manifest_hash": "0" * 64,
                    "grant_id": grant_id,
                },
            )
        )

    # 正常提交（provider 未注入也能排队——模型调用在执行期才发生）
    manager.submit(
        FlowRequest(
            run_id="run-ok",
            kind="insight_analysis",
            analysis={
                "manifest_id": manifest_id,
                "manifest_hash": manifest_hash,
                "grant_id": grant_id,
            },
        )
    )
    # 执行前重启：recover 标记失败
    manager.recover()
    from papersteward.core.enums import RunStatus

    run = wait_run(migrated_engine, "run-ok")
    assert run.status is RunStatus.FAILED


def wait_run(engine, run_id: str):
    import time

    from papersteward.core.enums import RunStatus
    from papersteward.storage.repositories import RunRepository

    factory = create_session_factory(engine)
    for _ in range(600):
        with session_scope(factory) as session:
            run = RunRepository(session).get(run_id)
        if run is not None and run.status is not RunStatus.RUNNING:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


# —— 提交入口与候选展示（JSON＋页面）—————————————————————————————


def test_json_submit_and_candidates_roundtrip(client, migrated_engine, monkeypatch):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    params, manifest_id, manifest_hash, grant_id = setup_run(
        factory, {"paper-api": NOTE_A, "paper-api2": "第二篇备注"}
    )
    del params, manifest_hash, grant_id

    # 无令牌提交 → 404
    missing = client.post("/reading-insights/analysis/runs", json={"preview_token": "ghost"})
    assert missing.status_code == 404

    # 建清单＋令牌（走页面预览同语义的 JSON：manifests）
    manifest = client.post(
        "/reading-insights/analysis/manifests",
        json={"paper_ids": ["paper-api", "paper-api2"], "kinds": ["user_notes"]},
    )
    assert manifest.status_code == 201
    token = manifest.json()["preview"]["token"]

    # 注入 FakeLLM 后提交
    provider = ReflectionProvider(
        {
            "paper-api": reflection_payload("paper-api"),
            "paper-api2": reflection_payload("paper-api2", quote=None),
        }
    )
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: provider,
    )
    submitted = client.post("/reading-insights/analysis/runs", json={"preview_token": token})
    assert submitted.status_code == 202
    run_id = submitted.json()["run_id"]

    run = wait_run(migrated_engine, run_id)
    from papersteward.core.enums import RunStatus

    assert run.status is RunStatus.SUCCEEDED

    # 令牌已消费：同令牌再提交 → 409
    again = client.post("/reading-insights/analysis/runs", json={"preview_token": token})
    assert again.status_code == 409

    # 运行摘要与候选
    runs = client.get("/reading-insights/analysis/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["run_id"] == run_id
    assert runs.json()[0]["candidate_count"] == 2

    candidates = client.get(f"/reading-insights/analysis/runs/{run_id}/candidates")
    assert candidates.status_code == 200
    items = candidates.json()
    assert [item["paper_id"] for item in items] == ["paper-api", "paper-api2"]
    assert items[0]["verification"] == "pending_user"
    assert items[0]["quote"] == QUOTE_A

    by_paper = client.get("/reading-insights/analysis/candidates?paper_id=paper-api")
    assert [item["candidate_id"] for item in by_paper.json()] == [items[0]["candidate_id"]]

    missing_run = client.get("/reading-insights/analysis/runs/ghost/candidates")
    assert missing_run.status_code == 404


def test_json_submit_requires_grant(client, migrated_engine):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    connect_llm(factory)
    seed_note_paper(factory, "paper-nog", NOTE_A)
    with session_scope(factory) as session:
        row, _ = create_manifest(
            session, paper_ids=["paper-nog"], kinds=["user_notes"], now=datetime.now(UTC)
        )
        token = create_preview_token(session, manifest_id=row.id, now=datetime.now(UTC))
    response = client.post("/reading-insights/analysis/runs", json={"preview_token": token.token})
    assert response.status_code == 403
    assert "授权" in response.json()["error"]["message"]


def test_page_analysis_flow_end_to_end(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    connect_llm(factory)
    seed_note_paper(factory, "paper-page", NOTE_A)
    from papersteward.reading.insights.authorization import grant_analysis_authorization

    with session_scope(factory) as session:
        grant_analysis_authorization(
            session,
            scope_mode="workspace",
            material_classes=["user_notes"],
            settings=SETTINGS,
            now=NOW2,
        )

    # 预览（页面语义：建清单＋令牌＋提交表单）
    preview = client.post(
        "/ui/reading-insights/analysis/preview",
        data={"paper_ids": "paper-page", "kind_user_notes": "1"},
        headers=HEADERS,
    )
    assert preview.status_code == 200
    assert "提交分析" in preview.text
    assert 'name="preview_token"' in preview.text
    import re as _re

    token_match = _re.search(r'name="preview_token" value="([0-9a-f]{32})"', preview.text)
    assert token_match is not None
    token = token_match.group(1)

    # 提交（FakeLLM）
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: ReflectionProvider(
            {"paper-page": reflection_payload("paper-page", quote=None)}
        ),
    )
    submitted = client.post(
        "/ui/reading-insights/analysis/runs",
        data={"preview_token": token, "run_id": "run-page"},
        headers=HEADERS,
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    assert submitted.headers["location"].endswith("/ui/runs/run-page")

    run = wait_run(client.app.state.engine, "run-page")
    from papersteward.core.enums import RunStatus

    assert run.status is RunStatus.SUCCEEDED

    # 分析页展示运行与候选
    page = client.get("/ui/reading-insights/analysis?run=run-page")
    assert page.status_code == 200
    assert "分析结果" in page.text
    assert "候选 1 条" in page.text
    assert "用户认为双盲流程值得复用" in page.text
    assert "用户表达" in page.text
    assert "候选未经你确认" in page.text

    # 运行页终态跳转目标
    detail = client.get("/ui/runs/run-page", follow_redirects=False)
    assert detail.status_code == 303
    assert "/ui/reading-insights/analysis?run=run-page" in detail.headers["location"]
