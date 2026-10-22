from datetime import UTC

import pytest
from pydantic import ValidationError

from papersteward.sources.contracts import PaperCandidate, QuerySpec, SourceProvenance


@pytest.mark.parametrize(
    "changes",
    [
        {"keywords": []},
        {"keywords": [" "]},
        {"max_results": 0},
        {"max_results": 1001},
        {"max_results": True},
        {"date_from": "2026-10-22", "date_to": "2026-10-21"},
        {"extra": "unsupported"},
    ],
)
def test_query_rejects_invalid_inputs(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QuerySpec.model_validate({"keywords": ["retrieval"], **changes})


def test_query_date_boundaries_and_keyword_order() -> None:
    query = QuerySpec.model_validate(
        {"keywords": [" retrieval ", "QA"], "date_from": "2026-10-21", "date_to": "2026-10-21"}
    )
    assert query.keywords == ("retrieval", "QA")
    assert query.date_from == query.date_to


def provenance() -> SourceProvenance:
    return SourceProvenance.model_validate(
        {
            "source": "fake",
            "source_id": "one",
            "url": "https://example.invalid/one",
            "retrieved_at": "2026-10-22T08:00:00+08:00",
            "raw_metadata": {"nested": [None, True, 1]},
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"authors": [""]},
        {"published_at": "2026-10-21T00:00:00"},
        {"oa_status": "unsupported"},
        {"citation_count": -1},
        {"citation_count": True},
        {"status": "queued"},
        {"doi": " "},
        {"provenance": None},
    ],
)
def test_candidate_rejects_invalid_metadata(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PaperCandidate.model_validate({"title": "Study", "provenance": provenance(), **changes})


def test_candidate_serialization_and_utc() -> None:
    candidate = PaperCandidate.model_validate(
        {
            "title": " Study ",
            "provenance": provenance(),
            "published_at": "2026-10-21T08:00:00+08:00",
        }
    )
    assert candidate.title == "Study"
    assert candidate.authors == () and candidate.abstract is None
    assert candidate.oa_status.value == "unknown"
    assert candidate.published_at is not None and candidate.published_at.tzinfo == UTC
    assert candidate.provenance.retrieved_at.hour == 0
    assert PaperCandidate.model_validate_json(candidate.model_dump_json()) == candidate


@pytest.mark.parametrize(
    "field,value", [("source", " "), ("source_id", ""), ("retrieved_at", "2026-10-22")]
)
def test_provenance_requires_identity_and_timezone(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        SourceProvenance.model_validate({**provenance().model_dump(), field: value})
