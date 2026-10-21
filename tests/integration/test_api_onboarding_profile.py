from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from papersteward.api import create_app
from papersteward.core.enums import DocumentStatus, EventType
from papersteward.documents import archive_profile, confirm_profile, parse_profile
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import ResearchProfileRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import EventRepository, ProfileRepository

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"
NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)
ROOT = "/onboarding/profile"


@pytest.fixture
def client(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("papersteward.api.onboarding.utc_now", lambda: NOW)
    with TestClient(create_app(engine=migrated_engine)) as client:
        yield client


def draft(client: TestClient, name: str = "basic", **extra: object):
    return client.post(
        f"{ROOT}/drafts",
        json={
            "profile_id": "demo-profile",
            "content_md": (FIXTURES / f"{name}.md").read_text(),
            **extra,
        },
    )


def approval(reviewed_hash: str, **extra: object) -> dict[str, object]:
    return {
        "confirmed": True,
        "confirmed_by": "researcher",
        "change_summary": "已核对方向与排除标准",
        "expected_revision": 1,
        "content_hash": reviewed_hash,
        **extra,
    }


def test_draft_edit_review_and_confirm_g1_atomically(
    client: TestClient, migrated_engine: Engine
) -> None:
    client.post("/onboarding/start")
    original = draft(client)
    assert original.status_code == 201
    assert original.json()["status"] == "draft"
    assert client.get("/onboarding").json()["stage"] == "profile"
    edited = draft(client, "v2", base_version="1.0")
    assert edited.status_code == 201
    assert "core_questions.Q2.text" in edited.json()["diff"]
    assert client.get(f"{ROOT}/demo-profile/1.1?base_version=1.0").json() == edited.json()
    reviewed = client.get(f"{ROOT}/demo-profile/1.1").json()
    assert reviewed["content_md"] == (FIXTURES / "v2.md").read_text().strip()
    assert client.get(f"{ROOT}/demo-profile/1.0").json() == original.json()
    confirmed = client.post(
        f"{ROOT}/demo-profile/1.1/confirm",
        json=approval(reviewed["content_hash"]),
        headers={"X-Request-ID": "g1-confirm"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"stage": "taxonomy", "revision": 2, "next_gate": "g2"}
    assert confirmed.headers["X-Request-ID"] == "g1-confirm"
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        assert (
            ProfileRepository(session).get("demo-profile", "1.1").status is DocumentStatus.CONFIRMED
        )
        assert ProfileRepository(session).get("demo-profile", "1.0").status is DocumentStatus.DRAFT
        events = EventRepository(session).recent()
        assert {e.event_type for e in events} == {
            EventType.ONBOARDING_STARTED,
            EventType.PROFILE_CONFIRMED,
            EventType.ONBOARDING_G1_CONFIRMED,
        }
        gate = next(e for e in events if e.event_type is EventType.ONBOARDING_G1_CONFIRMED)
        assert gate.payload_json["artifact_version"] == "1.1"
        assert gate.payload_json["artifact_id"] == "demo-profile"
        assert gate.payload_json["confirmed_by"] == "researcher"
    assert (
        client.post(
            f"{ROOT}/demo-profile/1.1/confirm", json=approval(reviewed["content_hash"])
        ).status_code
        == 409
    )
    assert draft(client, "basic").status_code == 409


@pytest.mark.parametrize(
    "override,code,field",
    [
        ({"expected_revision": 2}, 409, "onboarding.revision"),
        ({"content_hash": "0" * 64}, 409, "profile.content_hash"),
        ({"confirmed_by": " "}, 400, "onboarding.confirmed_by"),
        ({"confirmed": False}, 422, None),
    ],
)
def test_rejects_stale_or_nonexplicit_confirmation(
    client: TestClient,
    migrated_engine: Engine,
    override: dict[str, object],
    code: int,
    field: str | None,
) -> None:
    client.post("/onboarding/start")
    reviewed = draft(client).json()
    response = client.post(
        f"{ROOT}/demo-profile/1.0/confirm",
        json=approval(reviewed["content_hash"], **override),
        headers={"X-Request-ID": "invalid-g1"},
    )
    assert response.status_code == code
    assert response.json()["error"]["request_id"] == "invalid-g1"
    if field:
        assert response.json()["error"]["details"]["field"] == field
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert get_onboarding(session).revision == 1
        assert ProfileRepository(session).get("demo-profile", "1.0").status is DocumentStatus.DRAFT
        assert len(EventRepository(session).recent()) == 1


def test_incomplete_draft_can_be_edited_but_not_confirmed(client: TestClient) -> None:
    client.post("/onboarding/start")
    text = (
        (FIXTURES / "basic.md")
        .read_text()
        .replace('reading_goals: ["追 SOTA", "找选题"]', "reading_goals: []")
    )
    response = client.post(
        f"{ROOT}/drafts", json={"profile_id": "demo-profile", "content_md": text}
    )
    assert response.status_code == 201
    assert response.json()["missing_fields"] == ["reading.reading_goals"]
    rejected = client.post(
        f"{ROOT}/demo-profile/1.0/confirm", json=approval(response.json()["content_hash"])
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["details"]["field"] == "reading.reading_goals"
    assert draft(client, "v2", base_version="1.0").json()["g1_ready"]


def test_invalid_import_and_version_conflicts_leave_state_unchanged(client: TestClient) -> None:
    assert draft(client).status_code == 409
    client.post("/onboarding/start")
    invalid = draft(client, "invalid")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["details"]["field"]
    assert "line" in invalid.json()["error"]["details"]
    assert client.get(f"{ROOT}/demo-profile/1.0").status_code == 404
    assert draft(client).status_code == 201
    assert draft(client).status_code == 409
    assert draft(client, "v2").status_code == 409
    assert draft(client, "v2", base_version="missing").status_code == 404
    assert client.get("/onboarding").json()["stage"] == "profile"


def test_cache_corruption_cannot_bypass_g1(client: TestClient, migrated_engine: Engine) -> None:
    client.post("/onboarding/start")
    reviewed = draft(client).json()
    with session_scope(create_session_factory(migrated_engine)) as session:
        row = session.get(ResearchProfileRow, ("demo-profile", "1.0"))
        row.parsed_json = {**row.parsed_json, "exclude": ["伪造缓存"]}
    response = client.post(
        f"{ROOT}/demo-profile/1.0/confirm", json=approval(reviewed["content_hash"])
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["field"] == "profile.parsed_json"


def test_failed_gate_event_rolls_back_document_confirmation(
    client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "papersteward.api.onboarding.uuid4", lambda: SimpleNamespace(hex="collision")
    )
    client.post("/onboarding/start")
    reviewed = draft(client).json()
    response = client.post(
        f"{ROOT}/demo-profile/1.0/confirm", json=approval(reviewed["content_hash"])
    )
    assert response.status_code == 409
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert get_onboarding(session).revision == 1
        assert ProfileRepository(session).get("demo-profile", "1.0").status is DocumentStatus.DRAFT
        assert len(EventRepository(session).recent()) == 1


def test_existing_p1_confirmed_document_is_reused_without_duplicate_event(
    client: TestClient, migrated_engine: Engine
) -> None:
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        profile = parse_profile((FIXTURES / "basic.md").read_text())
        ProfileRepository(session).add(profile)
        confirm_profile(session, profile.id, profile.version, confirmed_at=NOW)
    client.post("/onboarding/start")
    reviewed = client.get(f"{ROOT}/demo-profile/1.0").json()
    assert (
        client.post(
            f"{ROOT}/demo-profile/1.0/confirm", json=approval(reviewed["content_hash"])
        ).status_code
        == 200
    )
    with session_scope(factory) as session:
        assert len(EventRepository(session).recent()) == 3


def test_revised_confirmation_archives_previous_profile(
    client: TestClient, migrated_engine: Engine
) -> None:
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        profile = parse_profile((FIXTURES / "basic.md").read_text())
        ProfileRepository(session).add(profile)
        confirm_profile(session, profile.id, profile.version, confirmed_at=NOW)
    client.post("/onboarding/start")
    reviewed = draft(client, "v2", base_version="1.0").json()
    response = client.post(
        f"{ROOT}/demo-profile/1.1/confirm", json=approval(reviewed["content_hash"])
    )
    assert response.status_code == 200
    with session_scope(factory) as session:
        assert (
            ProfileRepository(session).get("demo-profile", "1.0").status is DocumentStatus.ARCHIVED
        )
        assert (
            ProfileRepository(session).get("demo-profile", "1.1").status is DocumentStatus.CONFIRMED
        )
        assert any(
            e.event_type is EventType.PROFILE_ARCHIVED for e in EventRepository(session).recent()
        )


def test_archived_profile_cannot_be_confirmed_as_g1(
    client: TestClient, migrated_engine: Engine
) -> None:
    client.post("/onboarding/start")
    reviewed = draft(client).json()
    with session_scope(create_session_factory(migrated_engine)) as session:
        confirm_profile(session, "demo-profile", "1.0", confirmed_at=NOW)
        archive_profile(session, "demo-profile", "1.0", archived_at=NOW)
    response = client.post(
        f"{ROOT}/demo-profile/1.0/confirm", json=approval(reviewed["content_hash"])
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["field"] == "profile.status"
    assert client.get("/onboarding").json()["stage"] == "profile"


def test_import_size_is_limited_in_utf8_bytes(client: TestClient) -> None:
    client.post("/onboarding/start")
    response = client.post(
        f"{ROOT}/drafts", json={"profile_id": "demo-profile", "content_md": "中" * 50000}
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "profile.content_md"
    assert client.get(f"{ROOT}/demo-profile/1.0").status_code == 404
