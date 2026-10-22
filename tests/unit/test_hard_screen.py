from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from papersteward.pipeline.screen import ScreenConfig, screen
from papersteward.sources.contracts import PaperCandidate


def candidate(**values):
    return PaperCandidate.model_validate(
        {
            "title": "Synthetic Study",
            "authors": ["Example"],
            "venue": "Fixture Venue",
            "language": "en",
            "document_type": "article",
            "published_at": "2026-10-01T00:00:00Z",
            "doi": "10.0000/classic",
            "provenance": {
                "source": "fake",
                "source_id": "one",
                "url": "https://example.invalid/one",
                "retrieved_at": datetime(2026, 10, 22, tzinfo=UTC),
                "raw_metadata": {},
            },
            **values,
        }
    )


@pytest.mark.parametrize(
    "settings,code",
    [
        ({"languages": ["zh"]}, "language"),
        ({"document_types": ["review"]}, "document_type"),
        ({"date_from": "2026-10-02"}, "date_from"),
        ({"date_to": "2026-09-30"}, "date_to"),
        ({"exclude_terms": ["synthetic"]}, "exclude_term:synthetic"),
        ({"blocked_authors": ["example"]}, "blocked_author:example"),
        ({"blocked_venues": ["Fixture Venue"]}, "blocked_venue:Fixture Venue"),
        ({"predatory_venues": ["Fixture Venue"]}, "predatory_venue:Fixture Venue"),
    ],
)
def test_filter_reasons(settings, code):
    assert screen(candidate(), ScreenConfig.model_validate(settings)) == [code]


def test_date_boundary_classic_exception_and_unknowns():
    config = ScreenConfig(date_from="2026-10-01", date_to="2026-10-01")
    assert screen(candidate(), config) == []
    assert (
        screen(
            candidate(published_at="1900-01-01T00:00:00Z"),
            ScreenConfig(date_from="2026-10-01", classic_ids=("10.0000/classic",)),
        )
        == []
    )
    assert (
        screen(
            candidate(language=None, document_type=None, published_at=None),
            ScreenConfig(languages=("zh",), date_from="2026-10-01"),
        )
        == []
    )
    with pytest.raises(ValidationError):
        ScreenConfig(date_from="2026-10-02", date_to="2026-10-01")
