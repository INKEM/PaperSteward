import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from test_api_calibration import confirm as calibration_approval
from test_api_calibration import ready as calibration_ready
from test_api_calibration import setup_g2
from test_api_onboarding_taxonomy import NOW

from papersteward.api import create_app
from papersteward.core.entities import Event
from papersteward.core.enums import AssignedBy, EventType, PaperStatus
from papersteward.core.onboarding import GateApproval
from papersteward.onboarding.dry_run import DryRunRequest
from papersteward.onboarding.dry_run_service import create_preview
from papersteward.scoring.config import ScoringConfig
from papersteward.scoring.service import save_scoring_config
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    CalibrationRoundRow,
    ConfigRow,
    EventRow,
    PaperRow,
    ResearchProfileRow,
    RunRow,
    ScoreSnapshotRow,
    TaxonomyRow,
)
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ConfigRepository, EventRepository, PaperRepository

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/dry_run"
ROOT = "/onboarding/dry-runs"


@pytest.fixture
def client(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("papersteward.api.onboarding.utc_now", lambda: NOW)
    with TestClient(create_app(engine=migrated_engine)) as client:
        yield client


def setup_g3(client):
    setup_g2(client)
    view = calibration_ready(client)
    result = client.post("/onboarding/calibration/1/confirm", json=calibration_approval(view))
    assert result.status_code == 200, result.text


def body(version=1):
    data = json.loads((FIXTURES / "basic.json").read_text())
    data["version"] = version
    return data


def create(client, version=1):
    response = client.post(ROOT, json=body(version))
    assert response.status_code == 201, response.text
    return response.json()


def feedback(view, *, accepted=True, issues=None):
    return {
        "report_hash": view["report_hash"],
        "reviewed_by": "fixture-user",
        "accepted": accepted,
        "issues": issues or [],
    }


def accepted(client, version=1):
    view = create(client, version)
    response = client.post(f"{ROOT}/{version}/feedback", json=feedback(view))
    assert response.status_code == 200, response.text
    return response.json()


def approval(view):
    return {
        "confirmed": True,
        "confirmed_by": "fixture-user",
        "change_summary": "已审阅运行参数和试运行日报",
        "expected_revision": 4,
        "report_hash": view["report_hash"],
        "feedback_hash": view["feedback_hash"] or "0" * 64,
    }


def test_preview_snapshot_does_not_write_configs_or_business_data(client, migrated_engine):
    setup_g3(client)
    view = create(client)
    assert view["report"]["markdown"] + "\n" == (FIXTURES / "digest.md").read_text()
    assert view["report"]["display_only"] and view["feedback"] is None
    assert client.get(f"{ROOT}/1").json() == view
    assert client.post(f"{ROOT}/1/confirm", json=approval(view)).status_code == 409
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert get_onboarding(session).revision == 4
        assert PaperRepository(session).count() == 20
        assert all(p.status is PaperStatus.DISCOVERED for p in PaperRepository(session).list())
        for table in (ConfigRow, RunRow, ScoreSnapshotRow, ArchiveSnapshotRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0
        event = session.get(EventRow, "evt-dry-run-1")
        assert event.payload_json["report"] == view["report"]
        assert event.payload_json["report_hash"] == view["report_hash"]


@pytest.mark.parametrize("issue", ["too_many", "too_few", "inaccurate", "wrong_category"])
def test_negative_feedback_requires_review_and_g4_persists_selected_version(
    client, migrated_engine, issue
):
    setup_g3(client)
    old = create(client)
    rejected = client.post(
        f"{ROOT}/1/feedback", json=feedback(old, accepted=False, issues=[issue])
    ).json()
    assert client.post(f"{ROOT}/1/confirm", json=approval(rejected)).status_code == 409
    request = body(2)
    request["parameters"]["push_time"] = "09:00"
    request["items"].pop()
    new = client.post(ROOT, json=request)
    assert new.status_code == 201, new.text
    new = new.json()
    assert new["report_hash"] != old["report_hash"]
    assert client.get(f"{ROOT}/1").json() == rejected
    assert client.post(f"{ROOT}/1/confirm", json=approval(rejected)).status_code == 409
    assert client.post(f"{ROOT}/1/feedback", json=feedback(old)).status_code == 409
    new = client.post(f"{ROOT}/2/feedback", json=feedback(new)).json()
    response = client.post(
        f"{ROOT}/2/confirm", json=approval(new), headers={"X-Request-ID": "g4-success"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"stage": "active", "revision": 5, "next_gate": None}
    assert response.headers["X-Request-ID"] == "g4-success"
    assert client.post(f"{ROOT}/2/confirm", json=approval(new)).status_code == 409
    assert client.post(ROOT, json=body(3)).status_code == 409
    assert client.post(f"{ROOT}/2/feedback", json=feedback(new)).status_code == 409
    assert client.get(f"{ROOT}/2").json() == new
    with session_scope(create_session_factory(migrated_engine)) as session:
        runtime = ConfigRepository(session).get("onboarding_runtime", "2")
        assert dict(runtime.value_json) == request["parameters"]
        assert runtime.updated_by is AssignedBy.USER
        assert ConfigRepository(session).get("onboarding_runtime", "1") is None
        scoring = ConfigRepository(session).get("scoring", "1.0.0")
        assert dict(scoring.value_json) == new["report"]["scoring"]
        event = session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_G4_CONFIRMED.value)
        )
        assert event.payload_json["artifact_version"] == "2"
        assert event.payload_json["evidence"]["report"] == new["report"]
        assert event.payload_json["evidence"]["feedback"] == new["feedback"]
        assert event.payload_json["evidence"]["report"]["binding"]["calibration_round"] == 1
        assert session.scalar(select(func.count()).select_from(RunRow)) == 0


def test_feedback_is_append_only_and_old_acceptance_cannot_confirm(client):
    setup_g3(client)
    first = accepted(client)
    for field, value in [
        ("report_hash", "0" * 64),
        ("feedback_hash", "0" * 64),
        ("expected_revision", 3),
        ("confirmed", False),
    ]:
        request = approval(first)
        request[field] = value
        assert client.post(f"{ROOT}/1/confirm", json=request).status_code in (409, 422)
    rejected = client.post(
        f"{ROOT}/1/feedback", json=feedback(first, accepted=False, issues=["inaccurate"])
    ).json()
    assert len(rejected["feedback_history"]) == 2
    assert rejected["report_hash"] == first["report_hash"]
    assert rejected["feedback_hash"] != first["feedback_hash"]
    assert client.post(f"{ROOT}/1/confirm", json=approval(first)).status_code == 409
    assert client.post(f"{ROOT}/1/confirm", json=approval(rejected)).status_code == 409
    final = client.post(f"{ROOT}/1/feedback", json=feedback(first)).json()
    assert len(final["feedback_history"]) == 3
    assert client.post(f"{ROOT}/1/confirm", json=approval(final)).status_code == 200


def test_versions_and_invalid_feedback(client):
    assert client.post(ROOT, json=body()).status_code == 409
    setup_g3(client)
    assert client.get(f"{ROOT}/1").status_code == 404
    assert client.post(ROOT, json=body(2)).status_code == 409
    view = create(client)
    assert client.post(ROOT, json=body()).status_code == 409
    request = feedback(view)
    request["report_hash"] = "0" * 64
    assert client.post(f"{ROOT}/1/feedback", json=request).status_code == 409
    request = feedback(view)
    request["issues"] = ["too_many"]
    assert client.post(f"{ROOT}/1/feedback", json=request).status_code == 422
    request = body(2)
    request["passed"] = True
    assert client.post(ROOT, json=request).status_code == 422
    request = body(2)
    request["parameters"]["target_shares"] = {"T99": 1}
    assert client.post(ROOT, json=request).status_code == 400
    assert client.get(f"{ROOT}/1").json() == view


@pytest.mark.parametrize(
    "kind",
    ["profile", "taxonomy", "paper", "calibration", "g3", "report", "input", "scoring", "runtime"],
)
def test_drift_cannot_activate(client, migrated_engine, kind):
    setup_g3(client)
    view = accepted(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        if kind == "profile":
            session.get(ResearchProfileRow, ("demo-profile", "1.0")).content_md += "\nDrift"
        if kind == "taxonomy":
            session.get(TaxonomyRow, ("onboarding-taxonomy", "1.0")).content_md += "\nDrift"
        if kind == "paper":
            session.get(PaperRow, "cal-01").title = "Drift"
        if kind == "calibration":
            session.get(CalibrationRoundRow, 1).report_json = {}
        if kind == "g3":
            event = session.scalar(
                select(EventRow).where(
                    EventRow.event_type == EventType.ONBOARDING_G3_CONFIRMED.value
                )
            )
            data = json.loads(json.dumps(event.payload_json))
            data["evidence"]["report_hash"] = "0" * 64
            event.payload_json = data
        if kind in ("report", "input"):
            event = session.get(EventRow, "evt-dry-run-1")
            data = json.loads(json.dumps(event.payload_json))
            if kind == "report":
                data["report"]["markdown"] = "不同日报"
            else:
                data["input"]["parameters"]["push_time"] = "09:00"
            event.payload_json = data
        if kind == "scoring":
            config = ScoringConfig.model_validate(body()["scoring"])
            config.stability.max_daily_delta = 7
            save_scoring_config(session, config, updated_at=NOW)
        if kind == "runtime":
            from papersteward.core.entities import Config

            ConfigRepository(session).add(
                Config(
                    key="onboarding_runtime",
                    version="1",
                    value_json={"drift": True},
                    updated_at=NOW,
                )
            )
    response = client.post(f"{ROOT}/1/confirm", json=approval(view))
    assert response.status_code == 409, response.text
    with session_scope(factory) as session:
        assert get_onboarding(session).revision == 4
        assert not session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_G4_CONFIRMED.value)
        )


