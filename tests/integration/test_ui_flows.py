from __future__ import annotations

import time

from test_api_dry_run import client as client
from test_setup_pages import Inputs, complete_g3, post, runtime_form

from papersteward.config import Settings
from papersteward.core.entities import Paper, Run, utc_now
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.errors import InvalidRequestError
from papersteward.jobs.ui_flow import UIFlowManager
from papersteward.pipeline.process import ensure_topic_buckets
from papersteward.storage import session_scope
from papersteward.storage.repositories import (
    PaperRepository,
    ReviewRepository,
    RunRepository,
    TaxonomyRepository,
)


def ready(client):
    complete_g3(client)
    preview = post(
        client,
        "/ui/setup/dry-run/create",
        {**runtime_form(), "external_model_data": "metadata_abstract"},
    )
    accepted = post(
        client,
        "/ui/setup/dry-run/feedback",
        Inputs(preview.text, "/ui/setup/dry-run/feedback").values,
    )
    confirmation = Inputs(accepted.text, "/ui/setup/dry-run/confirm").values
    confirmation["confirmed"] = "yes"
    assert post(client, "/ui/setup/dry-run/confirm", confirmation).status_code == 200
    manager = client.app.state.ui_flows
    manager.settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_model="fixture",
        llm_base_url="http://127.0.0.1:9999/v1",
    )
    client.app.state.settings = manager.settings
    return manager


def wait(manager, identity):
    for _ in range(300):
        with session_scope(manager.factory) as session:
            report = RunRepository(session).get(identity)
        if report is not None and report.status is not RunStatus.RUNNING:
            return report
        time.sleep(0.01)
    raise AssertionError("UI run did not finish")


def request(identity, kind="sync"):
    return {
        "run_id": identity,
        "kind": kind,
        "source": "arxiv",
        "query": "fixture",
        "limit": "1",
        "allow_external": "yes",
    }


def test_ui_run_progress_idempotency_and_no_implicit_network(client, monkeypatch):
    assert post(client, "/ui/runs", request("before-g4")).status_code == 409
    manager = ready(client)
    calls = []
    monkeypatch.setattr(manager, "collect", lambda spec: calls.append("collect"))
    monkeypatch.setattr(manager, "process", lambda spec: calls.append("process"))
    assert post(client, "/ui/runs", {**request("denied"), "allow_external": ""}).status_code == 400
    response = post(client, "/ui/runs", request("ui-once"))
    assert response.status_code == 200, response.text
    assert wait(manager, "ui-once").status is RunStatus.SUCCEEDED
    assert post(client, "/ui/runs", request("ui-once")).status_code == 200
    assert calls == ["collect", "process"]
    assert post(client, "/ui/runs", {**request("ui-once"), "query": "changed"}).status_code == 409
    terminal = client.get("/ui/runs/ui-once?fragment=true")
    assert 'hx-trigger="every' not in terminal.text
    assert "已完成" in terminal.text
    assert "同步 / 处理" in client.get("/ui/today").text


def test_failed_step_preserves_committed_data_and_requires_explicit_retry(client, monkeypatch):
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
        manager, "process", lambda spec: (_ for _ in ()).throw(InvalidRequestError("model failure"))
    )
    assert post(client, "/ui/runs", request("ui-failed")).status_code == 200
    failed = wait(manager, "ui-failed")
    assert failed.status is RunStatus.FAILED and failed.stats_json["completed"] == ["collecting"]
    with session_scope(manager.factory) as session:
        assert PaperRepository(session).get("collected") is not None
    retry_page = client.get("/ui/runs/ui-failed")
    assert "显式重试" in retry_page.text
    monkeypatch.setattr(manager, "process", lambda spec: None)
    assert post(client, "/ui/runs", request("ui-retry")).status_code == 200
    assert wait(manager, "ui-retry").status is RunStatus.SUCCEEDED
    with session_scope(manager.factory) as session:
        assert RunRepository(session).get("ui-failed").status is RunStatus.FAILED


