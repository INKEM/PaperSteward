from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from papersteward.storage import PaperRepository, Run, utc_now


def test_create_and_get_paper(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    created = repository.create("p001", "Retrieval-Augmented Generation")

    assert created.id == "p001"
    assert created.status == "new"
    assert created.created_at is not None

    db_session.commit()
    db_session.expire_all()
    loaded = repository.get("p001")

    assert loaded is not None
    assert loaded.title == "Retrieval-Augmented Generation"
    assert loaded.status == "new"


def test_list_orders_and_paginates(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    base = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    repository.create("p002", "second", created_at=base + timedelta(minutes=2))
    repository.create("p001", "first", created_at=base + timedelta(minutes=1))
    repository.create("p003", "third", created_at=base)
    db_session.commit()

    assert [paper.id for paper in repository.list()] == ["p003", "p001", "p002"]
    assert [paper.id for paper in repository.list(limit=2)] == ["p003", "p001"]
    assert [paper.id for paper in repository.list(limit=1, offset=1)] == ["p001"]


def test_update_status_and_missing_paper(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    repository.create("p001", "RAG")
    db_session.commit()

    updated = repository.update_status("p001", "ready")
    assert updated is not None
    assert updated.status == "ready"
    db_session.commit()
    db_session.expire_all()
    loaded = repository.get("p001")
    assert loaded is not None
    assert loaded.status == "ready"
    assert repository.update_status("missing", "ready") is None


def test_delete_and_count(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    assert repository.count() == 0
    assert repository.delete("missing") is False

    repository.create("p001", "RAG")
    repository.create("p002", "Agent")
    db_session.commit()
    assert repository.count() == 2

    assert repository.delete("p001") is True
    db_session.commit()
    db_session.expire_all()
    assert repository.get("p001") is None
    assert repository.count() == 1


def test_duplicate_id_raises_integrity_error(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    repository.create("p001", "RAG")
    db_session.commit()

    with pytest.raises(IntegrityError):
        repository.create("p001", "duplicate")
    db_session.rollback()


def test_create_does_not_commit(db_session: Session) -> None:
    repository = PaperRepository(db_session)
    repository.create("p001", "RAG")

    db_session.rollback()

    assert repository.get("p001") is None


def test_run_model_round_trip(db_session: Session) -> None:
    db_session.add(Run(id="run-001", kind="ingest", started_at=utc_now()))
    db_session.commit()
    db_session.expire_all()

    run = db_session.get(Run, "run-001")

    assert run is not None
    assert run.kind == "ingest"
    assert run.status == "running"
    assert run.finished_at is None
    assert run.error is None
