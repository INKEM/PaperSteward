from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from papersteward.api import create_app
from papersteward.core.entities import Bucket, Paper
from papersteward.core.enums import BucketType, DocumentStatus, PaperStatus
from papersteward.documents.taxonomy import parse_taxonomy
from papersteward.storage import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ReviewRepository,
    TaxonomyRepository,
    create_session_factory,
    session_scope,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)
TAXONOMY = parse_taxonomy(
    (ROOT / "tests/fixtures/taxonomies/basic.md").read_text(encoding="utf-8"),
    source="basic.md",
    document_id="demo-taxonomy",
)
TAXONOMY = TAXONOMY.__class__(
    **{
        **{field: getattr(TAXONOMY, field) for field in TAXONOMY.__dataclass_fields__},
        "status": DocumentStatus.CONFIRMED,
        "confirmed_at": NOW,
    }
)


def _buckets() -> dict[str, Bucket]:
    return {
        f"T{index:02d}": Bucket(
            id=f"b-t{index:02d}",
            name=f"Topic T{index:02d}",
            bucket_type=BucketType.TOPIC,
            tag_code=f"T{index:02d}",
        )
        for index in range(1, 6)
    }


def _paper(paper_id: str) -> Paper:
    return Paper(
        id=paper_id,
        title=f"Synthetic review paper {paper_id}",
        status=PaperStatus.NEEDS_REVIEW,
        first_seen_at=NOW,
    )


def _seed(migrated_engine, paper_id: str = "review-001") -> Paper:
    factory = create_session_factory(migrated_engine)
    paper = _paper(paper_id)
    with session_scope(factory) as session:
        TaxonomyRepository(session).add(TAXONOMY)
        for bucket in _buckets().values():
            BucketRepository(session).add(bucket)
        PaperRepository(session).add(paper)
        ReviewRepository(session).open(
            paper.id,
            ["classification:low_primary_confidence"],
            {
                "taxonomy_id": TAXONOMY.id,
                "taxonomy_version": TAXONOMY.version,
                "classification": {"primary_tag": "T01"},
            },
            now=NOW,
        )
    return paper


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr("papersteward.pipeline.review.utc_now", lambda: NOW)
    with TestClient(create_app(engine=migrated_engine)) as test_client:
        yield test_client


def test_list_and_approve_review(client: TestClient, migrated_engine):
    paper = _seed(migrated_engine)
    listing = client.get("/review").json()
    assert listing["total"] == 1
    assert listing["items"][0]["paper_id"] == paper.id
    assert listing["items"][0]["paper_status"] == "needs_review"
    assert "classification:low_primary_confidence" in listing["items"][0]["reasons"]

    response = client.post(
        f"/review/{paper.id}/approve",
        json={"reviewed_by": "fixture-user", "note": "合成审核"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["paper_status"] == "classified"
    assert client.get("/review").json()["total"] == 0

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        assert PaperRepository(session).get(paper.id).status is PaperStatus.CLASSIFIED
        item = ReviewRepository(session).get(paper.id)
        assert item is not None and item.resolution.value == "approved"


def test_relabel_replaces_tags_and_bucket(client: TestClient, migrated_engine):
    paper = _seed(migrated_engine, "review-002")
    response = client.post(
        f"/review/{paper.id}/relabel",
        json={
            "reviewed_by": "fixture-user",
            "primary_tag": "T03",
            "secondary_tags": ["S01"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["paper_status"] == "classified"

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        tags = PaperTagRepository(session).list_for_paper(paper.id)
        assert {tag.tag_code for tag in tags} == {"T03", "S01"}
        buckets = PaperBucketRepository(session).list_for_paper(paper.id)
        assert [bucket.bucket_id for bucket in buckets] == ["b-t03"]


def test_missing_review_returns_404_and_duplicate_approve_conflicts(
    client: TestClient, migrated_engine
):
    assert client.post("/review/missing/approve", json={"reviewed_by": "user"}).status_code == 404
    paper = _seed(migrated_engine, "review-003")
    assert (
        client.post(f"/review/{paper.id}/approve", json={"reviewed_by": "user"}).status_code == 200
    )
    assert (
        client.post(f"/review/{paper.id}/approve", json={"reviewed_by": "user"}).status_code == 409
    )
