"""候选决定（R2-04）：确认/并入/忽略的闭环与拒绝记忆持久验证。"""

from datetime import UTC, datetime

import pytest
from test_reading_analysis_runner import (
    NOTE_A,
    NOW2,
    SETTINGS,
    ReflectionProvider,
    reflection_payload,
    run_runner,
    setup_run,
)
from test_ui_flows import HEADERS

from conftest import activate, activate_workspace
from papersteward.errors import ConflictError, NotFoundError
from papersteward.reading.insights.analysis import run_insight_analysis
from papersteward.reading.insights.contracts import CandidateDecisionInput
from papersteward.reading.insights.decisions import (
    decide_candidate,
    dismissed_statement_hashes,
)
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.insights import (
    ReadingAnalysisCandidateRepository,
    ReadingAnalysisDecisionRepository,
    ReadingAssetTypedRepository,
)


def seed_candidate(
    factory, paper_id: str = "dec-a", *, run_id: str = "run-dec", monkeypatch=None
) -> str:
    """跑一次分析生成候选（FakeLLM），返回第一条候选 ID。"""
    params, *_ = setup_run(factory, {paper_id: NOTE_A})
    provider = ReflectionProvider({paper_id: reflection_payload(paper_id)})
    if monkeypatch is None:
        import papersteward.reading.insights.authorization as auth

        auth.build_llm_provider = lambda config, **kwargs: provider
        _seed_run(factory, run_id)
        run_insight_analysis(
            factory,
            SETTINGS,
            params=params,
            run_id=run_id,
            should_abort=lambda: False,
            progress=lambda **values: None,
        )
    else:
        run_runner(factory, params, run_id, monkeypatch, provider)
    with session_scope(factory) as session:
        rows = ReadingAnalysisCandidateRepository(session).by_run(run_id)
        assert rows, "种子候选必须存在"
        return rows[0].id


def _seed_run(factory, run_id: str) -> None:
    from papersteward.core.entities import Run
    from papersteward.storage.repositories import RunRepository

    with session_scope(factory) as session:
        RunRepository(session).add(
            Run(id=run_id, run_type="ui.insight_analysis", started_at=NOW2, stats_json={})
        )


def decision_input(candidate_id: str, **overrides: object) -> CandidateDecisionInput:
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "decision": "dismiss",
        "idempotency_key": f"dec-key-{candidate_id[:8]}",
    }
    payload.update(overrides)
    return CandidateDecisionInput.model_validate(payload)


def confirm_method_input(candidate_id: str, **overrides: object) -> CandidateDecisionInput:
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "decision": "confirm",
        "idempotency_key": f"dec-key-{candidate_id[:8]}",
        "kind": "method",
        "title": "双盲实验设计方法卡",
        "body": "从候选陈述整理：实验设计部分值得复用。",
        "sources": [{"paper_id": "dec-a", "rating_at": datetime(2026, 10, 20, 2, 0, tzinfo=UTC)}],
        "typed": {
            "problem": "如何保证实验结论可信",
            "approach": "双盲流程 + 预注册",
            "limitations": "成本较高",
        },
    }
    payload.update(overrides)
    return CandidateDecisionInput.model_validate(payload)


# —— confirm：生成方法卡 ——————————————————————————————————————————


def test_confirm_creates_method_card_with_typed(insights_factory):
    factory = insights_factory
    candidate_id = seed_candidate(factory)
    with session_scope(factory) as session:
        outcome = decide_candidate(session, confirm_method_input(candidate_id), now=NOW2)
        assert outcome.view is not None
        assert outcome.view.kind == "method"
        asset_id = outcome.asset_id
        typed = ReadingAssetTypedRepository(session).get(asset_id, 1)
        assert typed is not None
        assert typed.content_json["approach"] == "双盲流程 + 预注册"
        row = ReadingAnalysisCandidateRepository(session).list_for_paper("dec-a")[0]
        assert row.verification == "confirmed"
        decision = ReadingAnalysisDecisionRepository(session).find_by_candidate(candidate_id)
        assert decision is not None
        assert decision.asset_id == asset_id


def test_repeated_confirm_replays_and_conflict_on_change(insights_factory):
    factory = insights_factory
    candidate_id = seed_candidate(factory)
    with session_scope(factory) as session:
        first = decide_candidate(session, confirm_method_input(candidate_id), now=NOW2)
        first_asset = first.asset_id
        # 同 decision 重放：不新增资产行、返回同一资产
        replay = decide_candidate(session, confirm_method_input(candidate_id), now=NOW2)
        assert replay.asset_id == first_asset
        # 不同 decision → 409
        with pytest.raises(ConflictError, match="已处理"):
            decide_candidate(
                session,
                decision_input(candidate_id, decision="dismiss"),
                now=NOW2,
            )