@pytest.mark.parametrize("step", ["preview", "feedback", "scoring", "runtime", "gate", "backdated"])
def test_audit_failure_rolls_back_entire_operation(client, migrated_engine, monkeypatch, step):
    setup_g3(client)
    factory = create_session_factory(migrated_engine)
    if step == "preview":
        identifier = "evt-dry-run-1"
    elif step == "feedback":
        identifier = "evt-dry-run-1-feedback-1"
    else:
        view = accepted(client)
        if step == "scoring":
            from papersteward.scoring.service import _event_id

            identifier = _event_id("1.0.0")
        elif step == "runtime":
            identifier = "evt-runtime-config-1"
        elif step == "gate":
            identifier = "evt-onboarding-collision"
            monkeypatch.setattr(
                "papersteward.api.onboarding.uuid4", lambda: SimpleNamespace(hex="collision")
            )
        else:
            identifier = None
            monkeypatch.setattr(
                "papersteward.api.onboarding.utc_now", lambda: NOW - timedelta(seconds=1)
            )
    if identifier:
        with session_scope(factory) as session:
            EventRepository(session).append(
                Event(id=identifier, event_type=EventType.ONBOARDING_STARTED, created_at=NOW)
            )
    if step == "preview":
        response = client.post(ROOT, json=body())
    elif step == "feedback":
        view = create(client)
        response = client.post(f"{ROOT}/1/feedback", json=feedback(view))
    else:
        response = client.post(f"{ROOT}/1/confirm", json=approval(view))
    assert response.status_code == 409, response.text
    with session_scope(factory) as session:
        assert get_onboarding(session).revision == 4
        assert session.scalar(select(func.count()).select_from(ConfigRow)) == 0
        assert not session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_G4_CONFIRMED.value)
        )
        if step == "feedback":
            assert client.get(f"{ROOT}/1").json()["feedback"] is None


