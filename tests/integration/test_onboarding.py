from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from papersteward.api import create_app
from papersteward.core.entities import ArchiveSnapshot, Paper
from papersteward.core.enums import PaperStatus
from papersteward.core.errors import InvalidTransition, InvariantViolation
from papersteward.core.onboarding import Gate, GateApproval, Onboarding, OnboardingStage
from papersteward.onboarding.service import confirm_gate, start
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import OnboardingRow
from papersteward.storage.onboarding import ensure_active, get_onboarding
from papersteward.storage.repositories import (
    ArchiveSnapshotRepository,
    EventRepository,
    PaperRepository,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)
APPROVAL = GateApproval("fixture-user", "synthetic-artifact", "fixture-v1", "仅验证流程")


def synthetic_validator(
    session: Session, state: Onboarding, gate: Gate, approval: GateApproval
) -> None:
    """S1 流程夹具；不声称满足真实 G1–G4 业务条件。"""
    assert approval.artifact_version == "fixture-v1"


def confirm(session: Session, gate: Gate, revision: int) -> Onboarding:
    return confirm_gate(
        session,
        gate,
        APPROVAL,
        expected_revision=revision,
        validate_evidence=synthetic_validator,
        event_id=f"fixture-{gate}",
        occurred_at=NOW,
    )