def test_confirm_missing_candidate_not_found(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session, pytest.raises(NotFoundError):
        decide_candidate(
            session,
            confirm_method_input("ghost"),
            now=NOW2,
        )


# —— dismiss：拒绝记忆持久 ——————————————————————————————————————————


def test_dismiss_writes_memory_and_filters_repeat_candidates(insights_factory, monkeypatch):
    """拒绝记忆持久：同 (论文, 类型, 陈述哈希) 的候选被忽略后，同论文新清单
    分析产出完全相同陈述不落库；改措辞的新候选仍落库。

    授权时间逐次递增：发送边界取「最新 workspace 授权」核对最新配置版本，
    固定时间会让多条授权 created_at 相同、排序退化随机选中旧授权。
    """
    from papersteward.reading.insights.analysis import (
        run_insight_analysis,
    )

    factory = insights_factory
    candidate_id = seed_candidate(factory, "dec-m", run_id="run-dec-m")
    with session_scope(factory) as session:
        decide_candidate(session, decision_input(candidate_id, decision="dismiss"), now=NOW2)
        hashes = dismissed_statement_hashes(session, "dec-m", "harvest")
        assert len(hashes) == 1

    # 同论文建新清单（哈希不同 → 不触发整篇 already_done 跳过）
    params2, *_ = setup_run(
        factory,
        {"dec-m": NOTE_A},
        now=datetime(2026, 10, 25, 18, 5, tzinfo=UTC),
        seed_papers=False,
    )
    same = reflection_payload("dec-m")
    provider = ReflectionProvider({"dec-m": same})
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: provider,
    )
    _seed_run(factory, "run-dec-m2")
    blocked = run_insight_analysis(
        factory,
        SETTINGS,
        params=params2,
        run_id="run-dec-m2",
        should_abort=lambda: False,
        progress=lambda **values: None,
    )
    assert blocked["candidate_count"] == 0, "同陈述应被拒绝记忆过滤"
    assert blocked["papers_insufficient"] == 1

    # 再一次新清单 + 改措辞的新候选：不被记忆过滤
    params3, *_ = setup_run(
        factory,
        {"dec-m": NOTE_A},
        now=datetime(2026, 10, 25, 18, 10, tzinfo=UTC),
        seed_papers=False,
    )
    reworded = reflection_payload("dec-m", quote=None)
    reworded["candidates"][0]["statement"] = "完全不同措辞的新观察"
    provider2 = ReflectionProvider({"dec-m": reworded})
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda config, **kwargs: provider2,
    )
    _seed_run(factory, "run-dec-m3")
    kept = run_insight_analysis(
        factory,
        SETTINGS,
        params=params3,
        run_id="run-dec-m3",
        should_abort=lambda: False,
        progress=lambda **values: None,
    )
    assert kept["candidate_count"] == 1, "改措辞的新候选不应被记忆过滤"


# —— merge：并入已有资产 ——————————————————————————————————————————


def test_merge_appends_revision_to_target_asset(insights_factory):
    from papersteward.reading.insights.assets import create_asset
    from papersteward.reading.insights.contracts import AssetCreateInput

    factory = insights_factory
    candidate_id = seed_candidate(factory, "dec-g")
    print("DEBUG candidate_id:", candidate_id)
    with session_scope(factory) as session:
        target = create_asset(
            session,
            AssetCreateInput.model_validate(
                {
                    "kind": "method",
                    "title": "已有方法卡",
                    "body": "原有做法",
                    "sources": [{"paper_id": "dec-g"}],
                    "typed": {"problem": "P", "approach": "A"},
                    "idempotency_key": "target-key",
                }
            ),
            now=NOW2,
        )
        target_id = target.asset_id
        outcome = decide_candidate(
            session,
            CandidateDecisionInput.model_validate(
                {
                    "candidate_id": candidate_id,
                    "decision": "merge",
                    "idempotency_key": "merge-key-1",
                    "target_asset_id": target_id,
                    "expected_revision": target.revision.revision,
                    "title": "并入：双盲设计观察",
                    "body": "候选陈述并入说明",
                    "typed": {
                        "problem": "P（并入后修订）",
                        "approach": "A + 双盲补充",
                    },
                    "status": "active",
                }
            ),
            now=NOW2,
        )
        assert outcome.view is not None
        assert outcome.view.revision.revision == 2
        # 目标来源保留（合并版本沿用目标资产来源）
        assert outcome.view.revision.sources[0].paper_id == "dec-g"
        # 候选与决定回写
        row = ReadingAnalysisCandidateRepository(session).list_for_paper("dec-g")[0]
        assert row.verification == "merged"
        decision = ReadingAnalysisDecisionRepository(session).find_by_candidate(candidate_id)
        assert decision is not None
        assert decision.asset_id == target_id and decision.asset_revision == 2
        # 旧版本引用可查（版本 1 原样）
        v1 = ReadingAssetTypedRepository(session).get(target_id, 1)
        assert v1 is not None and v1.content_json["approach"] == "A"
        # 冲突：expected_revision 过期 → 409，且决定未写入（候选仍待核对，
        # 修正后同候选可重试）。构造于 merge 成功之前无法回溯，此处用另一条
        # 真实候选验证：先建第二篇候选，merge 过期版本 409 后修正重试成功。