def test_matching_scoring_version_is_reused_and_outer_rollback_works(client, migrated_engine):
    setup_g3(client)
    factory = create_session_factory(migrated_engine)
    with factory() as session:
        create_preview(session, DryRunRequest.model_validate(body()), occurred_at=NOW)
        session.rollback()
    with session_scope(factory) as session:
        assert session.get(EventRow, "evt-dry-run-1") is None
        save_scoring_config(
            session, ScoringConfig.model_validate(body()["scoring"]), updated_at=NOW
        )
    view = accepted(client)
    assert client.post(f"{ROOT}/1/confirm", json=approval(view)).status_code == 200
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ConfigRow)) == 2
        assert len(ConfigRepository(session).versions("scoring")) == 1


def test_gate_service_failure_is_atomic_even_if_caller_commits(client, migrated_engine):
    from sqlalchemy.exc import IntegrityError

    from papersteward.onboarding.dry_run_service import confirm_g4

    setup_g3(client)
    view = accepted(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session, pytest.raises(IntegrityError):
        confirm_g4(
            session,
            1,
            GateApproval("user", "dry-run", "1", "reviewed"),
            expected_revision=4,
            expected_report_hash=view["report_hash"],
            expected_feedback_hash=view["feedback_hash"],
            event_id="evt-dry-run-1",
            occurred_at=NOW,
        )
    with session_scope(factory) as session:
        assert get_onboarding(session).revision == 4
        assert ConfigRepository(session).latest("scoring") is None
        assert ConfigRepository(session).latest("onboarding_runtime") is None
