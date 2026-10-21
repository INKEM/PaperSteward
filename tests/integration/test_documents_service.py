from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from papersteward.core.entities import Tag, Taxonomy
from papersteward.core.enums import DocumentStatus, EventType, TagLevel
from papersteward.core.errors import DomainValidationError, InvalidTransition, InvariantViolation
from papersteward.documents import (
    DocumentError,
    archive_profile,
    archive_taxonomy,
    confirm_profile,
    confirm_taxonomy,
    import_profile,
    import_taxonomy,
)
from papersteward.storage import (
    EventRepository,
    ProfileRepository,
    TaxonomyRepository,
    create_session_factory,
    session_scope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE_V1 = FIXTURES / "profiles" / "basic.md"
PROFILE_V2 = FIXTURES / "profiles" / "v2.md"
PROFILE_INVALID = FIXTURES / "profiles" / "invalid.md"
TAXONOMY_V1 = FIXTURES / "taxonomies" / "basic.md"
TAXONOMY_V2 = FIXTURES / "taxonomies" / "v2.md"

T1 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
T3 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def test_import_profile_is_draft_and_does_not_commit(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    session = factory()
    try:
        profile = import_profile(session, PROFILE_V1)
        assert profile.status is DocumentStatus.DRAFT
        session.rollback()
    finally:
        session.close()

    with session_scope(factory) as session:
        assert ProfileRepository(session).get("demo-profile", "1.0") is None
        imported = import_profile(session, PROFILE_V1)
        assert imported.id == "demo-profile"
        assert imported.version == "1.0"


def test_confirm_profile_archives_previous_and_writes_events(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        profiles = ProfileRepository(session)
        events = EventRepository(session)
        import_profile(session, PROFILE_V1)
        confirmed = confirm_profile(session, "demo-profile", "1.0", confirmed_at=T1)
        assert confirmed.status is DocumentStatus.CONFIRMED
        assert confirmed.confirmed_at == T1

        import_profile(session, PROFILE_V2)
        latest = confirm_profile(session, "demo-profile", "1.1", confirmed_at=T2)
        assert latest.status is DocumentStatus.CONFIRMED
        assert latest.confirmed_at == T2

        archived = profiles.get("demo-profile", "1.0")
        assert archived is not None
        assert archived.status is DocumentStatus.ARCHIVED
        assert archived.confirmed_at == T1
        assert profiles.latest_confirmed("demo-profile") == latest

        event_types = [event.event_type for event in events.recent(limit=10)]
        assert event_types == [
            EventType.PROFILE_CONFIRMED,
            EventType.PROFILE_ARCHIVED,
            EventType.PROFILE_CONFIRMED,
        ]
        assert events.recent(limit=1)[0].payload_json == {
            "document_id": "demo-profile",
            "version": "1.1",
            "status": "confirmed",
        }


def test_confirm_profile_rejects_missing_and_double_confirm(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        with pytest.raises(DomainValidationError, match="不存在"):
            confirm_profile(session, "missing", "1.0", confirmed_at=T1)

        import_profile(session, PROFILE_V1)
        confirm_profile(session, "demo-profile", "1.0", confirmed_at=T1)
        with pytest.raises(InvalidTransition):
            confirm_profile(session, "demo-profile", "1.0", confirmed_at=T2)


def test_archive_profile_requires_confirmed_version(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        import_profile(session, PROFILE_V1)
        with pytest.raises(InvalidTransition):
            archive_profile(session, "demo-profile", "1.0", archived_at=T1)

        confirm_profile(session, "demo-profile", "1.0", confirmed_at=T1)
        archived = archive_profile(session, "demo-profile", "1.0", archived_at=T3)
        assert archived.status is DocumentStatus.ARCHIVED
        assert EventRepository(session).recent(limit=1)[0].event_type is EventType.PROFILE_ARCHIVED

        with pytest.raises(InvalidTransition):
            archive_profile(session, "demo-profile", "1.0", archived_at=T3)


def test_invalid_profile_is_not_written(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        with pytest.raises(DocumentError, match="core_questions"):
            import_profile(session, PROFILE_INVALID)
        assert ProfileRepository(session).list() == []


def test_taxonomy_confirm_and_archive_flow(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        taxonomies = TaxonomyRepository(session)
        events = EventRepository(session)
        import_taxonomy(session, TAXONOMY_V1)
        confirm_taxonomy(session, "demo-taxonomy", "1.0", confirmed_at=T1)

        import_taxonomy(session, TAXONOMY_V2)
        latest = confirm_taxonomy(session, "demo-taxonomy", "1.1", confirmed_at=T2)
        assert latest.status is DocumentStatus.CONFIRMED
        assert len(latest.tags) == 8

        archived = taxonomies.get("demo-taxonomy", "1.0")
        assert archived is not None
        assert archived.status is DocumentStatus.ARCHIVED
        assert len(archived.tags) == 7
        assert taxonomies.latest_confirmed("demo-taxonomy") == latest

        event_types = [event.event_type for event in events.recent(limit=10)]
        assert event_types == [
            EventType.TAXONOMY_CONFIRMED,
            EventType.TAXONOMY_ARCHIVED,
            EventType.TAXONOMY_CONFIRMED,
        ]

        archived_manual = archive_taxonomy(session, "demo-taxonomy", "1.1", archived_at=T3)
        assert archived_manual.status is DocumentStatus.ARCHIVED
        assert events.recent(limit=1)[0].event_type is EventType.TAXONOMY_ARCHIVED


def test_document_service_missing_document_errors(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        with pytest.raises(DomainValidationError, match="不存在"):
            confirm_taxonomy(session, "missing", "1.0", confirmed_at=T1)
        with pytest.raises(DomainValidationError, match="不存在"):
            archive_profile(session, "missing", "1.0", archived_at=T1)
        with pytest.raises(DomainValidationError, match="不存在"):
            archive_taxonomy(session, "missing", "1.0", archived_at=T1)


@pytest.mark.parametrize("count", [4, 10])
def test_confirm_taxonomy_revalidates_primary_tag_count(
    migrated_engine: Engine, count: int
) -> None:
    factory = create_session_factory(migrated_engine)
    tags = tuple(
        Tag(code=f"T{i:02d}", name=f"主题 {i}", level=TagLevel.PRIMARY) for i in range(1, count + 1)
    )
    taxonomy = Taxonomy(
        id="tax-shape",
        version="1.0",
        status=DocumentStatus.DRAFT,
        content_md="# taxonomy",
        tags=tags,
    )

    with session_scope(factory) as session:
        TaxonomyRepository(session).add(taxonomy)
        with pytest.raises(InvariantViolation, match="5–9"):
            confirm_taxonomy(session, "tax-shape", "1.0", confirmed_at=T1)

        stored = TaxonomyRepository(session).get("tax-shape", "1.0")
        assert stored is not None
        assert stored.status is DocumentStatus.DRAFT
        assert EventRepository(session).recent(limit=10) == []
