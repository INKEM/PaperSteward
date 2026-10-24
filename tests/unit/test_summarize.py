from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Paper
from papersteward.core.enums import OaStatus, SummaryValidationStatus
from papersteward.llm import FakeLLM, LLMGateway, LLMValidationError
from papersteward.pipeline.summarize import (
    SummaryError,
    generate_summary,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "summaries"
NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)

ABSTRACT = (
    "We propose hierarchical retrieval for long-document QA. "
    "Paragraph clustering recall and cross-layer attention reranking form our retrieval pipeline. "
    "The method improves EM by 2–4 points on three long-document QA benchmarks, "
    "but offline clustering adds latency."
)
PAPER = Paper(
    id="sum-001",
    title="Hierarchical retrieval and reranking for long-document QA",
    abstract=ABSTRACT,
    oa_status=OaStatus.OPEN,
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _gateway(*responses: str) -> LLMGateway:
    return LLMGateway(FakeLLM(list(responses), model_id="fake-llm"), clock=lambda: NOW)


def test_valid_summary_covers_four_slots_with_verbatim_evidence():
    gateway = _gateway(_fixture("valid.json"))
    result = generate_summary(PAPER, gateway)

    assert result.needs_review is False
    assert result.summary.validation_status is SummaryValidationStatus.PASSED
    assert result.missing_fields == result.invalid_quotes == ()
    assert result.unsupported_numbers == result.vague_terms == ()
    assert len(result.evidence) == 4 and all(item.valid for item in result.evidence)
    assert len(result.summary.evidence) == 4
    assert result.provenance.model == "fake-llm"
    assert result.provenance.prompt_version == "summary/v2"
    assert "不可信数据" in gateway.provider.calls[0]


def test_missing_evidence_marks_review_and_keeps_valid_quotes():
    result = generate_summary(PAPER, _gateway(_fixture("missing_evidence.json")))

    assert result.needs_review is True
    assert result.summary.validation_status is SummaryValidationStatus.NEEDS_REVIEW
    assert result.missing_fields == ("what", "how", "limitation")
    assert result.summary.evidence == ("improves EM by 2–4 points",)
    assert "missing_evidence:what" in result.reasons


def test_fabricated_evidence_is_invalid_and_reviews():
    result = generate_summary(PAPER, _gateway(_fixture("fabricated_evidence.json")))

    assert result.needs_review is True
    assert result.invalid_quotes == ("a fabricated claim not in the paper",)
    assert "invalid_evidence" in result.reasons
    assert result.missing_fields == ("what",)
    assert "a fabricated claim not in the paper" not in result.summary.evidence


def test_unsupported_number_reviews_even_with_valid_evidence():
    result = generate_summary(PAPER, _gateway(_fixture("unsupported_number.json")))

    assert result.needs_review is True
    assert result.unsupported_numbers == ("7",)
    assert "unsupported_number:7" in result.reasons


def test_vague_language_reviews():
    result = generate_summary(PAPER, _gateway(_fixture("vague.json")))

    assert result.needs_review is True
    assert result.vague_terms == ("很好",)
    assert "vague_language:很好" in result.reasons


def test_missing_abstract_is_rejected_before_llm_call():
    gateway = _gateway(_fixture("valid.json"))
    paper = replace(PAPER, abstract=None)

    with pytest.raises(SummaryError, match="摘要"):
        generate_summary(paper, gateway)

    assert gateway.provider.calls == []


def test_schema_failure_is_repaired_once_then_succeeds():
    gateway = _gateway(_fixture("invalid_schema.json"), _fixture("valid.json"))
    result = generate_summary(PAPER, gateway)

    assert result.summary.validation_status is SummaryValidationStatus.PASSED
    assert result.provenance.prompt_version == "summary/v2+repair:repair/v2"
    assert gateway.provider.call_count == 2


def test_schema_failure_after_repair_propagates():
    gateway = _gateway(_fixture("invalid_schema.json"), _fixture("invalid_schema.json"))

    with pytest.raises(LLMValidationError):
        generate_summary(PAPER, gateway)


def test_summary_json_contains_audit_fields():
    result = generate_summary(PAPER, _gateway(_fixture("missing_evidence.json")))
    payload = result.as_json()

    assert payload["needs_review"] is True
    assert payload["validation_status"] == "needs_review"
    assert payload["missing_fields"] == ["what", "how", "limitation"]
    assert payload["evidence"][0]["valid"] is True
    assert payload["provenance"]["prompt_version"] == "summary/v2"
