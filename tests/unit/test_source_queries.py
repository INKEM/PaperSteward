from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.core.enums import DocumentStatus
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.documents.profile import parse_profile
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.queries import generate_queries

TEXT = (Path(__file__).resolve().parents[1] / "fixtures/profiles/basic.md").read_text()


def profile(text: str = TEXT):
    return replace(
        parse_profile(text),
        status=DocumentStatus.CONFIRMED,
        confirmed_at=datetime(2026, 10, 22, tzinfo=UTC),
    )


def test_stable_queries_with_budget_and_literal_exclusions():
    queries = generate_queries(profile(), max_queries=2, max_results=5)
    assert queries == generate_queries(profile(), max_queries=2, max_results=5)
    assert queries[0].keywords == (
        "long-document retrieval",
        "cross-segment reasoning",
        "reranking",
    )
    assert queries[1].keywords[0] == "RAG"
    assert all(q.max_results == 5 and q.operator == "any" for q in queries)
    assert queries[0].exclude_keywords[0] == "pure prompt-engineering tricks without evaluation"


def test_follow_lists_and_boolean_contract():
    text = TEXT.replace(
        'venues: ["ACL", "EMNLP", "SIGIR"]',
        'venues: ["ACL", "ACL"]\n  authors: ["A. Example"]\n  teams: ["Lab"]',
    )
    queries = generate_queries(profile(text))
    assert [(q.field, q.keywords) for q in queries[-3:]] == [
        ("author", ("A. Example",)),
        ("venue", ("ACL",)),
        ("team", ("Lab",)),
    ]
    assert QuerySpec(keywords=("one", "two"), operator="all").operator == "all"


@pytest.mark.parametrize("budget", [0, 101, True])
def test_bad_query_budget(budget):
    with pytest.raises(DomainValidationError):
        generate_queries(profile(), max_queries=budget)


def test_draft_and_cached_content_drift_are_rejected():
    with pytest.raises(InvariantViolation):
        generate_queries(parse_profile(TEXT))
    with pytest.raises(InvariantViolation):
        generate_queries(replace(profile(), parsed_json={}))


def test_empty_search_fields_and_unstructured_methods():
    text = TEXT
    import re

    text = re.sub(r"keywords: \[.*\]", "keywords: []", text)
    text = text.replace(
        'methods:\n  - "dense retrieval"\n  - "cross-encoder reranking"', "methods: []"
    )
    text = text.replace('data:\n  - "long-document QA benchmarks"', "data: []")
    text = text.replace('venues: ["ACL", "EMNLP", "SIGIR"]', "venues: []")
    with pytest.raises(DomainValidationError, match="没有可用"):
        generate_queries(profile(text))
    with pytest.raises(DomainValidationError):
        generate_queries(
            profile(
                TEXT.replace(
                    'methods:\n  - "dense retrieval"\n  - "cross-encoder reranking"',
                    "methods: {unexpected: shape}",
                )
            )
        )