def test_restart_marks_only_unfinished_ui_runs_failed(client):
    manager = ready(client)
    with session_scope(manager.factory) as session:
        RunRepository(session).add(
            Run(
                "interrupted",
                "ui.sync",
                started_at=utc_now(),
                stats_json={"completed": ["collecting"]},
            )
        )
        RunRepository(session).add(Run("cli-unrelated", "ingest", started_at=utc_now()))
    restarted = UIFlowManager(manager.factory, manager.settings)
    restarted.close()
    with session_scope(manager.factory) as session:
        run = RunRepository(session).get("interrupted")
        assert run.status is RunStatus.FAILED and run.stats_json["completed"] == ["collecting"]
        assert "interrupted" in str(run.errors_json)
        assert RunRepository(session).get("cli-unrelated").status is RunStatus.RUNNING


def test_digest_button_and_review_forms_use_existing_services(client):
    manager = ready(client)
    from sqlalchemy import func, select
    from test_custom_views import _seed

    from papersteward.storage.models import DigestRow

    assert post(client, "/ui/runs", request("ui-empty", "digest")).status_code == 200
    assert wait(manager, "ui-empty").status is RunStatus.FAILED
    with session_scope(manager.factory) as session:
        assert session.scalar(select(func.count()).select_from(DigestRow)) == 0
        _seed(session, "digest-paper", relevance=70, authority=50, extra=50)
    response = post(client, "/ui/runs", request("ui-digest", "digest"))
    assert response.status_code == 200
    assert wait(manager, "ui-digest").status is RunStatus.SUCCEEDED
    assert "没有待审核项" in client.get("/ui/review").text
    with session_scope(manager.factory) as session:
        taxonomy = TaxonomyRepository(session).get("onboarding-taxonomy", "1.0")
        ensure_topic_buckets(session, taxonomy)
        for identity in ("approve-me", "relabel-me"):
            PaperRepository(session).add(
                Paper(
                    identity,
                    "<script>review</script>",
                    status=PaperStatus.NEEDS_REVIEW,
                    first_seen_at=utc_now(),
                )
            )
            ReviewRepository(session).open(
                identity,
                ["classification:low_primary_confidence"],
                {
                    "taxonomy_id": taxonomy.id,
                    "taxonomy_version": taxonomy.version,
                    "classification": {"primary_tag": "T01"},
                },
                now=utc_now(),
            )
    listing = client.get("/ui/review")
    assert "&lt;script&gt;review" in listing.text
    assert (
        post(
            client, "/ui/review/approve", {"paper_id": "approve-me", "reviewed_by": "fixture-user"}
        ).status_code
        == 200
    )
    assert (
        post(
            client,
            "/ui/review/relabel",
            {"paper_id": "relabel-me", "reviewed_by": "fixture-user", "primary_tag": "T02"},
        ).status_code
        == 200
    )
    with session_scope(manager.factory) as session:
        assert PaperRepository(session).get("relabel-me").status is PaperStatus.CLASSIFIED


def test_sync_uses_fake_llm_pipeline_and_skips_calibration_metadata(client, monkeypatch):
    from dataclasses import replace

    from test_process_review import PAPER, _gateway

    manager = ready(client)
    with session_scope(manager.factory) as session:
        PaperRepository(session).add(replace(PAPER, id="flow-paper", first_seen_at=utc_now()))
    monkeypatch.setattr(manager, "collect", lambda spec: None)
    monkeypatch.setattr(
        "papersteward.jobs.ui_flow.build_llm_provider", lambda *a, **kw: _gateway().provider
    )
    assert post(client, "/ui/runs", request("ui-real-pipeline")).status_code == 200
    report = wait(manager, "ui-real-pipeline")
    assert report.status is RunStatus.SUCCEEDED, report.errors_json
    assert report.stats_json["processed"] == 1
    with session_scope(manager.factory) as session:
        assert PaperRepository(session).get("flow-paper").status is not PaperStatus.SCREENED
