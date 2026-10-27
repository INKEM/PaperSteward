"""跨文献知识版回顾（R2-05）：服务、导出与页面闭环验证。"""

import json
import re
from datetime import UTC, date, datetime

import pytest
from test_reading_analysis_runner import connect_llm
from test_reading_insight_projection import T_DONE, seed_archive, seed_paper, system_content
from test_ui_flows import HEADERS, wait

from conftest import activate
from papersteward.config import Settings
from papersteward.core.entities import Run
from papersteward.errors import NotAuthorizedError
from papersteward.llm.errors import LLMError
from papersteward.llm.provider import FakeLLM
from papersteward.reading.insights.authorization import grant_analysis_authorization
from papersteward.reading.insights.contracts import (
    SYNTHESIS_PURPOSE,
    InsightPeriodRequest,
    InsightQueryRequest,
)
from papersteward.reading.insights.materials import (
    build_synthesis_manifest,
    create_preview_token,
    store_manifest,
)
from papersteward.reading.insights.reports import read_report
from papersteward.reading.insights.synthesis import SynthesisRunParamsView, run_insight_synthesis
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.repositories import RunRepository

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
ANCHOR = date(2026, 10, 22)
SETTINGS = Settings()

TOPIC = {
    "question": "双盲流程在多大规模下仍可复用？",
    "understanding": "材料只支持单篇实验设置下的判断，尚无可比规模数据。",
    "supporting_indexes": [0],
    "limiting_indexes": [1, 2],
    "comparison": "两篇材料的研究设置不同，暂不能直接比较。",
    "unverified": "缺少部署测量。",
}


def payload(
    topics: list[dict] | None = None,
    *,
    insufficient: bool = False,
    note: str | None = None,
    links: list[dict] | None = None,
) -> dict:
    return {
        "topics": topics or [],
        "open_questions": [],
        "candidate_links": links or [],
        "insufficient_material": insufficient,
        "note": note,
    }


def seed_note_paper(factory, paper_id: str, note: str = "双盲流程完整") -> None:
    with session_scope(factory) as session:
        seed_paper(session, paper_id, first_seen_at=T_DONE)
        seed_archive(
            session,
            paper_id,
            archived_at=T_DONE,
            content=system_content(title=f"知识-{paper_id}", completed_at=T_DONE),
            rating_col=4,
            note_col=note,
            ratings=[(T_DONE, 4, note)],
        )


def query(kind: str = "week", anchor: date = ANCHOR) -> InsightQueryRequest:
    return InsightQueryRequest(
        period=InsightPeriodRequest(kind=kind, timezone="Asia/Shanghai", anchor_date=anchor)
    )


def setup_synthesis(
    factory,
    paper_notes: dict[str, str],
    *,
    kinds: list[str] | None = None,
    now: datetime = NOW,
    seed: bool = True,
) -> tuple[SynthesisRunParamsView, str, str, str]:
    connect_llm(factory)
    if seed:
        for paper_id, note in paper_notes.items():
            seed_note_paper(factory, paper_id, note)
    selected = kinds or ["archive_metadata", "user_notes"]
    with session_scope(factory) as session:
        content = build_synthesis_manifest(session, query=query(), kinds=selected, now=now)
        row, _ = store_manifest(session, content, now=now)
        token = create_preview_token(
            session, manifest_id=row.id, now=now, purpose=SYNTHESIS_PURPOSE
        )
        grant = grant_analysis_authorization(
            session,
            scope_mode="workspace",
            material_classes=selected,
            settings=SETTINGS,
            now=now,
            purpose=SYNTHESIS_PURPOSE,
        )
        manifest_id, manifest_hash, grant_id = row.id, row.manifest_hash, grant.grant_id
    params = SynthesisRunParamsView(
        period="week",
        mode="as_recorded",
        anchor_date=ANCHOR,
        start_date=None,
        end_date=None,
        tag=None,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        grant_id=grant_id,
    )
    return params, manifest_id, manifest_hash, token.token


def seed_run(factory, run_id: str) -> None:
    with session_scope(factory) as session:
        RunRepository(session).add(
            Run(id=run_id, run_type="ui.insight_synthesis", started_at=NOW, stats_json={})
        )


def run_synthesis(factory, params, provider, monkeypatch, run_id: str = "syn-1") -> dict:
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda *args, **kwargs: provider,
    )
    seed_run(factory, run_id)
    return run_insight_synthesis(
        factory,
        SETTINGS,
        params=params,
        run_id=run_id,
        should_abort=lambda: False,
        progress=lambda **values: None,
    )


