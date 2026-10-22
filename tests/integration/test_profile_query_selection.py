from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Event
from papersteward.core.enums import DocumentStatus, EventType
from papersteward.core.errors import InvariantViolation
from papersteward.documents.profile import parse_profile
from papersteward.sources.queries import generate_queries, selected_profile
from papersteward.storage.repositories import EventRepository, ProfileRepository


def test_selected_profile_uses_exact_g1_reference(db_session):
    with pytest.raises(InvariantViolation):
        selected_profile(db_session)
    chosen = replace(
        parse_profile(
            (Path(__file__).resolve().parents[1] / "fixtures/profiles/basic.md").read_text()
        ),
        status=DocumentStatus.CONFIRMED,
        confirmed_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    profiles = ProfileRepository(db_session)
    profiles.add(chosen)
    profiles.add(replace(chosen, id="unrelated"))
    EventRepository(db_session).append(
        Event(
            id="g1",
            event_type=EventType.ONBOARDING_G1_CONFIRMED,
            created_at=datetime(2026, 10, 22, tzinfo=UTC),
            payload_json={"artifact_id": chosen.id, "artifact_version": chosen.version},
        )
    )
    assert selected_profile(db_session).id == chosen.id
    assert generate_queries(selected_profile(db_session))
