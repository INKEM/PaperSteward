from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.enums import DocumentStatus
from papersteward.core.errors import InvariantViolation
from papersteward.documents.profile import parse_profile
from papersteward.sources.queries import generate_queries, selected_profile
from papersteward.storage.repositories import ProfileRepository


def test_selected_profile_uses_latest_confirmed_document(db_session):
    with pytest.raises(InvariantViolation):
        selected_profile(db_session)
    base = parse_profile(
        (Path(__file__).resolve().parents[1] / "fixtures/profiles/basic.md").read_text()
    )
    older = replace(
        base,
        status=DocumentStatus.CONFIRMED,
        confirmed_at=datetime(2026, 10, 21, tzinfo=UTC),
    )
    newer = replace(
        base,
        id="newer-profile",
        status=DocumentStatus.CONFIRMED,
        confirmed_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    profiles = ProfileRepository(db_session)
    profiles.add(older)
    profiles.add(newer)
    assert selected_profile(db_session).id == "newer-profile"
    assert generate_queries(selected_profile(db_session))
