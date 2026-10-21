import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine
from test_api_onboarding_taxonomy import NOW, approval, g1, payload
from test_migrations import _downgrade, _upgrade

from papersteward.api import create_app
from papersteward.core.entities import Event, Paper
from papersteward.core.enums import EventType, PaperStatus
from papersteward.core.errors import InvariantViolation
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import (
    CalibrationRoundRow,
    EventRow,
    PaperRow,
    PaperTagRow,
    RatingRow,
    ResearchProfileRow,
    ScoreSnapshotRow,
    TaxonomyRow,
)
from papersteward.storage.onboarding import ensure_active, get_onboarding
from papersteward.storage.repositories import EventRepository, PaperRepository

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/calibration/basic.json"
ROOT = "/onboarding/calibration"


@pytest.fixture
def client(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("papersteward.api.onboarding.utc_now", lambda: NOW)
    with TestClient(create_app(engine=migrated_engine)) as client:
        yield client


def setup_g2(client):
    g1(client)
    review = client.post("/onboarding/taxonomy/drafts", json=payload()).json()
    result = client.post(
        "/onboarding/taxonomy/onboarding-taxonomy/1.0/confirm", json=approval(review)
    )
    assert result.status_code == 200, result.text


def body():
    data = json.loads(FIXTURE.read_text())
    return {key: data[key] for key in ("round_number", "sample_kind", "rubric_version", "papers")}


def labels(side):
    return json.loads(FIXTURE.read_text())[f"{side}_batch"]


def create(client, number=1):
    result = client.post(f"{ROOT}/rounds", json={**body(), "round_number": number})
    assert result.status_code == 201, result.text
    return result.json()


def ready(client, number=1, *, inverse=False):
    create(client, number)
    agent = labels("agent")
    if inverse:
        for i, label in enumerate(agent["labels"]):
            label["relevance"] = i * 5
    result = client.post(f"{ROOT}/{number}/agent-labels", json=agent)
    assert result.status_code == 200, result.text
    result = client.post(f"{ROOT}/{number}/user-labels", json=labels("user"))
    assert result.status_code == 200, result.text
    return result.json()


def confirm(view, **changes):
    return dict(
        confirmed=True,
        confirmed_by="fixture-user",
        change_summary="审阅校准报告",
        expected_revision=3,
        report_hash=view["report_hash"],
        **changes,
    )


def test_seed_import_is_idempotent_and_preserves_production_data(client, migrated_engine):
    setup_g2(client)
    first = client.post("/onboarding/seeds/import")
    assert first.status_code == 200, first.text
    assert first.json() == {"created": 20, "existing": 0, "total": 20, "sample_kind": "synthetic"}
    assert client.post("/onboarding/seeds/import").json()["created"] == 0
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert PaperRepository(session).count() == 20
        assert all(p.status is PaperStatus.DISCOVERED for p in PaperRepository(session).list())
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(EventRow.event_type == EventType.ONBOARDING_SEEDS_IMPORTED.value)
            )
            == 1
        )
        for table in (PaperTagRow, RatingRow, ScoreSnapshotRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0


def test_seed_conflict_rolls_back_earlier_inserts(client, migrated_engine):
    setup_g2(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        PaperRepository(session).add(Paper(id="syn-T05-4", title="保留既有文献"))
    response = client.post("/onboarding/seeds/import", headers={"X-Request-ID": "seed-conflict"})
    assert response.status_code == 409
    assert response.json()["error"]["request_id"] == "seed-conflict"
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert PaperRepository(session).count() == 1
        assert not session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_SEEDS_IMPORTED.value)
        )