def test_ordered_flow_persists_across_sessions_with_audit(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        assert get_onboarding(session) == Onboarding()
        start(session, event_id="start", occurred_at=NOW)
    for revision, gate in enumerate(Gate, 1):
        with session_scope(factory) as session:
            assert get_onboarding(session).next_gate is gate
            confirm(session, gate, revision)
    with session_scope(factory) as session:
        ensure_active(session, action="push")
        assert get_onboarding(session) == Onboarding(OnboardingStage.ACTIVE, 5)
        assert start(session, event_id="ignored", occurred_at=NOW).revision == 5
        events = EventRepository(session).recent()
        assert len(events) == 5
        confirmations = [e for e in events if e.payload_json.get("gate")]
        assert {e.event_type.value for e in confirmations} == {
            f"onboarding.{gate}.confirmed" for gate in Gate
        }
        for event in confirmations:
            assert event.payload_json["confirmed_by"] == "fixture-user"
            assert event.payload_json["artifact_version"] == "fixture-v1"
            assert event.payload_json["change_summary"] == "仅验证流程"


def test_reject_skip_stale_duplicate_and_invalid_evidence(db_session: Session) -> None:
    start(db_session, event_id="start", occurred_at=NOW)
    assert start(db_session, event_id="ignored", occurred_at=NOW).revision == 1
    with pytest.raises(InvalidTransition):
        confirm(db_session, Gate.G2, 1)
    with pytest.raises(InvariantViolation, match="revision"):
        confirm(db_session, Gate.G1, 0)

    def reject(*_: object) -> None:
        raise InvariantViolation("onboarding.evidence", "产物版本或校验报告不合格")

    with pytest.raises(InvariantViolation, match="evidence"):
        confirm_gate(
            db_session,
            Gate.G1,
            APPROVAL,
            expected_revision=1,
            validate_evidence=reject,
            event_id="rejected",
            occurred_at=NOW,
        )
    assert get_onboarding(db_session).revision == 1
    assert len(EventRepository(db_session).recent()) == 1
    confirm(db_session, Gate.G1, 1)
    with pytest.raises(InvalidTransition):
        confirm(db_session, Gate.G1, 2)
    assert get_onboarding(db_session).revision == 2
    assert len(EventRepository(db_session).recent()) == 2


def test_failed_audit_does_not_leave_advanced_state(db_session: Session) -> None:
    start(db_session, event_id="start", occurred_at=NOW)
    with pytest.raises(InvariantViolation):
        confirm_gate(
            db_session,
            Gate.G1,
            APPROVAL,
            expected_revision=1,
            validate_evidence=synthetic_validator,
            event_id="old",
            occurred_at=NOW - timedelta(days=1),
        )
    assert get_onboarding(db_session).revision == 1
    assert len(EventRepository(db_session).recent()) == 1


@pytest.mark.parametrize("confirmations", range(5))
def test_every_pre_g4_stage_blocks_all_execution_paths(
    db_session: Session, confirmations: int
) -> None:
    if confirmations:
        start(db_session, event_id="start", occurred_at=NOW)
        for revision, gate in enumerate(list(Gate)[: confirmations - 1], 1):
            confirm(db_session, gate, revision)
    papers = PaperRepository(db_session)
    for source, target in (
        (PaperStatus.SCORED, PaperStatus.QUEUED),
        (PaperStatus.QUEUED, PaperStatus.PUSHED),
        (PaperStatus.SHELVED, PaperStatus.PUSHED),
        (PaperStatus.SNOOZED, PaperStatus.QUEUED),
        (PaperStatus.DONE, PaperStatus.ARCHIVED),
    ):
        # 直接构造既有历史行，检查迁移入口同样守门。
        from papersteward.storage.mappers import paper_to_row

        db_session.add(paper_to_row(Paper(id=f"{source}-{target}", title="Fixture", status=source)))
        db_session.flush()
        with pytest.raises(InvariantViolation, match="G4"):
            papers.transition(f"{source}-{target}", target, event_id=f"blocked-{source}")
        assert papers.get(f"{source}-{target}").status is source
        with pytest.raises(InvariantViolation, match="G4"):
            papers.add(Paper(id=f"direct-{source}", title="Fixture", status=target))
    with pytest.raises(InvariantViolation, match="G4"):
        ArchiveSnapshotRepository(db_session).add(snapshot("done-archived"))
    assert not any(e.paper_id for e in EventRepository(db_session).recent())


def snapshot(paper_id: str) -> ArchiveSnapshot:
    return ArchiveSnapshot(
        paper_id=paper_id,
        archived_at=NOW,
        primary_tag="T01",
        filing_bucket_id="b-t01",
        profile_version="1",
        taxonomy_version="1",
        config_version="1",
        content_hash="a" * 64,
    )


def test_g4_allows_execution_without_weakening_paper_transition(db_session: Session) -> None:
    start(db_session, event_id="start", occurred_at=NOW)
    for revision, gate in enumerate(Gate, 1):
        confirm(db_session, gate, revision)
    papers = PaperRepository(db_session)
    papers.add(Paper(id="paper", title="Fixture", status=PaperStatus.SCORED))
    for target in (
        PaperStatus.QUEUED,
        PaperStatus.PUSHED,
        PaperStatus.READING,
        PaperStatus.DONE,
        PaperStatus.ARCHIVED,
    ):
        papers.transition("paper", target, event_id=f"paper-{target}", occurred_at=NOW)
    ArchiveSnapshotRepository(db_session).add(snapshot("paper"))
    with pytest.raises(InvalidTransition):
        papers.transition("paper", PaperStatus.QUEUED, event_id="invalid")


def test_outer_transaction_rolls_back_state_and_events(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    with pytest.raises(RuntimeError), session_scope(factory) as session:
        start(session, event_id="start", occurred_at=NOW)
        confirm(session, Gate.G1, 1)
        raise RuntimeError("rollback")
    with session_scope(factory) as session:
        assert get_onboarding(session) == Onboarding()
        assert EventRepository(session).recent() == []


def test_start_status_api_is_read_only_until_explicit_start(migrated_engine: Engine) -> None:
    with TestClient(create_app(engine=migrated_engine)) as client:
        assert client.get("/onboarding").json() == {
            "stage": "seeds",
            "revision": 0,
            "next_gate": None,
        }
        factory = create_session_factory(migrated_engine)
        with session_scope(factory) as session:
            assert session.scalars(select(OnboardingRow)).all() == []
        response = client.post("/onboarding/start", headers={"X-Request-ID": "onboarding"})
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "onboarding"
        assert response.json() == {"stage": "profile", "revision": 1, "next_gate": "g1"}
        assert client.post("/onboarding/start").json() == response.json()
        assert client.get("/onboarding").json() == response.json()
        assert client.post("/onboarding/confirm/g4", json={"passed": True}).status_code == 404
    with session_scope(factory) as session:
        assert len(EventRepository(session).recent()) == 1


def test_0003_downgrade_preserves_p1_data(migrated_engine: Engine) -> None:
    from test_migrations import _downgrade, _upgrade

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(Paper(id="retained", title="P1 data"))
        start(session, event_id="start", occurred_at=NOW)
    _downgrade(migrated_engine, "0002")
    assert "onboarding" not in inspect(migrated_engine).get_table_names()
    _upgrade(migrated_engine)
    with session_scope(factory) as session:
        assert PaperRepository(session).get("retained") is not None
        assert get_onboarding(session) == Onboarding()
        assert EventRepository(session).get("start") is not None
