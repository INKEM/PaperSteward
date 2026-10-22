from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Bucket, Paper
from papersteward.core.enums import BucketRole, BucketType, DocumentStatus, OaStatus, TagRole
from papersteward.documents.taxonomy import parse_taxonomy
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.pipeline.classify import (
    ClassificationError,
    classify_paper,
    persist_classification,
)
from papersteward.storage import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    create_session_factory,
    session_scope,
)

ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_TEXT = (ROOT / "tests" / "fixtures" / "taxonomies" / "basic.md").read_text(
    encoding="utf-8"
)
FIXTURES = ROOT / "tests" / "fixtures" / "classifications"
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
PAPER = Paper(
    id="cls-001",
    title="Retrieval-augmented generation for long-document QA",
    abstract=(
        "We study retrieval-augmented generation for long-document QA. "
        "Sparse retrieval and cross-segment reranking improve grounding, "
        "and benchmark evaluation shows fewer unsupported citations."
    ),
    oa_status=OaStatus.OPEN,
    first_seen_at=NOW,
)


def _taxonomy():
    taxonomy = parse_taxonomy(TAXONOMY_TEXT, source="basic.md", document_id="demo-taxonomy")
    return replace(taxonomy, status=DocumentStatus.CONFIRMED, confirmed_at=NOW)


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


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _gateway(name: str) -> LLMGateway:
    return LLMGateway(FakeLLM([_fixture(name)], model_id="fake-llm"), clock=lambda: NOW)


def test_persist_classification_is_idempotent_and_keeps_status(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(PAPER)
        for bucket in _buckets().values():
            BucketRepository(session).add(bucket)
        result = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("valid.json"))
        assert persist_classification(session, result) is True
        assert persist_classification(session, result) is False

        tags = PaperTagRepository(session).list_for_paper(PAPER.id)
        assert {tag.tag_code for tag in tags} == {"T02", "S01", "S02"}
        assert sum(tag.role is TagRole.PRIMARY for tag in tags) == 1
        assert all(tag.provenance is not None for tag in tags if tag.assigned_by.value == "agent")

        buckets = PaperBucketRepository(session).list_for_paper(PAPER.id)
        assert [item.bucket_id for item in buckets] == ["b-t02"]
        assert buckets[0].role is BucketRole.FILING

        paper = PaperRepository(session).get(PAPER.id)
        assert paper is not None and paper.status.value == "discovered"


def test_conflicting_classification_is_rejected_before_writing(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(PAPER)
        for bucket in _buckets().values():
            BucketRepository(session).add(bucket)
        first = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("valid.json"))
        persist_classification(session, first)
        conflicting = classify_paper(
            PAPER, _taxonomy(), _buckets(), _gateway("low_confidence.json")
        )
        with pytest.raises(ClassificationError, match="不同分类"):
            persist_classification(session, conflicting)

        tags = PaperTagRepository(session).list_for_paper(PAPER.id)
        buckets = PaperBucketRepository(session).list_for_paper(PAPER.id)
        assert len(tags) == 3 and len(buckets) == 1