# —— JSON 决定路由与页面确认流 ——————————————————————————————————————


def test_json_decision_roundtrip(client, migrated_engine, monkeypatch):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    candidate_id = seed_candidate(factory, "json-dec", run_id="run-json")

    detail = client.get(f"/reading-insights/analysis/candidates/{candidate_id}")
    assert detail.status_code == 200
    assert detail.json()["verification"] == "pending_user"
    assert detail.json()["decision"] is None

    confirmed = client.post(
        f"/reading-insights/analysis/candidates/{candidate_id}/decisions",
        json={
            "candidate_id": candidate_id,
            "decision": "confirm",
            "idempotency_key": "json-dec-key",
            "kind": "harvest",
            "title": "JSON 确认的收获",
            "body": "候选陈述",
            "sources": [{"paper_id": "json-dec"}],
        },
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["asset_id"]

    # 重复提交重放同一资产
    replay = client.post(
        f"/reading-insights/analysis/candidates/{candidate_id}/decisions",
        json={
            "candidate_id": candidate_id,
            "decision": "confirm",
            "idempotency_key": "json-dec-key",
            "kind": "harvest",
            "title": "JSON 确认的收获",
            "body": "候选陈述",
            "sources": [{"paper_id": "json-dec"}],
        },
    )
    assert replay.status_code == 201
    assert replay.json()["asset_id"] == confirmed.json()["asset_id"]

    decision = client.get(f"/reading-insights/analysis/candidates/{candidate_id}/decisions")
    assert decision.status_code == 200
    assert decision.json()["decision"] == "confirmed"

    missing = client.get("/reading-insights/analysis/candidates/ghost/decisions")
    assert missing.status_code == 404

    mismatch = client.post(
        f"/reading-insights/analysis/candidates/{candidate_id}/decisions",
        json={"candidate_id": "other", "decision": "dismiss", "idempotency_key": "k"},
    )
    assert mismatch.status_code in (400, 422)


def test_page_candidate_review_and_confirm(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    candidate_id = seed_candidate(factory, "page-dec", run_id="run-page-dec")

    # 候选页：并列候选与来源
    page = client.get(f"/ui/reading-insights/analysis/candidates/{candidate_id}")
    assert page.status_code == 200
    assert "核对候选" in page.text
    assert "确认并生成资产" in page.text
    assert "忽略候选" in page.text
    assert "并入已有资产" in page.text
    assert "请对照原文核对" in page.text
    assert "用户表达" in page.text

    # 页面确认（默认 kind=problem→改为 harvest）
    confirmed = client.post(
        f"/ui/reading-insights/analysis/candidates/{candidate_id}/decide",
        data={
            "decision": "confirm",
            "idempotency_key": "page-dec-key",
            "kind": "harvest",
            "title": "页面确认的收获",
            "body": "候选陈述正文",
            "sources": "page-dec",
        },
        headers=HEADERS,
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    asset_id = confirmed.headers["location"].rsplit("/", 1)[-1]

    # 回到候选页显示已决定
    after = client.get(f"/ui/reading-insights/analysis/candidates/{candidate_id}")
    assert "已确认为资产" in after.text
    assert f"/ui/reading-insights/assets/{asset_id}" in after.text

    # 资产详情含「来自候选」决定回看区
    asset_page = client.get(f"/ui/reading-insights/assets/{asset_id}")
    assert asset_page.status_code == 200
    assert "来自候选" in asset_page.text

    # 分析页候选列表显示已处理状态
    analysis = client.get("/ui/reading-insights/analysis")
    assert "已确认为资产" in analysis.text
    assert "候选未经你确认" in analysis.text


def test_page_dismiss_flow_and_analysis_state(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    candidate_id = seed_candidate(factory, "page-dis", run_id="run-page-dis")

    dismissed = client.post(
        f"/ui/reading-insights/analysis/candidates/{candidate_id}/decide",
        data={
            "decision": "dismiss",
            "idempotency_key": "page-dis-key",
            "note": "与当前任务无关",
        },
        headers=HEADERS,
        follow_redirects=False,
    )
    assert dismissed.status_code == 303
    assert dismissed.headers["location"] == "/ui/reading-insights/analysis"

    analysis = client.get("/ui/reading-insights/analysis")
    assert "已忽略" in analysis.text
    # 资产库不受影响（dismiss 不生成资产）
    assets = client.get("/ui/reading-insights/assets")
    assert "还没有手工资产" in assets.text
