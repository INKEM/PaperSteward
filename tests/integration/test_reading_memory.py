"""R2-06：跨期门槛、用户决定、发送边界与本地研究记忆闭环。"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_reading_analysis_runner import connect_llm
from test_reading_insight_projection import import_content, seed_archive, seed_paper, system_content
from test_ui_flows import HEADERS, wait

from conftest import activate
from papersteward.config import Settings
from papersteward.core.entities import Run
from papersteward.errors import ConflictError, NotAuthorizedError
from papersteward.llm.provider import FakeLLM
from papersteward.reading.insights.authorization import (
    grant_analysis_authorization,
    revoke_analysis_authorization,
)
from papersteward.reading.insights.contracts import (
    AssetCreateInput,
    AssetSourceInput,
    ReadTimeInput,
    StructuredFeedbackInput,
)
from papersteward.reading.insights.materials import store_manifest
from papersteward.reading.insights.memory import (
    decide_memory,
    delete_memory,
    get_memory,
    list_memory,
    run_insight_memory,
)
from papersteward.reading.insights.memory_contracts import MEMORY_PURPOSE, MemoryDecisionInput
from papersteward.reading.insights.memory_evidence import (
    begin_memory_snapshot,
    build_memory_manifest,
    group_memory_evidence,
)
from papersteward.reading.insights.structured_feedback import revise_structured_feedback
from papersteward.reading.insights.timeline import record_reading_time
from papersteward.storage.db import session_scope
from papersteward.storage.models import ReadingDerivedCopyRow, ReadingMemoryRevisionRow
from papersteward.storage.repositories import RunRepository

NOW = datetime(2026, 10, 27, 2, tzinfo=UTC)
TIMES = [
    datetime(2026, 8, 15, 2, tzinfo=UTC),
    datetime(2026, 9, 18, 2, tzinfo=UTC),
    datetime(2026, 10, 20, 2, tzinfo=UTC),
]
KEY = "value:method_reusable"
KINDS = ["archive_metadata", "user_rating", "structured_reasons"]


def seed(factory, *, times=None, batch=False, single=False, contrary=False, unknown=False):
    times = times or TIMES
    with session_scope(factory) as session:
        for index, stamp in enumerate(times):
            paper_id = f"m-{index}"
            archived = NOW - timedelta(days=1) if batch else stamp
            seed_paper(session, paper_id, first_seen_at=archived)
            seed_archive(
                session,
                paper_id,
                archived_at=archived,
                content=import_content(title=paper_id)
                if unknown
                else system_content(title=paper_id, completed_at=stamp),
                ratings=[(archived if batch else stamp, 5, "不能外发的私人备注")],
            )
            revise_structured_feedback(
                session,
                paper_id,
                StructuredFeedbackInput(
                    expected_revision=0,
                    idempotency_key=f"reason-{index}",
                    value_sources=("method_reusable",),
                    limited_reasons=("evidence_doubtful",) if contrary and index == 2 else (),
                    context_note="同一项目" if single else f"项目-{index}",
                ),
                now=archived if batch else stamp,
            )


def manifest(factory, *, kinds=None, grant=True):
    from papersteward.llm.runtime import CONFIG_KEY
    from papersteward.storage.repositories import ConfigRepository

    with session_scope(factory) as session:
        configured = ConfigRepository(session).latest(CONFIG_KEY) is not None
    if not configured:
        connect_llm(factory)
    with session_scope(factory) as session:
        content = build_memory_manifest(session, kinds=kinds or KINDS, exclusions=[], now=NOW)
        row, _ = store_manifest(session, content, now=NOW)
        authorization = (
            grant_analysis_authorization(
                session,
                scope_mode="workspace",
                material_classes=kinds or KINDS,
                settings=Settings(),
                now=datetime.now(UTC),
                purpose=MEMORY_PURPOSE,
            )
            if grant
            else None
        )
        return row.id, row.manifest_hash, authorization.grant_id if authorization else "missing"


def groups(factory, *, kinds=None):
    with session_scope(factory) as session:
        content = build_memory_manifest(session, kinds=kinds or KINDS, exclusions=[], now=NOW)
    with session_scope(factory) as session:
        begin_memory_snapshot(session)
        return group_memory_evidence(session, content)


def output(key=KEY, statement="多次反馈提到可复用的方法，仍需核对适用任务"):
    return json.dumps(
        {
            "candidates": [
                {
                    "group_key": key,
                    "statement": statement,
                    "uncertainty": "未覆盖其他任务，限制反馈需单独核对",
                }
            ],
            "insufficient_material": False,
        },
        ensure_ascii=False,
    )


def run(factory, spec, provider, monkeypatch, identity="memory-run", should_abort=lambda: False):
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda *args, **kwargs: provider,
    )
    with session_scope(factory) as session:
        RunRepository(session).add(
            Run(id=identity, run_type="ui.insight_memory", started_at=NOW, stats_json={})
        )
    return run_insight_memory(
        factory,
        Settings(),
        manifest_id=spec[0],
        manifest_hash=spec[1],
        grant_id=spec[2],
        run_id=identity,
        should_abort=should_abort,
        progress=lambda **values: None,
    )


def decision(revision, action, **kwargs):
    return MemoryDecisionInput(
        expected_revision=revision,
        idempotency_key=f"decide-{revision}-{action}",
        action=action,
        **kwargs,
    )


def test_stable_threshold_uses_actual_feedback_months_and_keeps_counterevidence(insights_factory):
    seed(insights_factory, contrary=True)
    group = next(item for item in groups(insights_factory) if item.key == KEY)
    assert group.stable_eligible and group.kind == "value_standard"
    assert group.distinct_papers == 3
    assert group.feedback_months == ["2026-08", "2026-09", "2026-10"]
    assert any(item.role == "contrary" for item in group.evidence)
    assert group.windows["recent_8_weeks"] == 2
    assert group.windows["previous_8_weeks"] == 1
    assert "限制" in "".join(group.limitations)


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"times": TIMES[:1]}, "单篇"),
        ({"batch": True}, "集中补录"),
        ({"single": True}, "单任务"),
        ({"times": [TIMES[-1] + timedelta(days=index) for index in range(3)]}, "同月"),
    ],
)
def test_weak_samples_are_only_interest(insights_factory, options, reason):
    seed(insights_factory, **options)
    group = next(item for item in groups(insights_factory) if item.key == KEY)
    assert group.kind == "interest", reason
    assert not group.stable_eligible


def test_recalled_old_reading_does_not_create_old_feedback(insights_factory):
    seed(insights_factory, batch=True, unknown=True)
    with session_scope(insights_factory) as session:
        for index in range(3):
            record_reading_time(
                session,
                f"m-{index}",
                ReadTimeInput.model_validate(
                    {
                        "precision": "month",
                        "month": f"202{index}-01",
                        "expected_revision": 0,
                        "idempotency_key": f"recall-{index}",
                    }
                ),
                now=NOW - timedelta(hours=1),
                expected_revision=0,
                idempotency_key=f"recall-{index}",
            )
    group = next(item for item in groups(insights_factory) if item.key == KEY)
    assert group.feedback_months == ["2026-10"]
    assert not group.stable_eligible
    assert all(item.reading_basis == "declared" for item in group.evidence)
    assert "回溯声明" in "".join(group.limitations)


def test_unknown_dates_are_not_guessed(insights_factory):
    seed(insights_factory, unknown=True)
    group = next(item for item in groups(insights_factory) if item.key.startswith("topic:"))
    assert all(item.reading_precision == "unknown" for item in group.evidence)
    assert "未知" in "".join(group.limitations)


def test_runner_saves_pending_with_versioned_sources_and_no_private_notes(
    insights_factory, monkeypatch
):
    seed(insights_factory)
    spec = manifest(insights_factory)
    provider = FakeLLM([output()])
    result = run(insights_factory, spec, provider, monkeypatch)
    assert result["candidates"] == 1
    assert "不能外发的私人备注" not in provider.calls[0]
    assert "项目-0" not in provider.calls[0]  # 理由类别只授权码和有无标记，不含语境原文。
    with session_scope(insights_factory) as session:
        item = list_memory(session)[0]
        assert item.status == "pending" and not item.usable
        assert not item.recommendation_enabled
        assert len(item.group.evidence) >= 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReadingDerivedCopyRow)
                .where(ReadingDerivedCopyRow.derived_kind == "memory")
            )
            == 6
        )
        assert item.provenance["prompt_version"] == "reading_profile_inference/v1"
    # 同清单重跑不重复发送或产生版本。
    repeated = FakeLLM([output()])
    run(insights_factory, spec, repeated, monkeypatch, "memory-repeat")
    assert repeated.call_count == 1  # 未输出的主题组仍可重试；已保存价值组不会再生成。
    with session_scope(insights_factory) as session:
        assert len(list_memory(session)) == 1
        assert get_memory(session, item.memory_id).revision == 1


def test_forged_group_is_rejected(insights_factory, monkeypatch):
    seed(insights_factory)
    spec = manifest(insights_factory)
    result = run(insights_factory, spec, FakeLLM([output("invented")]), monkeypatch)
    assert result["rejected_items"] == 1
    with session_scope(insights_factory) as session:
        assert list_memory(session) == []


def test_no_grant_constructs_no_provider(insights_factory, monkeypatch):
    seed(insights_factory)
    spec = manifest(insights_factory, grant=False)
    provider = FakeLLM([output()])
    with pytest.raises(NotAuthorizedError):
        run(insights_factory, spec, provider, monkeypatch)
    assert provider.call_count == 0


@pytest.mark.parametrize("action", ["not_preference", "disable", "defer", "task_only", "confirm"])
def test_user_decisions_survive_reanalysis(insights_factory, monkeypatch, action):
    seed(insights_factory)
    run(insights_factory, manifest(insights_factory), FakeLLM([output()]), monkeypatch)
    with session_scope(insights_factory, immediate=True) as session:
        identity = list_memory(session)[0].memory_id
        data = decision(1, action, statement="我只在评估设计任务中参考这个方法")
        saved = decide_memory(session, identity, data, now=NOW)
        replay = decide_memory(session, identity, data, now=NOW)
        assert saved.revision == replay.revision == 2
        with pytest.raises(ConflictError):
            decide_memory(
                session, identity, data.model_copy(update={"statement": "different"}), now=NOW
            )
    spec = manifest(insights_factory)
    provider = FakeLLM([output(statement="模型不得覆盖这个用户修正")])
    run(insights_factory, spec, provider, monkeypatch, "after-decision")
    assert "我只在评估设计任务中" not in provider.calls[0]
    assert KEY not in provider.calls[0]
    with session_scope(insights_factory) as session:
        latest = get_memory(session, identity)
        assert latest.statement == saved.statement and latest.status == saved.status
        assert latest.revision == 2
        assert get_memory(session, identity, 1).statement != latest.statement
        assert list_memory(session, keyword="评估设计")
        assert list_memory(session, start="2026-09-01", end="2026-10-01")


def test_new_evidence_requests_review_without_overwriting_user(insights_factory, monkeypatch):
    seed(insights_factory)
    run(insights_factory, manifest(insights_factory), FakeLLM([output()]), monkeypatch)
    with session_scope(insights_factory, immediate=True) as session:
        identity = list_memory(session)[0].memory_id
        decide_memory(
            session, identity, decision(1, "confirm", statement="用户明确的审慎措辞"), now=NOW
        )
        revise_structured_feedback(
            session,
            "m-2",
            StructuredFeedbackInput(
                expected_revision=1,
                idempotency_key="new-counter",
                value_sources=("method_reusable",),
                limited_reasons=("evidence_doubtful",),
            ),
            now=NOW - timedelta(minutes=1),
        )
    run(
        insights_factory,
        manifest(insights_factory),
        FakeLLM([output("invented")]),
        monkeypatch,
        "new-evidence",
    )
    with session_scope(insights_factory) as session:
        item = get_memory(session, identity)
        assert item.status == "needs_review"
        assert item.statement == "用户明确的审慎措辞"
        assert any(evidence.role == "contrary" for evidence in item.group.evidence)


def test_old_preview_cannot_overwrite_new_user_decision(insights_factory, monkeypatch):
    seed(insights_factory)
    run(insights_factory, manifest(insights_factory), FakeLLM([output()]), monkeypatch)
    spec = manifest(insights_factory)
    with session_scope(insights_factory, immediate=True) as session:
        identity = list_memory(session)[0].memory_id
        decide_memory(session, identity, decision(1, "disable"), now=NOW)
    provider = FakeLLM([output()])
    with pytest.raises(ConflictError, match="画像决定已更新"):
        run(insights_factory, spec, provider, monkeypatch, "stale-preview")
    assert provider.call_count == 0


def test_revoke_before_repair_prevents_second_send(insights_factory, monkeypatch):
    seed(insights_factory)
    spec = manifest(insights_factory)

    class RevokeOnReply(FakeLLM):
        def complete(self, prompt, *, timeout_s):
            result = super().complete(prompt, timeout_s=timeout_s)
            with session_scope(insights_factory, immediate=True) as session:
                revoke_analysis_authorization(session, grant_id=spec[2], now=NOW)
            return result

    provider = RevokeOnReply(["{}", output()])
    with pytest.raises(NotAuthorizedError):
        run(insights_factory, spec, provider, monkeypatch)
    assert provider.call_count == 1
    with session_scope(insights_factory) as session:
        assert list_memory(session) == []


def test_revoke_marks_memory_review_and_delete_cleans_all_versions(insights_factory, monkeypatch):
    seed(insights_factory)
    spec = manifest(insights_factory)
    run(insights_factory, spec, FakeLLM([output()]), monkeypatch)
    with session_scope(insights_factory, immediate=True) as session:
        identity = list_memory(session)[0].memory_id
        decide_memory(session, identity, decision(1, "confirm"), now=NOW)
        revoke_analysis_authorization(session, grant_id=spec[2], now=NOW)
        item = get_memory(session, identity)
        assert item.status == "needs_review" and not item.usable
        assert item.source_problems
    with session_scope(insights_factory, immediate=True) as session:
        delete_memory(session, identity)
        assert session.scalar(select(func.count()).select_from(ReadingMemoryRevisionRow)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(ReadingDerivedCopyRow)
                .where(ReadingDerivedCopyRow.derived_kind == "memory")
            )
            == 0
        )


def test_asset_source_delete_is_disclosed_and_requires_review(insights_factory, monkeypatch):
    from papersteward.reading.insights.assets import create_asset, delete_asset, delete_preview

    seed(insights_factory)
    with session_scope(insights_factory) as session:
        asset = create_asset(
            session,
            AssetCreateInput(
                kind="harvest",
                title="私人成果",
                body="可复用观察",
                sources=[AssetSourceInput(paper_id="m-0")],
                idempotency_key="asset-memory",
            ),
            now=NOW - timedelta(minutes=1),
        )
    kinds = [*KINDS, "asset_content"]
    run(insights_factory, manifest(insights_factory, kinds=kinds), FakeLLM([output()]), monkeypatch)
    with session_scope(insights_factory, immediate=True) as session:
        identity = list_memory(session)[0].memory_id
        assert identity in delete_preview(session, asset.asset_id)["memory_copies"]
        delete_asset(session, asset.asset_id)
        item = get_memory(session, identity)
        assert item.status == "needs_review" and item.source_problems


def test_json_and_browser_journey(client, migrated_engine, monkeypatch):
    from papersteward.storage.db import create_session_factory

    activate(client)
    factory = create_session_factory(migrated_engine)
    seed(factory)
    connect_llm(factory)
    provider = FakeLLM([output()])
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda *args, **kwargs: provider,
    )
    response = client.get("/ui/reading-insights/memory")
    assert response.status_code == 200 and "不参与检索" in response.text
    assert provider.call_count == 0
    grant = client.post(
        "/reading-insights/memory/grants",
        json={
            "purpose": MEMORY_PURPOSE,
            "scope_mode": "workspace",
            "material_classes": KINDS,
            "acknowledged": True,
        },
    )
    assert grant.status_code == 201
    preview = client.post("/reading-insights/memory/manifests", json={"kinds": KINDS})
    assert preview.status_code == 201
    response = client.post(
        "/reading-insights/memory/runs", json={"preview_token": preview.json()["preview"]["token"]}
    )
    assert response.status_code == 202
    wait(client.app.state.ui_flows, response.json()["run_id"])
    rows = client.get("/reading-insights/memory").json()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    identity = rows[0]["memory_id"]
    page = client.get(f"/ui/reading-insights/memory/{identity}")
    assert "反馈形成时间" in page.text and "系统归档／补录" in page.text
    assert "（系统记录）" in page.text
    assert "此前 8 周" in page.text and "独立文献 3 篇" in page.text
    key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text)[1]
    response = client.post(
        f"/ui/reading-insights/memory/{identity}/decisions",
        data={
            "expected_revision": "1",
            "idempotency_key": key,
            "action": "task_only",
            "statement": "仅用于这次评估",
            "explanation": "这是当前任务",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200 and "仅当前任务适用" in response.text
    assert "多次反馈" in client.get(f"/ui/reading-insights/memory/{identity}/revisions/1").text
    export = client.get(f"/ui/reading-insights/memory/{identity}/export")
    assert "私人推测" in export.text
    personal = client.get(
        f"/ui/reading-insights/memory/{identity}/export?confirm_private=yes"
    ).json()
    assert len(personal["revisions"]) == 2
    assert personal["revisions"][-1]["statement"] == "仅用于这次评估"


def test_recommendation_layers_do_not_import_memory():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/papersteward"
    for directory in (root / "sources", root / "scoring"):
        for file in directory.rglob("*.py"):
            for node in ast.walk(ast.parse(file.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert "memory" not in (node.module or ""), file


def test_clearing_all_value_reasons_requests_review(insights_factory, monkeypatch):
    seed(insights_factory)
    run(insights_factory, manifest(insights_factory), FakeLLM([output()]), monkeypatch)
    with session_scope(insights_factory, immediate=True) as session:
        identity = list_memory(session)[0].memory_id
        decide_memory(session, identity, decision(1, "confirm"), now=NOW)
        for index in range(3):
            revise_structured_feedback(
                session,
                f"m-{index}",
                StructuredFeedbackInput(
                    expected_revision=1,
                    idempotency_key=f"clear-{index}",
                ),
                now=NOW - timedelta(minutes=1),
            )
    run(
        insights_factory,
        manifest(insights_factory),
        FakeLLM([output("invented")]),
        monkeypatch,
        "clear-reasons",
    )
    with session_scope(insights_factory) as session:
        item = get_memory(session, identity)
        assert item.status == "needs_review"
        assert item.group.distinct_papers == 0
        assert all(evidence.role != "supporting" for evidence in item.group.evidence)


def test_cancel_and_provider_failure_save_no_memory(insights_factory, monkeypatch):
    from papersteward.errors import InvalidRequestError
    from papersteward.llm.errors import LLMTimeoutError

    seed(insights_factory)
    spec = manifest(insights_factory)
    provider = FakeLLM([output()])
    result = run(insights_factory, spec, provider, monkeypatch, should_abort=lambda: True)
    assert result["stopped_reason"] and provider.call_count == 0
    failing = FakeLLM([output()], failures=[LLMTimeoutError("PRIVATE_SECRET")] * 3)
    with pytest.raises(InvalidRequestError, match="研究记忆模型分析未完成") as error:
        run(insights_factory, spec, failing, monkeypatch, "failed-run")
    assert "PRIVATE_SECRET" not in str(error.value)
    with session_scope(insights_factory) as session:
        assert list_memory(session) == []


def test_once_authorization_preserves_preview_token_in_browser(
    client, migrated_engine, monkeypatch
):
    from papersteward.storage.db import create_session_factory

    activate(client)
    factory = create_session_factory(migrated_engine)
    seed(factory)
    connect_llm(factory)
    provider = FakeLLM([output()])
    monkeypatch.setattr(
        "papersteward.reading.insights.authorization.build_llm_provider",
        lambda *args, **kwargs: provider,
    )
    preview = client.post(
        "/ui/reading-insights/memory/preview",
        data={
            "kind_structured_reasons": "1",
            "kind_user_rating": "1",
            "kind_archive_metadata": "1",
        },
        headers=HEADERS,
    )
    assert preview.status_code == 200
    token = re.search(r'name="preview_token" value="([^"]+)"', preview.text)[1]
    manifest_id = re.search(r'name="manifest_id" value="([^"]+)"', preview.text)[1]
    authorization = client.post(
        "/ui/reading-insights/memory/grants",
        data={
            "scope_mode": "once",
            "acknowledged": "1",
            "manifest_id": manifest_id,
            "preview_token": token,
            "cls_structured_reasons": "1",
            "cls_user_rating": "1",
            "cls_archive_metadata": "1",
        },
        headers=HEADERS,
    )
    assert authorization.status_code == 200
    assert f'value="{token}"' in authorization.text
    assert 'value="once" selected' in authorization.text
    assert 'name="kind_structured_reasons" value="1" checked' in authorization.text
    assert provider.call_count == 0
    response = client.post(
        "/ui/reading-insights/memory/runs", data={"preview_token": token}, headers=HEADERS
    )
    assert response.status_code == 200
    identity = str(response.url).rsplit("/", 1)[-1]
    finished = wait(client.app.state.ui_flows, identity)
    assert finished.status.value == "succeeded"
    assert provider.call_count == 1


@pytest.mark.parametrize("count,eligible", [(2, False), (3, True)])
def test_exact_three_paper_two_month_boundary(insights_factory, count, eligible):
    times = [TIMES[0], TIMES[1], TIMES[1] + timedelta(days=1)]
    seed(insights_factory, times=times[:count])
    group = next(item for item in groups(insights_factory) if item.key == KEY)
    assert group.feedback_months == ["2026-08", "2026-09"]
    assert group.stable_eligible is eligible


def test_period_asset_search_does_not_hide_older_evidence(insights_factory):
    from papersteward.api.reading_memory import _list_body
    from papersteward.reading.insights.assets import create_asset

    seed(insights_factory, times=TIMES[:1])
    with session_scope(insights_factory) as session:
        older = create_asset(
            session,
            AssetCreateInput(
                kind="harvest",
                title="早期可找回的证据",
                body="老资产仍有价值",
                sources=[AssetSourceInput(paper_id="m-0")],
                idempotency_key="old-evidence",
            ),
            now=TIMES[0],
        )
        for index in range(201):
            create_asset(
                session,
                AssetCreateInput(
                    kind="harvest",
                    title=f"近期资产-{index}",
                    body="新近材料",
                    sources=[AssetSourceInput(paper_id="m-0")],
                    idempotency_key=f"new-evidence-{index}",
                ),
                now=TIMES[-1] + timedelta(seconds=index),
            )
    with session_scope(insights_factory) as session:
        body = _list_body(session, start="2026-08-01", end="2026-08-31")
        assert older.asset_id in body
        assert "早期可找回的证据" in body
        assert "近期资产-0" not in body
