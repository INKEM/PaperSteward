from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from papersteward.api import create_app
from papersteward.core.entities import Paper, PaperTag
from papersteward.core.enums import DocumentStatus, EventType, TagRole
from papersteward.core.errors import InvariantViolation
from papersteward.documents import confirm_profile, parse_profile
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import PaperRow, ResearchProfileRow, TagRow
from papersteward.storage.onboarding import ensure_active, get_onboarding
from papersteward.storage.repositories import (
    EventRepository,
    PaperRepository,
    PaperTagRepository,
    ProfileRepository,
    TaxonomyRepository,
)

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEXT = (FIXTURES / "taxonomies/onboarding.md").read_text()
ROOT = "/onboarding/taxonomy"
NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)


@pytest.fixture
def client(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("papersteward.api.onboarding.utc_now", lambda: NOW)
    with TestClient(create_app(engine=migrated_engine)) as client:
        yield client


def g1(client: TestClient) -> None:
    assert client.post("/onboarding/start").status_code == 200
    draft = client.post(
        "/onboarding/profile/drafts",
        json={
            "profile_id": "demo-profile",
            "content_md": (FIXTURES / "profiles/basic.md").read_text(),
        },
    ).json()
    assert (
        client.post(
            "/onboarding/profile/demo-profile/1.0/confirm",
            json={
                "confirmed": True,
                "confirmed_by": "fixture-user",
                "change_summary": "确认合成档案",
                "expected_revision": 1,
                "content_hash": draft["content_hash"],
            },
        ).status_code
        == 200
    )


def payload(**overrides: object) -> dict[str, object]:
    return {
        "taxonomy_id": "onboarding-taxonomy",
        "content_md": TEXT,
        "sample_kind": "synthetic",
        "seeds": [
            {
                "paper_id": f"syn-T{tag:02}-{index}",
                "title": f"合成 {tag}/{index}",
                "primary_tag": f"T{tag:02}",
            }
            for tag in range(1, 6)
            for index in range(1, 5)
        ],
        **overrides,
    }


def approval(review: dict, **overrides: object) -> dict[str, object]:
    return {
        "confirmed": True,
        "confirmed_by": "fixture-user",
        "change_summary": "已核对分类法与影响预览",
        "expected_revision": 2,
        "content_hash": review["content_hash"],
        "review_hash": review["review_hash"],
        **overrides,
    }


def test_edit_impact_preview_and_g2_preserve_existing_paper_data(
    client: TestClient, migrated_engine: Engine
) -> None:
    g1(client)
    first = client.post(f"{ROOT}/drafts", json=payload())
    assert first.status_code == 201
    assert first.json()["report"]["sample_notice"] == "仅流程验证"
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        papers = PaperRepository(session)
        papers.add(Paper(id="syn-T01-4", title="既有未读文献"))
        # 模拟 P1 留存行；新入队仍由 G4 禁止。
        session.get(PaperRow, "syn-T01-4").status = "queued"
        PaperTagRepository(session).add(
            PaperTag(
                paper_id="syn-T01-4", tag_code="T01", role=TagRole.PRIMARY, taxonomy_version="1.0"
            )
        )
    edited = payload(
        content_md=TEXT.replace('version: "1.0"', 'version: "1.1"')
        .replace("## T01 检索", "## T01 召回与重排")
        .replace("贡献是检索机制", "贡献仍是检索机制"),
        base_version="1.0",
    )
    edited["seeds"][3]["primary_tag"] = "T02"
    response = client.post(f"{ROOT}/drafts", json=edited)
    assert response.status_code == 201
    review = response.json()
    assert review["report"]["g2_ready"]
    assert review["report"]["seed_changes"] == [
        {"paper_id": "syn-T01-4", "from_tag": "T01", "to_tag": "T02"}
    ]
    assert review["report"]["queue_impact"][0]["to_tag"] == "T02"
    assert review["report"]["boundary_changes"]
    assert review["report"]["query_rubric_impact"]
    assert "name" in review["diff"] and "召回与重排" in review["markdown_diff"]
    assert client.get(f"{ROOT}/onboarding-taxonomy/1.1").json() == review
    confirmed = client.post(
        f"{ROOT}/onboarding-taxonomy/1.1/confirm",
        json=approval(review),
        headers={"X-Request-ID": "g2"},
    )
    assert confirmed.status_code == 200
    assert confirmed.headers["X-Request-ID"] == "g2"
    assert confirmed.json() == {"stage": "calibration", "revision": 3, "next_gate": "g3"}
    with session_scope(factory) as session:
        assert (
            TaxonomyRepository(session).get("onboarding-taxonomy", "1.1").status
            is DocumentStatus.CONFIRMED
        )
        assert (
            TaxonomyRepository(session).get("onboarding-taxonomy", "1.0").status
            is DocumentStatus.DRAFT
        )
        assert PaperRepository(session).count() == 1
        assert PaperTagRepository(session).list_for_paper("syn-T01-4")[0].tag_code == "T01"
        event = next(
            e
            for e in EventRepository(session).recent()
            if e.event_type is EventType.ONBOARDING_G2_CONFIRMED
        )
        assert event.payload_json["evidence"]["review_hash"] == review["review_hash"]
        assert event.payload_json["evidence"]["profile"]["id"] == "demo-profile"
        assert event.payload_json["evidence"]["report"] == review["report"]
        with pytest.raises(InvariantViolation, match="G4"):
            ensure_active(session, action="push")
    assert (
        client.post(f"{ROOT}/onboarding-taxonomy/1.1/confirm", json=approval(review)).status_code
        == 409
    )
    assert client.post(f"{ROOT}/drafts", json=payload()).status_code == 409


@pytest.mark.parametrize(
    "kind,field",
    [
        ("empty", "taxonomy.coverage"),
        ("coverage", "taxonomy.coverage"),
        ("balance", "taxonomy.balance"),
        ("boundary", "taxonomy.T01.boundaries"),
    ],
)
def test_incomplete_report_is_visible_and_blocks_g2(
    client: TestClient, migrated_engine: Engine, kind: str, field: str
) -> None:
    g1(client)
    body = payload()
    if kind == "empty":
        body["seeds"] = []
    elif kind == "coverage":
        for index in (3, 7, 11):
            body["seeds"][index]["primary_tag"] = None
    elif kind == "balance":
        for seed in body["seeds"]:
            seed["primary_tag"] = "T01"
    else:
        body["content_md"] = TEXT.replace(
            "- 边界：检索器与生成器共同改进但检索是主要贡献；判为：T01；理由：检索实验是主线。",
            "- 边界：缺少理由",
        )
    draft = client.post(f"{ROOT}/drafts", json=body)
    assert draft.status_code == 201
    assert not draft.json()["report"]["g2_ready"]
    rejected = client.post(f"{ROOT}/onboarding-taxonomy/1.0/confirm", json=approval(draft.json()))
    assert rejected.status_code == 400
    assert rejected.json()["error"]["details"]["field"] == field
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert get_onboarding(session).revision == 2
        assert (
            TaxonomyRepository(session).get("onboarding-taxonomy", "1.0").status
            is DocumentStatus.DRAFT
        )
        assert not any(
            e.event_type is EventType.ONBOARDING_G2_CONFIRMED
            for e in EventRepository(session).recent()
        )


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"review_hash": "0" * 64}, 409),
        ({"content_hash": "0" * 64}, 409),
        ({"expected_revision": 1}, 409),
        ({"confirmed": False}, 422),
    ],
)
def test_stale_or_nonexplicit_g2_confirmation_fails(
    client: TestClient, overrides: dict, code: int
) -> None:
    g1(client)
    review = client.post(f"{ROOT}/drafts", json=payload()).json()
    response = client.post(
        f"{ROOT}/onboarding-taxonomy/1.0/confirm", json=approval(review, **overrides)
    )
    assert response.status_code == code
    assert client.get("/onboarding").json()["revision"] == 2


