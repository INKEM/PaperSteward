import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from papersteward.sources.contracts import QuerySpec, SourceError
from papersteward.sources.http import SourceHTTP
from papersteward.sources.semantic_scholar import SemanticScholarAdapter, compile_query, parse_paper

BODY = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/sources/semantic_scholar.json").read_text()
)
NOW = datetime(2026, 10, 22, tzinfo=UTC)


def test_mapping_and_missing_metadata_tldr_is_not_abstract():
    candidate = parse_paper(BODY["data"][0], NOW)
    assert candidate.abstract is candidate.published_at is None
    assert candidate.doi == "10.0000/fixture" and candidate.citation_count == 2
    assert "tldr" in candidate.provenance.raw_metadata
    minimal = parse_paper({"paperId": "b" * 40, "title": "Unknown"}, NOW)
    assert minimal.authors == () and minimal.oa_status.value == "unknown"


def test_boolean_compiler_and_field_rejection():
    assert (
        compile_query(QuerySpec(keywords=("one", "two"), exclude_keywords=("three",)))
        == '("one" | "two") -"three"'
    )
    with pytest.raises(SourceError, match="unsupported_query"):
        compile_query(QuerySpec(keywords=("author",), field="author"))
    with pytest.raises(SourceError):
        compile_query(QuerySpec(keywords=('bad"',)))


@pytest.mark.parametrize(
    "change",
    [{"paperId": "bad"}, {"title": None}, {"publicationDate": "bad"}, {"citationCount": -1}],
)
def test_invalid_paper(change):
    with pytest.raises(SourceError, match="invalid_response"):
        parse_paper({**BODY["data"][0], **change}, NOW)


def test_token_pagination_budget_and_key_header():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["x-api-key"] == "offline-key"
        assert "offline-key" not in str(request.url)
        if "token" not in request.url.params:
            return httpx.Response(200, json={**BODY, "token": "next"})
        return httpx.Response(200, json={"data": [{**BODY["data"][0], "paperId": "b" * 40}]})

    http = SourceHTTP(
        "semantic_scholar",
        transport=httpx.MockTransport(handle),
        sleep=lambda _: None,
        api_key=SecretStr("offline-key"),
    )
    try:
        result = SemanticScholarAdapter(http, now=lambda: NOW).search(
            QuerySpec(keywords=("retrieval",), date_to="2026-10-22", max_results=2)
        )
        assert len(result) == 2 and http.request_count == 2
        assert requests[0].url.params["publicationDateOrYear"] == ":2026-10-22"
        assert "offline-key" not in json.dumps([p.model_dump(mode="json") for p in result])
        with pytest.raises(SourceError):
            http.get("https://api.openalex.org/works", {})
    finally:
        http.close()


@pytest.mark.parametrize("payload", [b"bad", b"[]", b'{"data":null}', b'{"data":[]}'])
def test_empty_and_malformed_response(payload):
    http = SourceHTTP(
        "semantic_scholar",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)),
    )
    try:
        adapter = SemanticScholarAdapter(http)
        if payload == b'{"data":[]}':
            assert adapter.search(QuerySpec(keywords=("one",))) == []
        else:
            with pytest.raises(SourceError, match="invalid_response"):
                adapter.search(QuerySpec(keywords=("one",)))
    finally:
        http.close()