def test_synthesis_report_has_fixed_facts_and_traceable_topics(insights_factory, monkeypatch):
    factory = insights_factory
    params, _manifest_id, _manifest_hash, _token = setup_synthesis(
        factory, {"paper-a": "双盲流程完整", "paper-b": "样本只有 40 人"}
    )
    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch)
    assert generated["report_id"]
    assert generated["topics"] == 1
    assert generated["materials_used"] == 4

    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
    assert detail.contract == "reading-insights.report/2"
    assert detail.content.metrics.readings_total == 2
    topic = detail.content.synthesis.topics[0]
    assert topic.question == TOPIC["question"]
    assert topic.independent_sources == 2
    assert topic.private is True
    assert "user_notes" in {ref.kind for ref in topic.limiting_refs}
    assert detail.content.analysis.materials_used == 4
    assert detail.content.analysis.partial is False
    # 个人导出包含私人归纳；分享导出按来源链整条排除。
    personal = detail.model_dump(mode="json")
    assert personal["content"]["synthesis"]["topics"][0]["private"] is True


def test_share_export_excludes_private_synthesis_by_default(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {"paper-a": "双盲流程完整"})
    private_note = "来自私人备注的模型说明"
    provider = FakeLLM([json.dumps(payload([TOPIC], note=private_note), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-export")
    from papersteward.reading.insights.export import export_report

    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
    shared = export_report(detail, "markdown")
    assert "分享副本已排除" in shared
    assert TOPIC["question"] not in shared
    assert private_note not in shared
    assert "模型说明因涉及私人材料已省略" in shared
    personal = export_report(detail, "markdown", include_private=True)
    assert TOPIC["question"] in personal
    assert private_note in personal
    assert "个人副本包含" in personal


def test_synthesis_rejects_unverifiable_refs_and_keeps_valid(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {"paper-a": "双盲流程完整"})
    bad = {
        **TOPIC,
        "supporting_indexes": [99],
        "limiting_indexes": [],
    }
    valid = {**TOPIC, "limiting_indexes": [1]}
    provider = FakeLLM([json.dumps(payload([bad, valid]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-refs")
    assert generated["topics"] == 1
    assert generated["rejected_items"] == 1
    assert generated["rejected_refs"] == 1


def test_synthesis_insufficient_material_saves_facts_without_model_call(
    insights_factory, monkeypatch
):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {}, kinds=["archive_metadata"], seed=False)
    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-empty")
    assert provider.call_count == 0
    assert generated["insufficient_material"] is True
    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
    assert detail.content.synthesis.insufficient_material is True
    assert detail.content.metrics.readings_total == 0


def test_synthesis_requires_grant_and_never_constructs_provider(insights_factory, monkeypatch):
    factory = insights_factory
    params, manifest_id, manifest_hash, token = setup_synthesis(
        factory, {"paper-a": "双盲流程完整"}
    )
    with session_scope(factory) as session:
        from papersteward.reading.insights.authorization import revoke_analysis_authorization
        from papersteward.storage.insights import ReadingAnalysisGrantRepository

        grant = ReadingAnalysisGrantRepository(session).active_for_purpose(SYNTHESIS_PURPOSE)[0]
        revoke_analysis_authorization(session, grant_id=grant.id, now=NOW)

    calls = {"count": 0}

    def provider(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("未授权路径不应构造 provider")

    monkeypatch.setattr("papersteward.reading.insights.authorization.build_llm_provider", provider)
    seed_run(factory, "syn-noauth")
    with pytest.raises(NotAuthorizedError, match="授权"):
        run_insight_synthesis(
            factory,
            SETTINGS,
            params=params,
            run_id="syn-noauth",
            should_abort=lambda: False,
            progress=lambda **values: None,
        )
    assert calls["count"] == 0


def test_unknown_report_contract_is_rejected(insights_factory):
    assert insights_factory is not None
    from papersteward.core.errors import InvariantViolation
    from papersteward.reading.insights.reports import parse_report_content
    from papersteward.storage.delivery import content_hash

    payload_data = {"contract": "reading-insights.report/9"}
    with pytest.raises(InvariantViolation, match="未知报告契约版本"):
        parse_report_content("reading-insights.report/9", payload_data)
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_synthesis_page_preview_grant_and_submit(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(migrated_engine)
    connect_llm(factory)
    seed_note_paper(factory, "ui-a")
    # 授权页表单。
    grant = client.post(
        "/ui/reading-insights/synthesis/grants",
        data={
            "scope_mode": "workspace",
            "cls_archive_metadata": "1",
            "cls_user_notes": "1",
            "acknowledged": "1",
        },
        headers=HEADERS,
    )
    assert grant.status_code == 200
    assert "已保存知识版回顾授权" in grant.text
    # 预览：只读本地，不发送模型请求。
    preview = client.post(
        "/ui/reading-insights/synthesis/preview",
        data={
            "kind": "week",
            "anchor_date": "2026-10-22",
            "mode": "as_recorded",
            "kind_archive_metadata": "1",
            "kind_user_notes": "1",
        },
        headers=HEADERS,
    )
    assert preview.status_code == 200
    assert "材料预览" in preview.text
    match = re.search(r'name="preview_token" value="([0-9a-f]+)"', preview.text)
    assert match is not None
    token = match.group(1)

    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda *args, **kwargs: provider,
    )
    submitted = client.post(
        "/ui/reading-insights/synthesis/runs",
        data={"preview_token": token, "run_id": "syn-ui"},
        headers=HEADERS,
    )
    # TestClient 默认跟随 303，最终落在运行进度页。
    assert submitted.status_code == 200
    assert str(submitted.url).endswith("/ui/runs/syn-ui")
    run = wait(client.app.state.ui_flows, "syn-ui")
    assert run.status.value == "succeeded"
    report_id = run.stats_json["generated"]["report_id"]
    page = client.get(f"/ui/reading-insights/reports/{report_id}")
    assert page.status_code == 200
    assert "知识整理" in page.text
    assert TOPIC["question"] in page.text
    assert "实际分析覆盖" in page.text
    api = client.get(f"/reading-insights/reports/{report_id}")
    assert api.status_code == 200
    assert api.json()["content"]["contract"] == "reading-insights.report/2"
    assert api.json()["content"]["synthesis"]["topics"][0]["private"] is True
    shared = client.get(f"/reading-insights/reports/{report_id}/export?format=markdown")
    assert "分享副本已排除" in shared.text
    assert TOPIC["question"] not in shared.text
    personal = client.get(
        f"/reading-insights/reports/{report_id}/export?format=markdown&include_private=true"
    )
    assert TOPIC["question"] in personal.text
    export_page = client.get(f"/ui/reading-insights/export?report_id={report_id}")
    assert "条归纳依赖私人材料" in export_page.text


def test_manifest_includes_verified_candidates_and_confirmed_assets(insights_factory, monkeypatch):
    factory = insights_factory
    from papersteward.reading.insights.assets import create_asset
    from papersteward.reading.insights.contracts import AssetCreateInput
    from papersteward.storage.insights import (
        ReadingAnalysisCandidateRepository,
        ReadingAnalysisDecisionRepository,
    )
    from papersteward.storage.models import (
        ReadingAnalysisCandidateRow,
        ReadingAnalysisDecisionRow,
    )

    connect_llm(factory)
    seed_note_paper(factory, "paper-a")
    with session_scope(factory) as session:
        view = create_asset(
            session,
            AssetCreateInput.model_validate(
                {
                    "kind": "harvest",
                    "title": "种子资产",
                    "body": "可复用的收获",
                    "sources": [{"paper_id": "paper-a"}],
                    "idempotency_key": "syn-asset-seed",
                }
            ),
            now=T_DONE,
        )
        session.add(
            ReadingAnalysisCandidateRow(
                id="cand-1",
                run_id="run-seed",
                manifest_id="m-seed",
                manifest_hash="f" * 64,
                grant_id="g-seed",
                model_config_version="v-seed",
                paper_id="paper-a",
                kind="harvest",
                nature="user_expression",
                statement="用户确认的单篇结果",
                conditions=None,
                limitations=None,
                quote=None,
                quote_source_id=None,
                source_refs_json=[],
                verification="confirmed",
                stale=0,
                created_at=T_DONE,
            )
        )
        session.flush()
        session.add(
            ReadingAnalysisDecisionRow(
                id="dec-1",
                candidate_id="cand-1",
                decision="confirmed",
                asset_id=view.asset_id,
                asset_revision=view.revision.revision,
                paper_id="paper-a",
                kind="harvest",
                statement_hash="a" * 64,
                note=None,
                decided_at=T_DONE,
            )
        )
    with session_scope(factory) as session:
        content = build_synthesis_manifest(
            session,
            query=query(),
            kinds=["archive_metadata", "asset_content", "verified_candidate"],
            now=NOW,
        )
        candidates = ReadingAnalysisCandidateRepository(session).by_run("run-seed")
        decisions = ReadingAnalysisDecisionRepository(session).find_by_candidate("cand-1")
    kinds = [ref.kind for ref in content.materials]
    assert "asset_content" in kinds
    assert "verified_candidate" in kinds
    assert candidates and decisions is not None
    assert content.coverage.selected_papers == 1


def test_monthly_synthesis_does_not_require_weekly_report(insights_factory, monkeypatch):
    factory = insights_factory
    from papersteward.reading.insights.reports import list_reports

    connect_llm(factory)
    seed_note_paper(factory, "paper-month")
    month_query = query("month", date(2026, 10, 15))
    with session_scope(factory) as session:
        content = build_synthesis_manifest(
            session, query=month_query, kinds=["archive_metadata", "user_notes"], now=NOW
        )
        row, _ = store_manifest(session, content, now=NOW)
        grant = grant_analysis_authorization(
            session,
            scope_mode="workspace",
            material_classes=["archive_metadata", "user_notes"],
            settings=SETTINGS,
            now=NOW,
            purpose=SYNTHESIS_PURPOSE,
        )
        manifest_id, manifest_hash, grant_id = row.id, row.manifest_hash, grant.grant_id
    params = SynthesisRunParamsView(
        period="month",
        mode="as_recorded",
        anchor_date=date(2026, 10, 15),
        start_date=None,
        end_date=None,
        tag=None,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        grant_id=grant_id,
    )
    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-month")
    assert generated["report_id"]
    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
        listing = list_reports(session, limit=10, offset=0)
    assert detail.period_label.startswith("2026-10")
    assert detail.content.metrics.readings_total == 1
    assert listing["total"] == 1
    assert listing["items"][0].contract == "reading-insights.report/2"


def test_sanitized_knowledge_report_removes_private_items(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {"paper-a": "双盲流程完整"})
    public_link = {
        "statement": "可以把评估流程迁移到相邻任务，尚待验证。",
        "link_kind": "research_opportunity",
        "source_indexes": [0],
        "caution": "仅基于单篇材料。",
    }
    provider = FakeLLM([json.dumps(payload([TOPIC], links=[public_link]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-sanitize")
    from papersteward.reading.insights.reports import save_sanitized_report

    with session_scope(factory) as session:
        original = read_report(session, generated["report_id"])
        sanitized = save_sanitized_report(session, generated["report_id"], now=NOW)
    assert original.content.synthesis.topics
    assert sanitized.report_id != original.report_id
    assert sanitized.contract == "reading-insights.report/2"
    assert sanitized.content.synthesis.topics == []
    assert sanitized.content.synthesis.redacted_private_items == 1
    assert sanitized.content.synthesis.candidate_links
    with session_scope(factory) as session:
        again = read_report(session, generated["report_id"])
    assert again.content.synthesis.topics


def test_stale_materials_are_disclosed_not_hidden(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {"paper-a": "双盲流程完整", "paper-b": "样本只有 40 人"})
    from papersteward.storage.models import ArchiveSnapshotRow

    with session_scope(factory) as session:
        session.delete(session.get(ArchiveSnapshotRow, "paper-b"))
    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-stale")
    assert generated["partial"] is True
    # 归档删除使元数据锚点失效；评分备注仍按自己的版本锚点可用。
    assert generated["materials_stale"] == 1
    assert generated["materials_used"] == 3
    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
    assert detail.content.analysis.partial is True
    assert detail.content.analysis.materials_stale == 1
    assert detail.content.analysis.materials_used == 3


def test_model_failure_saves_no_report(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {"paper-a": "双盲流程完整"})
    from papersteward.llm.errors import LLMProviderError
    from papersteward.storage.insights import ReadingReportRepository

    provider = FakeLLM(
        ["not-json"],
        failures=[LLMProviderError("服务不可用")] * 6,
    )
    with pytest.raises(LLMError):
        run_synthesis(factory, params, provider, monkeypatch, run_id="syn-fail")
    with session_scope(factory) as session:
        assert ReadingReportRepository(session).get_by_run("syn-fail") is None


def test_synthesis_json_api_manifest_grant_and_run(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(migrated_engine)
    connect_llm(factory)
    seed_note_paper(factory, "api-a")
    page = client.get("/ui/reading-insights/synthesis")
    assert page.status_code == 200
    assert "知识版回顾" in page.text
    authorization = client.get(
        "/reading-insights/analysis/authorization", params={"purpose": "reading_synthesis"}
    )
    assert authorization.status_code == 200
    assert authorization.json()["grants"] == []
    grant = client.post(
        "/reading-insights/analysis/grants",
        json={
            "purpose": "reading_synthesis",
            "scope_mode": "workspace",
            "material_classes": ["archive_metadata", "user_notes"],
            "acknowledged": True,
        },
    )
    assert grant.status_code == 201
    assert grant.json()["purpose"] == "reading_synthesis"
    manifest = client.post(
        "/reading-insights/synthesis/manifests",
        json={
            "period": {
                "kind": "week",
                "timezone": "Asia/Shanghai",
                "anchor_date": "2026-10-22",
            },
            "mode": "as_recorded",
            "kinds": ["archive_metadata", "user_notes"],
        },
    )
    assert manifest.status_code == 201
    body = manifest.json()
    assert body["preview"]["purpose"] == "reading_synthesis"
    assert body["manifest"]["scope_request"]["period"]["kind"] == "week"
    token = body["preview"]["token"]
    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda *args, **kwargs: provider,
    )
    submitted = client.post(
        "/reading-insights/synthesis/runs",
        json={"preview_token": token, "run_id": "syn-api"},
    )
    assert submitted.status_code == 202
    run = wait(client.app.state.ui_flows, "syn-api")
    assert run.status.value == "succeeded"
    assert run.stats_json["generated"]["report_id"]


def test_asset_delete_preview_finds_private_synthesis_lineage(insights_factory, monkeypatch):
    factory = insights_factory
    from datetime import timedelta

    from papersteward.reading.insights.assets import create_asset, delete_preview
    from papersteward.reading.insights.contracts import AssetCreateInput

    connect_llm(factory)
    seed_note_paper(factory, "paper-a")
    old = T_DONE - timedelta(days=10)
    with session_scope(factory) as session:
        view = create_asset(
            session,
            AssetCreateInput.model_validate(
                {
                    "kind": "harvest",
                    "title": "早期资产",
                    "body": "资产正文",
                    "sources": [{"paper_id": "paper-a"}],
                    "idempotency_key": "syn-asset-old",
                }
            ),
            now=old,
        )
        asset_id = view.asset_id
    with session_scope(factory) as session:
        content = build_synthesis_manifest(
            session,
            query=query(),
            kinds=["archive_metadata", "asset_content"],
            now=NOW,
        )
        row, _ = store_manifest(session, content, now=NOW)
        grant = grant_analysis_authorization(
            session,
            scope_mode="workspace",
            material_classes=["archive_metadata", "asset_content"],
            settings=SETTINGS,
            now=NOW,
            purpose=SYNTHESIS_PURPOSE,
        )
        manifest_id, manifest_hash, grant_id = row.id, row.manifest_hash, grant.grant_id
    params = SynthesisRunParamsView(
        period="week",
        mode="as_recorded",
        anchor_date=ANCHOR,
        start_date=None,
        end_date=None,
        tag=None,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        grant_id=grant_id,
    )
    topic = {**TOPIC, "supporting_indexes": [0], "limiting_indexes": [1]}
    provider = FakeLLM([json.dumps(payload([topic]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-asset-del")
    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
        preview = delete_preview(session, asset_id)
    assert all(ref.asset_id != asset_id for ref in detail.content.assets)
    listed = {item["report_id"]: item["fields"] for item in preview["private_copies"]}
    assert listed[detail.report_id] == ["synthesis"]


def test_synthesis_input_budget_is_disclosed_not_silent(insights_factory, monkeypatch):
    factory = insights_factory
    params, *_ = setup_synthesis(factory, {"paper-a": "双盲流程完整"})
    monkeypatch.setattr("papersteward.reading.insights.synthesis.SYNTHESIS_MAX_INPUT_CHARS", 1)
    provider = FakeLLM([json.dumps(payload([TOPIC]), ensure_ascii=False)])
    generated = run_synthesis(factory, params, provider, monkeypatch, run_id="syn-budget")
    assert provider.call_count == 0
    assert generated["materials_used"] == 0
    assert generated["partial"] is True
    assert generated["insufficient_material"] is True
    with session_scope(factory) as session:
        detail = read_report(session, generated["report_id"])
    assert detail.content.analysis.omitted_materials == 2
    assert detail.content.analysis.partial is True