@pytest.mark.parametrize("first", ["agent", "user"])
def test_blind_batches_report_and_g3_leave_g4_closed(client, migrated_engine, first):
    setup_g2(client)
    view = create(client)
    assert view["report"] is None and view["report_hash"] is None
    assert all(set(p) == {"paper_id", "title", "abstract"} for p in view["papers"])
    assert (
        client.post(f"{ROOT}/1/confirm", json=confirm({"report_hash": "0" * 64})).status_code == 409
    )
    result = client.post(f"{ROOT}/1/{first}-labels", json=labels(first))
    assert result.status_code == 200, result.text
    view = result.json()
    assert view[f"{first}_submitted"] and view["report"] is None
    assert client.get(f"{ROOT}/1").json() == view
    assert client.post(f"{ROOT}/1/{first}-labels", json=labels(first)).status_code == 409
    second = "user" if first == "agent" else "agent"
    view = client.post(f"{ROOT}/1/{second}-labels", json=labels(second)).json()
    assert view["report"]["metrics_passed"] and view["report"]["top5_overlap"] == 5
    assert view["report"]["sample_notice"] == "仅流程验证"
    assert len(view["report"]["annotations"]) == 20
    stale = confirm(view)
    stale["report_hash"] = "0" * 64
    assert client.post(f"{ROOT}/1/confirm", json=stale).status_code == 409
    stale = confirm(view)
    stale["expected_revision"] = 2
    assert client.post(f"{ROOT}/1/confirm", json=stale).status_code == 409
    response = client.post(f"{ROOT}/1/confirm", json=confirm(view))
    assert response.status_code == 200, response.text
    assert response.json() == {"stage": "dry_run", "revision": 4, "next_gate": "g4"}
    assert client.get(f"{ROOT}/1").json() == view
    assert client.post(f"{ROOT}/1/confirm", json=confirm(view)).status_code == 409
    assert client.post("/onboarding/seeds/import").status_code == 409
    with session_scope(create_session_factory(migrated_engine)) as session:
        event = session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_G3_CONFIRMED.value)
        )
        evidence = event.payload_json["evidence"]
        assert (
            evidence["report"] == view["report"] and evidence["report_hash"] == view["report_hash"]
        )
        assert evidence["report"]["binding"]["profile"]["id"] == "demo-profile"
        assert evidence["accepted_deviations"] is False
        for action in ("queue", "push", "archive"):
            with pytest.raises(InvariantViolation, match="G4"):
                ensure_active(session, action=action)
        for table in (PaperTagRow, RatingRow, ScoreSnapshotRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0


def test_low_metrics_need_explicit_written_acceptance(client, migrated_engine):
    setup_g2(client)
    view = ready(client, inverse=True)
    assert not view["report"]["metrics_passed"]
    assert client.post(f"{ROOT}/1/confirm", json=confirm(view)).status_code == 400
    assert (
        client.post(f"{ROOT}/1/confirm", json=confirm(view, accept_deviations=True)).status_code
        == 400
    )
    assert (
        client.post(f"{ROOT}/1/confirm", json=confirm(view, known_biases="仍未接受")).status_code
        == 400
    )
    result = client.post(
        f"{ROOT}/1/confirm",
        json=confirm(view, accept_deviations=True, known_biases="合成排序反向，仅接受流程验证"),
    )
    assert result.status_code == 200, result.text
    with session_scope(create_session_factory(migrated_engine)) as session:
        event = session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_G3_CONFIRMED.value)
        )
        assert event.payload_json["evidence"]["accepted_deviations"] is True
        assert event.payload_json["evidence"]["known_biases"] == "合成排序反向，仅接受流程验证"


def test_two_round_limit_and_latest_only_confirmation(client):
    setup_g2(client)
    create(client)
    assert client.post(f"{ROOT}/rounds", json={**body(), "round_number": 2}).status_code == 409
    client.post(f"{ROOT}/1/agent-labels", json=labels("agent"))
    view = client.post(f"{ROOT}/1/user-labels", json=labels("user")).json()
    create(client, 2)
    assert client.post(f"{ROOT}/1/confirm", json=confirm(view)).status_code == 409
    assert client.post(f"{ROOT}/rounds", json=body()).status_code == 409
    assert client.post(f"{ROOT}/rounds", json={**body(), "round_number": 3}).status_code == 422
    assert client.post(f"{ROOT}/2/agent-labels", json=labels("agent")).status_code == 200
    view = client.post(f"{ROOT}/2/user-labels", json=labels("user")).json()
    assert client.post(f"{ROOT}/2/confirm", json=confirm(view)).status_code == 200


@pytest.mark.parametrize("kind", ["seed", "boundary", "coverage", "negative", "duplicate"])
def test_bad_cohorts_cannot_be_overridden(client, migrated_engine, kind):
    setup_g2(client)
    request = body()
    if kind == "seed":
        request["papers"][0]["paper_id"] = "syn-T01-1"
    if kind == "boundary":
        request["papers"][12]["boundary_tags"] = []
    if kind == "coverage":
        request["papers"][0]["reference_tag"] = "T99"
    if kind == "negative":
        request["papers"][10]["preference_negative"] = False
    if kind == "duplicate":
        request["papers"][0]["paper_id"] = request["papers"][1]["paper_id"]
    assert client.post(f"{ROOT}/rounds", json=request).status_code == 400
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert session.get(CalibrationRoundRow, 1) is None
        assert PaperRepository(session).count() == 0