def test_exact_g1_binding_ignores_another_profile_and_rejects_later_drift(
    client: TestClient, migrated_engine: Engine
) -> None:
    g1(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        other = parse_profile((FIXTURES / "profiles/basic.md").read_text(), document_id="other")
        ProfileRepository(session).add(other)
        confirm_profile(session, "other", "1.0", confirmed_at=NOW)
    review = client.post(f"{ROOT}/drafts", json=payload()).json()
    assert review["report"]["profile"]["id"] == "demo-profile"
    with session_scope(factory) as session:
        row = session.get(ResearchProfileRow, ("demo-profile", "1.0"))
        row.content_md += "\n已改变的研究边界"
    assert client.get(f"{ROOT}/onboarding-taxonomy/1.0").status_code == 409
    assert (
        client.post(f"{ROOT}/onboarding-taxonomy/1.0/confirm", json=approval(review)).status_code
        == 409
    )


def test_changed_queue_or_tag_cache_invalidates_review(
    client: TestClient, migrated_engine: Engine
) -> None:
    g1(client)
    review = client.post(f"{ROOT}/drafts", json=payload()).json()
    with session_scope(create_session_factory(migrated_engine)) as session:
        PaperRepository(session).add(Paper(id="legacy", title="既有队列"))
        session.get(PaperRow, "legacy").status = "queued"
        PaperTagRepository(session).add(
            PaperTag(
                paper_id="legacy", tag_code="T01", role=TagRole.PRIMARY, taxonomy_version="1.0"
            )
        )
    stale = client.post(f"{ROOT}/onboarding-taxonomy/1.0/confirm", json=approval(review))
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["field"] == "taxonomy.review_hash"
    fresh = client.get(f"{ROOT}/onboarding-taxonomy/1.0").json()
    with session_scope(create_session_factory(migrated_engine)) as session:
        session.get(TagRow, ("onboarding-taxonomy", "1.0", "T01")).definition = "缓存伪造"
    assert (
        client.post(f"{ROOT}/onboarding-taxonomy/1.0/confirm", json=approval(fresh)).status_code
        == 409
    )


def test_document_and_gate_events_roll_back_together(
    client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    g1(client)
    review = client.post(f"{ROOT}/drafts", json=payload()).json()
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        start = next(
            e
            for e in EventRepository(session).recent()
            if e.event_type is EventType.ONBOARDING_STARTED
        )
    monkeypatch.setattr(
        "papersteward.api.onboarding.uuid4",
        lambda: SimpleNamespace(hex=start.id.removeprefix("evt-onboarding-")),
    )
    assert (
        client.post(f"{ROOT}/onboarding-taxonomy/1.0/confirm", json=approval(review)).status_code
        == 409
    )
    with session_scope(factory) as session:
        assert get_onboarding(session).revision == 2
        assert (
            TaxonomyRepository(session).get("onboarding-taxonomy", "1.0").status
            is DocumentStatus.DRAFT
        )
        assert len(EventRepository(session).recent()) == 4


def test_draft_input_and_audit_failures_leave_no_partial_version(
    client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post(f"{ROOT}/drafts", json=payload()).status_code == 409
    g1(client)
    duplicate = payload()
    duplicate["seeds"].append(duplicate["seeds"][0])
    assert client.post(f"{ROOT}/drafts", json=duplicate).status_code == 400
    assert client.post(f"{ROOT}/drafts", json=payload(taxonomy_id="wrong")).status_code == 400
    assert client.post(f"{ROOT}/drafts", json=payload(content_md="not markdown")).status_code == 400
    assert client.get(f"{ROOT}/onboarding-taxonomy/missing").status_code == 404
    monkeypatch.setattr("papersteward.api.onboarding.utc_now", lambda: NOW - timedelta(days=1))
    assert client.post(f"{ROOT}/drafts", json=payload()).status_code == 409
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert TaxonomyRepository(session).list() == []
        assert len(EventRepository(session).recent()) == 3


def test_boundary_only_changes_and_removed_seeds_are_visible(
    client: TestClient, migrated_engine: Engine
) -> None:
    g1(client)
    assert client.post(f"{ROOT}/drafts", json=payload()).status_code == 201
    with session_scope(create_session_factory(migrated_engine)) as session:
        PaperRepository(session).add(Paper(id="old-paper", title="边界待复核"))
        session.get(PaperRow, "old-paper").status = "queued"
        PaperTagRepository(session).add(
            PaperTag(
                paper_id="old-paper", tag_code="T01", role=TagRole.PRIMARY, taxonomy_version="1.0"
            )
        )
    body = payload(
        content_md=TEXT.replace('version: "1.0"', 'version: "1.1"').replace(
            "检索实验是主线", "主要贡献在检索实验"
        ),
        base_version="1.0",
    )
    body["seeds"].pop()
    response = client.post(f"{ROOT}/drafts", json=body)
    assert response.status_code == 201
    report = response.json()["report"]
    assert report["g2_ready"]
    assert report["removed_seed_ids"] == ["syn-T05-4"]
    assert report["baseline_seed_count"] == 20 and report["seed_count"] == 19
    assert report["queue_impact"][0]["paper_id"] == "old-paper"
    assert report["query_rubric_impact"][0]["boundary_rules_changed"]
