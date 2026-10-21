from datetime import UTC, datetime

import pytest

from papersteward.pipeline.dedupe import canonicalize, deduplicate, duplicate_reason
from papersteward.sources.contracts import PaperCandidate


def candidate(source="fake", source_id="one", **changes):
    return PaperCandidate.model_validate(
        {
            "title": "Efficient sparse retrieval for very long documents",
            "authors": ["A. Example"],
            "venue": "Fixture Venue",
            "published_at": "2026-10-01T00:00:00Z",
            "provenance": {
                "source": source,
                "source_id": source_id,
                "url": "https://example.invalid/one",
                "retrieved_at": datetime(2026, 10, 22, tzinfo=UTC),
                "raw_metadata": {"synthetic": True},
            },
            **changes,
        }
    )


@pytest.mark.parametrize(
    "left,right,reason",
    [
        ({"doi": "https://doi.org/10.0000/ABC"}, {"doi": "doi:10.0000/abc"}, "doi"),
        (
            {"arxiv_id": "https://arxiv.org/pdf/2610.00001v2.pdf"},
            {"arxiv_id": "2610.00001v1"},
            "arxiv",
        ),
        ({}, {"title": "Efficient sparse retrieval for VERY LONG DOCUMENTS!"}, "title"),
        ({}, {"title": "Sparse efficient retrieval for very long documents"}, "author_year"),
        (
            {
                "title": "A lexical test about document retrieval",
                "abstract": " ".join(f"token{i}" for i in range(60)),
            },
            {
                "title": "Completely distinct wording for another version",
                "abstract": " ".join(f"token{i}" for i in range(60)),
            },
            "abstract",
        ),
    ],
)
def test_five_duplicate_cases(left, right, reason):
    assert duplicate_reason(candidate(**left), candidate(source_id="two", **right)) == reason
    groups = deduplicate([candidate(**left), candidate(source_id="two", **right)])
    assert len(groups) == 1 and groups[0].reasons == (reason,)


def test_formal_metadata_earliest_date_and_provenance_are_preserved():
    preprint = candidate(
        "arxiv",
        "2610.00001v1",
        arxiv_id="2610.00001v1",
        venue=None,
        abstract="earliest abstract",
        document_type="preprint",
    )
    formal = candidate(
        "openalex",
        "W1",
        doi="10.0000/ABC",
        arxiv_id="2610.00001",
        published_at="2026-10-21T00:00:00Z",
        citation_count=9,
        abstract="later abstract",
    )
    (group,) = deduplicate([preprint, formal])
    assert group.candidate.doi == "10.0000/abc" and group.candidate.venue == "Fixture Venue"
    assert group.candidate.published_at == preprint.published_at
    assert group.candidate.abstract == "earliest abstract" and group.candidate.citation_count == 9
    assert len(group.provenances) == 2
    assert deduplicate([formal, preprint]) == [group]
    assert group.canonical_id.startswith("p_")


def test_conflicting_ids_and_same_author_year_do_not_merge():
    assert duplicate_reason(candidate(doi="10.0000/one"), candidate(doi="10.0000/two")) is None
    assert (
        duplicate_reason(candidate(), candidate(title="A completely different investigation"))
        is None
    )
    assert duplicate_reason(candidate(title="Short"), candidate(title="Short", authors=[])) is None
    assert (
        duplicate_reason(
            candidate(abstract="template"), candidate(title="Distinct topic", abstract="template")
        )
        is None
    )


def test_strong_id_bridge_merges_previously_separate_groups():
    a = candidate(source_id="a", doi="10.0000/one", title="First distinct title", arxiv_id=None)
    b = candidate(source_id="b", doi=None, title="Second distinct title", arxiv_id="2610.00002")
    bridge = candidate(source_id="z", doi="10.0000/one", arxiv_id="2610.00002")
    groups = deduplicate([a, b, bridge])
    assert len(groups) == 1 and len(groups[0].members) == 3
    assert len(groups[0].reasons) == 2
    assert canonicalize(candidate(document_type="JournalArticle")).document_type == "article"
    assert deduplicate([]) == []


def test_missing_id_does_not_bridge_conflicting_known_papers():
    assert (
        len(
            deduplicate(
                [
                    candidate(source_id="one", doi="10.0000/one"),
                    candidate(source_id="two", doi="10.0000/two"),
                    candidate(source_id="unknown"),
                ]
            )
        )
        == 3
    )


def test_ambiguous_short_titles_have_distinct_fallback_ids():
    groups = deduplicate(
        [
            candidate(source_id="one", title="Short", authors=[]),
            candidate(source_id="two", title="Short", authors=[]),
        ]
    )
    assert len({group.canonical_id for group in groups}) == 2