@pytest.mark.parametrize(
    "kind,status",
    [
        ("missing", 400),
        ("duplicate", 400),
        ("unknown", 400),
        ("tag", 400),
        ("score", 422),
        ("role", 422),
    ],
)
def test_bad_labels_are_atomic(client, migrated_engine, kind, status):
    setup_g2(client)
    create(client)
    request = labels("user")
    if kind == "missing":
        request["labels"].pop()
    if kind == "duplicate":
        request["labels"][0]["paper_id"] = request["labels"][1]["paper_id"]
    if kind == "unknown":
        request["labels"][0]["paper_id"] = "unknown"
    if kind == "tag":
        request["labels"][0]["primary_tag"] = "T99"
    if kind == "score":
        request["labels"][0]["relevance"] = 4
    if kind == "role":
        request["source"] = "agent"
    assert client.post(f"{ROOT}/1/user-labels", json=request).status_code == status
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert session.get(CalibrationRoundRow, 1).user_json is None


@pytest.mark.parametrize("kind", ["paper", "profile", "taxonomy", "report"])
def test_drift_blocks_g3_and_preserves_state(client, migrated_engine, kind):
    setup_g2(client)
    view = ready(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        if kind == "paper":
            session.get(PaperRow, "cal-01").title = "drift"
        if kind == "profile":
            session.get(ResearchProfileRow, ("demo-profile", "1.0")).content_md += "\nDrift"
        if kind == "taxonomy":
            session.get(TaxonomyRow, ("onboarding-taxonomy", "1.0")).content_md += "\nDrift"
        if kind == "report":
            session.get(CalibrationRoundRow, 1).report_json = {"metrics_passed": True}
    assert client.post(f"{ROOT}/1/confirm", json=confirm(view)).status_code == 409
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert get_onboarding(session).revision == 3
        assert not session.scalar(
            select(EventRow).where(EventRow.event_type == EventType.ONBOARDING_G3_CONFIRMED.value)
        )


@pytest.mark.parametrize("kind", ["label_event", "report_event", "backdated", "g3_event"])
def test_audit_failure_rolls_back_batch_report_or_gate(client, migrated_engine, monkeypatch, kind):
    setup_g2(client)
    create(client)
    assert client.post(f"{ROOT}/1/agent-labels", json=labels("agent")).status_code == 200
    factory = create_session_factory(migrated_engine)
    if kind in ("label_event", "report_event"):
        identifier = (
            "evt-calibration-1-user" if kind == "label_event" else "evt-calibration-1-report"
        )
        with session_scope(factory) as session:
            EventRepository(session).append(
                Event(id=identifier, event_type=EventType.ONBOARDING_STARTED, created_at=NOW)
            )
    if kind == "backdated":
        monkeypatch.setattr(
            "papersteward.api.onboarding.utc_now", lambda: NOW - timedelta(seconds=1)
        )
    if kind == "g3_event":
        view = client.post(f"{ROOT}/1/user-labels", json=labels("user")).json()
        monkeypatch.setattr(
            "papersteward.api.onboarding.uuid4", lambda: type("ID", (), {"hex": "collision"})()
        )
        with session_scope(factory) as session:
            EventRepository(session).append(
                Event(
                    id="evt-onboarding-collision",
                    event_type=EventType.ONBOARDING_STARTED,
                    created_at=NOW,
                )
            )
        response = client.post(f"{ROOT}/1/confirm", json=confirm(view))
    else:
        response = client.post(f"{ROOT}/1/user-labels", json=labels("user"))
    assert response.status_code == 409, response.text
    with session_scope(factory) as session:
        row = session.get(CalibrationRoundRow, 1)
        assert get_onboarding(session).revision == 3
        if kind != "g3_event":
            assert row.user_json is None and row.report_json is None
            assert row.agent_json is not None


def test_outer_transaction_rollback_and_migration_preserve_papers(client, migrated_engine):
    from papersteward.onboarding.calibration_service import import_seeds

    setup_g2(client)
    factory = create_session_factory(migrated_engine)
    with factory() as session:
        import_seeds(session, occurred_at=NOW)
        session.rollback()
    with session_scope(factory) as session:
        assert PaperRepository(session).count() == 0
    ready(client)
    _downgrade(migrated_engine, "0003")
    assert "onboarding_calibration" not in inspect(migrated_engine).get_table_names()
    with session_scope(factory) as session:
        assert PaperRepository(session).count() == 20
        assert session.get(EventRow, "evt-calibration-1-report") is not None
    _upgrade(migrated_engine)
    with session_scope(factory) as session:
        assert session.get(CalibrationRoundRow, 1) is None


def test_wrong_phase_and_not_found(client):
    assert client.post("/onboarding/seeds/import").status_code == 409
    assert client.post(f"{ROOT}/rounds", json=body()).status_code == 409
    setup_g2(client)
    assert client.get(f"{ROOT}/1").status_code == 404
    assert client.post(f"{ROOT}/rounds", json={**body(), "round_number": 2}).status_code == 409


def test_seed_import_preserves_existing_fields_and_synthetic_binding(client, migrated_engine):
    setup_g2(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        PaperRepository(session).add(
            Paper(id="syn-T01-1", title="合成 1/1", abstract="已有摘要", authors=("Author",))
        )
        session.get(PaperRow, "syn-T01-1").status = "queued"
    result = client.post("/onboarding/seeds/import")
    assert result.status_code == 200, result.text
    assert result.json()["existing"] == 1
    with session_scope(create_session_factory(migrated_engine)) as session:
        paper = PaperRepository(session).get("syn-T01-1")
        assert paper.abstract == "已有摘要" and paper.authors == ("Author",)
        assert paper.status is PaperStatus.QUEUED
    request = body()
    request["sample_kind"] = "user"
    assert client.post(f"{ROOT}/rounds", json=request).status_code == 201
    client.post(f"{ROOT}/1/user-labels", json=labels("user"))
    view = client.post(f"{ROOT}/1/agent-labels", json=labels("agent")).json()
    assert view["report"]["sample_kind"] == "synthetic"
    assert view["report"]["composition"] == {"relevant": 12, "boundary": 4, "irrelevant": 4}


@pytest.mark.parametrize("kind", ["number", "sample_kind", "report", "foreign_key"])
def test_calibration_database_constraints(client, migrated_engine, kind):
    from sqlalchemy.exc import IntegrityError

    setup_g2(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        g2_id = session.scalar(
            select(EventRow.id).where(
                EventRow.event_type == EventType.ONBOARDING_G2_CONFIRMED.value
            )
        )
    data = {
        "round_number": 1,
        "g2_event_id": g2_id,
        "binding_json": {},
        "cohort_json": [],
        "rubric_version": "test/v1",
        "sample_kind": "synthetic",
        "created_at": NOW,
    }
    if kind == "number":
        data["round_number"] = 3
    if kind == "sample_kind":
        data["sample_kind"] = "unknown"
    if kind == "report":
        data["report_json"] = {"metrics_passed": True}
    if kind == "foreign_key":
        data["g2_event_id"] = "missing-event"
    with (
        pytest.raises(IntegrityError),
        session_scope(create_session_factory(migrated_engine)) as session,
    ):
        session.add(CalibrationRoundRow(**data))


def test_calibration_snapshot_conflict_rolls_back_even_if_caller_catches(client, migrated_engine):
    from papersteward.errors import ConflictError
    from papersteward.onboarding.calibration import CalibrationPaper
    from papersteward.onboarding.calibration_service import create_round

    setup_g2(client)
    request = body()
    request["papers"][-1]["abstract"] = None
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(
            Paper(id="cal-20", title=request["papers"][-1]["title"], abstract="既有摘要不丢失")
        )
    with session_scope(factory) as session, pytest.raises(ConflictError):
        create_round(
            session,
            1,
            [CalibrationPaper.model_validate(p) for p in request["papers"]],
            sample_kind="synthetic",
            rubric_version="fixture/v1",
            occurred_at=NOW,
        )
    with session_scope(factory) as session:
        assert PaperRepository(session).count() == 1
        assert session.get(CalibrationRoundRow, 1) is None
        assert session.get(EventRow, "evt-calibration-1-created") is None
