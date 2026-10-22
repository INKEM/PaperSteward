from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_api_dry_run import client as client
from test_digest_delivery import prepared
from test_pipeline_ingest import activate
from test_priority_queue import DAY, NOW, seed

from papersteward.cli.main import main
from papersteward.config import Settings, get_settings
from papersteward.core.enums import EventType, PaperStatus
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.actions import apply_action
from papersteward.delivery.contracts import Action
from papersteward.delivery.service import create_digest, deliver
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import EventRow, RatingRow
from papersteward.storage.repositories import EventRepository, PaperRepository, RatingRepository


def test_actions_api_gate_done_idempotency_and_revisions(client, migrated_engine, monkeypatch):
    body = {
        "kind": "done",
        "mode": "deep",
        "score": 4,
        "note": "备注不外发",
        "request_id": "done-1",
    }
    assert client.post("/papers/p/actions", json=body).status_code == 409
    factory = prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW)
    first = client.post("/papers/p/actions", json=body)
    assert first.status_code == 200 and first.json()["status"] == "archived"
    assert client.post("/papers/p/actions", json=body).json() == first.json()
    assert client.post("/papers/p/actions", json={**body, "score": 5}).status_code == 409
    assert client.post("/papers/p/actions", json={**body, "score": True}).status_code == 422
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW + timedelta(seconds=1))
    assert (
        client.post(
            "/papers/p/actions", json={"kind": "score", "score": 5, "request_id": "rate-1"}
        ).status_code
        == 200
    )
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW + timedelta(seconds=2))
    assert (
        client.post(
            "/papers/p/actions", json={"kind": "note", "note": "新备注", "request_id": "note-1"}
        ).status_code
        == 200
    )
    with session_scope(factory) as session:
        rows = RatingRepository(session).list_for_paper("p")
        assert [(r.score, r.note) for r in rows] == [
            (4, "备注不外发"),
            (5, "备注不外发"),
            (5, "新备注"),
        ]
        assert rows[0].tags_at_read == ("T01",)
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(EventRow.event_type == "paper.done")
            )
            == 1
        )


@pytest.mark.parametrize("kind,days", [("skip", 7), ("snooze", 3)])
def test_cooldown_due_day_and_reject(client, migrated_engine, kind, days):
    factory = prepared(client, migrated_engine)
    request = Action(request_id=kind, kind=kind, **({"days": days} if kind == "snooze" else {}))
    with session_scope(factory) as session:
        apply_action(session, "p", request, now=NOW)
        tomorrow = create_digest(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        assert not tomorrow.items
        due = create_digest(session, DAY + timedelta(days=days), now=NOW + timedelta(days=days))
        assert [i.paper_id for i in due.items] == ["p"]
        apply_action(
            session,
            "p",
            Action(request_id="reject", kind="reject", note="无关原因"),
            now=NOW + timedelta(days=days),
        )
        assert PaperRepository(session).get("p").status is PaperStatus.REJECTED
        assert session.scalar(select(func.count()).select_from(RatingRow)) == 0


def test_actions_atomic_failure_and_outer_rollback(client, migrated_engine, monkeypatch):
    factory = prepared(client, migrated_engine)
    original = EventRepository.append

    def broken(self, event):
        if event.event_type is EventType.ACTION_RECORDED:
            raise RuntimeError("receipt failure")
        return original(self, event)

    request = Action(request_id="done", kind="done", mode="skim")
    with session_scope(factory) as session:
        with monkeypatch.context() as m:
            m.setattr(EventRepository, "append", broken)
            with pytest.raises(RuntimeError):
                apply_action(session, "p", request, now=NOW)
        assert PaperRepository(session).get("p").status is PaperStatus.QUEUED
        assert not RatingRepository(session).list_for_paper("p")
    with factory() as session:
        apply_action(session, "p", request, now=NOW)
        session.rollback()
    with session_scope(factory) as session:
        assert PaperRepository(session).get("p").status is PaperStatus.QUEUED


def test_skipped_draft_cannot_be_delivered(client, migrated_engine, tmp_path):
    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        apply_action(session, "p", Action(request_id="skip", kind="skip"), now=NOW)
    with pytest.raises(InvariantViolation, match="冷却"):
        deliver(factory, DAY, now=NOW, settings=Settings(_env_file=None), out=tmp_path / "no.md")
    assert not (tmp_path / "no.md").exists()


def test_delivered_receipt_is_idempotent_after_done(client, migrated_engine, tmp_path):
    factory = prepared(client, migrated_engine)
    settings = Settings(_env_file=None)
    path = tmp_path / "sent.md"
    deliver(factory, DAY, now=NOW, settings=settings, out=path)
    with session_scope(factory) as session:
        apply_action(session, "p", Action(request_id="finish", kind="done", mode="skim"), now=NOW)
    assert deliver(factory, DAY, now=NOW, settings=settings, out=path)["existing"]


def test_prequeue_cannot_be_done_and_cli_can_read(client, migrated_engine, monkeypatch, capsys):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session)
        with pytest.raises(InvariantViolation):
            apply_action(
                session, "p", Action(request_id="early", kind="done", mode="skim"), now=NOW
            )
        create_digest(session, DAY, now=NOW)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    monkeypatch.setattr("papersteward.cli.reading.utc_now", lambda: NOW)
    get_settings.cache_clear()
    try:
        assert (
            main(
                [
                    "done",
                    "p",
                    "--mode",
                    "deep",
                    "--score",
                    "4",
                    "--note",
                    "CLI",
                    "--request-id",
                    "cli",
                ]
            )
            == 0
        )
        assert '"status": "archived"' in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
