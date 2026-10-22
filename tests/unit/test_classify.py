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
    build_taxonomy_block,
    classify_paper,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "classifications"
TAXONOMY_TEXT = (ROOT / "tests" / "fixtures" / "taxonomies" / "basic.md").read_text(
    encoding="utf-8"
)
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
)


def _taxonomy(*, draft: bool = False):
    taxonomy = parse_taxonomy(TAXONOMY_TEXT, source="basic.md", document_id="demo-taxonomy")
    if draft:
        return taxonomy
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


def test_valid_classification_maps_tags_and_topic_bucket():
    gateway = _gateway("valid.json")
    result = classify_paper(PAPER, _taxonomy(), _buckets(), gateway)

    assert result.needs_review is False
    assert result.reasons == ()
    assert result.primary_tag.tag_code == "T02"
    assert result.primary_tag.role is TagRole.PRIMARY
    assert result.primary_tag.confidence == 0.86
    assert [tag.tag_code for tag in result.secondary_tags] == ["S01", "S02"]
    assert all(tag.role is TagRole.SECONDARY for tag in result.secondary_tags)
    assert result.bucket.bucket_id == "b-t02" and result.bucket.role is BucketRole.FILING
    assert result.provenance.model == "fake-llm"
    assert result.provenance.prompt_version == "classification/v2"
    assert "检索增强生成" in gateway.provider.calls[0]
    assert "不可信数据" in gateway.provider.calls[0]
    assert result.as_json()["primary_tag"]["tag_code"] == "T02"


def test_ambiguous_and_low_confidence_require_review():
    ambiguous = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("ambiguous.json"))
    assert ambiguous.needs_review is True
    assert "ambiguous_primary" in ambiguous.reasons

    low = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("low_confidence.json"))
    assert low.needs_review is True
    assert "low_primary_confidence" in low.reasons


def test_boundary_note_and_missing_abstract_require_review():
    boundary = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("boundary.json"))
    assert boundary.needs_review is True
    assert "boundary_case" in boundary.reasons

    no_abstract = classify_paper(
        replace(PAPER, abstract=None), _taxonomy(), _buckets(), _gateway("valid.json")
    )
    assert no_abstract.needs_review is True
    assert "missing_abstract" in no_abstract.reasons


def test_evidence_validation_and_missing_evidence():
    invalid = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("invalid_evidence.json"))
    assert invalid.needs_review is True
    assert "invalid_evidence" in invalid.reasons
    assert "missing_evidence" in invalid.reasons

    missing = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("missing_evidence.json"))
    assert "missing_evidence" in missing.reasons


def test_runner_up_missing_or_unknown():
    no_runner = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("no_runner_up.json"))
    assert no_runner.needs_review is False

    invalid_runner = classify_paper(
        PAPER, _taxonomy(), _buckets(), _gateway("invalid_runner_up.json")
    )
    assert invalid_runner.needs_review is True
    assert "invalid_runner_up" in invalid_runner.reasons


def test_secondary_dedupe_and_free_tags():
    duplicate = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("duplicate_secondary.json"))
    assert duplicate.needs_review is True
    assert [tag.tag_code for tag in duplicate.secondary_tags] == ["S01"]
    assert "duplicate_secondary_tag:S01" in duplicate.reasons

    free = classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("free_secondary.json"))
    assert free.needs_review is False
    assert free.secondary_tags[0].tag_code == "custom concept"


def test_unknown_primary_draft_taxonomy_and_missing_bucket_are_rejected():
    with pytest.raises(ClassificationError, match="taxonomy"):
        classify_paper(PAPER, _taxonomy(draft=True), _buckets(), _gateway("valid.json"))
    with pytest.raises(ClassificationError, match="primary_tag"):
        classify_paper(PAPER, _taxonomy(), _buckets(), _gateway("unknown_primary.json"))
    with pytest.raises(ClassificationError, match="主题桶"):
        classify_paper(PAPER, _taxonomy(), {}, _gateway("valid.json"))


def test_taxonomy_block_contains_primary_and_secondary_definitions():
    block = build_taxonomy_block(_taxonomy())
    assert "主标签" in block and "副标签" in block
    assert "T01" in block and "S01" in block
    assert "检索增强生成" in block
