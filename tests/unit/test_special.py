from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Paper
from papersteward.core.enums import DocumentStatus, OaStatus, SpecialBucketKind
from papersteward.documents.profile import parse_profile
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.pipeline.special import SpecialError, nominate_special

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "special_nominations"
PROFILE_TEXT = (ROOT / "tests" / "fixtures" / "profiles" / "basic.md").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)

PAPER = Paper(
    id="special-001",
    title="A survey of retrieval and generation evaluation",
    abstract=(
        "This survey compares retrieval and generation evaluation methods and argues that "
        "graph neural network gating may transfer to long-document reranking with a "
        "falsifiable experiment."
    ),
    oa_status=OaStatus.OPEN,
)


def _profile():
    profile = parse_profile(PROFILE_TEXT, source="basic.md", document_id="demo-profile")
    return replace(profile, status=DocumentStatus.CONFIRMED, confirmed_at=NOW)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _gateway(name: str) -> LLMGateway:
    return LLMGateway(FakeLLM([_fixture(name)], model_id="fake-llm"), clock=lambda: NOW)


def test_valid_review_and_distant_selects_review_with_bridge_memo():
    gateway = _gateway("valid.json")
    result = nominate_special(PAPER, _profile(), gateway)

    assert result.needs_review is False
    assert result.reasons == ()
    assert result.selected_kind is SpecialBucketKind.REVIEW
    assert [item.kind for item in result.accepted] == [
        SpecialBucketKind.REVIEW,
        SpecialBucketKind.DISTANT,
    ]
    distant = result.accepted[1]
    assert distant.bridge_memo is not None
    assert distant.bridge_memo.source_mechanism.startswith("图神经网络")
    assert all(item.valid for item in distant.evidence)
    assert result.provenance.prompt_version == "special_nomination/v1"
    assert "不可信数据" in gateway.provider.calls[0]
    assert "bridge_memo" in gateway.provider.calls[0]
    assert result.as_json()["selected_kind"] == "review"


def test_classic_only_selects_classic():
    result = nominate_special(PAPER, _profile(), _gateway("classic.json"))

    assert result.selected_kind is SpecialBucketKind.CLASSIC
    assert result.accepted[0].classic_basis == ("standard",)
    assert result.needs_review is False


def test_low_confidence_is_retained_but_not_selected():
    result = nominate_special(PAPER, _profile(), _gateway("low_confidence.json"))

    assert result.selected_kind is None
    assert result.accepted == ()
    assert result.needs_review is True
    assert "low_confidence:review" in result.reasons
    assert result.nominations[0].accepted is False


def test_invalid_evidence_is_retained_but_not_selected():
    result = nominate_special(PAPER, _profile(), _gateway("invalid_evidence.json"))

    assert result.selected_kind is None
    assert result.needs_review is True
    assert "invalid_evidence:classic" in result.reasons
    assert "missing_evidence:classic" in result.reasons


def test_no_nominations_is_not_an_error():
    result = nominate_special(PAPER, _profile(), _gateway("empty.json"))

    assert result.nominations == ()
    assert result.selected_kind is None
    assert result.needs_review is False
    assert result.reasons == ()


def test_missing_abstract_invalidates_abstract_only_evidence_and_reviews():
    result = nominate_special(replace(PAPER, abstract=None), _profile(), _gateway("valid.json"))

    assert result.selected_kind is SpecialBucketKind.REVIEW
    assert result.needs_review is True
    assert "missing_abstract" in result.reasons
    assert any("distant" in reason for reason in result.reasons)


def test_draft_profile_and_invalid_confidence_are_rejected():
    with pytest.raises(SpecialError, match="已确认"):
        nominate_special(
            PAPER,
            replace(_profile(), status=DocumentStatus.DRAFT, confirmed_at=None),
            _gateway("valid.json"),
        )
    with pytest.raises(SpecialError, match="min_confidence"):
        nominate_special(PAPER, _profile(), _gateway("valid.json"), min_confidence=2.0)
