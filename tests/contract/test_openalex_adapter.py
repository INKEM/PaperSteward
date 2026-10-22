import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from papersteward.sources.contracts import QuerySpec, SourceError
from papersteward.sources.http import SourceHTTP
from papersteward.sources.openalex import OpenAlexAdapter, parse_work

BODY = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/sources/openalex.json").read_text()
)
NOW = datetime(2026, 10, 22, tzinfo=UTC)


def test_metadata_and_missing_optional_fields():
    candidate = parse_work(BODY["results"][0], NOW)
    assert candidate.abstract == "Synthetic retrieval" and candidate.citation_count == 7
    assert candidate.venue == "Fixture Venue" and candidate.authors == ("Fixture Author",)
    assert candidate.published_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert candidate.provenance.raw_metadata == BODY["results"][0]
    minimal = parse_work({"id": "https://openalex.org/W2", "title": "Unknown"}, NOW)
    assert minimal.abstract is minimal.published_at is minimal.citation_count is None
    assert minimal.authors == () and minimal.oa_status.value == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"id": "bad"},
        {"title": None},
        {"publication_date": "bad"},
        {"abstract_inverted_index": {"one": [-1]}},
        {"cited_by_count": -1},
    ],
)
def test_invalid_work(change):
    with pytest.raises(SourceError, match="invalid_response"):
        parse_work({**BODY["results"][0], **change}, NOW)


def test_pagination_keeps_page_size_and_trims_last_page():
    requests = []

    def handle(request):
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {**BODY["results"][0], "id": f"https://openalex.org/W{page * 10 + i}"}
                    for i in range(2)
                ]
            },
        )

    http = SourceHTTP("openalex", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        result = OpenAlexAdapter(http, page_size=2, now=lambda: NOW).search(
            QuerySpec(
                keywords=("retrieval", "QA"),
                exclude_keywords=("bad",),
                max_results=3,
                date_from="2026-10-01",
            )
        )
        assert len(result) == 3 and len(requests) == 2
        assert all(r.url.params["per_page"] == "2" for r in requests)
        assert requests[0].url.params["search"] == '("retrieval" OR "QA") NOT "bad"'
        assert requests[0].url.params["filter"] == "from_publication_date:2026-10-01"
    finally:
        http.close()


@pytest.mark.parametrize(
    "field,endpoint,prefix",
    [("author", "authors", "A"), ("venue", "sources", "S"), ("team", "institutions", "I")],
)
def test_follow_list_resolves_exact_identity(field, endpoint, prefix):
    def handle(request):
        if request.url.path == "/" + endpoint:
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 1},
                    "results": [
                        {"display_name": "Example", "id": f"https://openalex.org/{prefix}1"}
                    ],
                },
            )
        assert f"https://openalex.org/{prefix}1" in request.url.params["filter"]
        return httpx.Response(200, json={"results": []})

    http = SourceHTTP("openalex", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        assert OpenAlexAdapter(http).search(QuerySpec(keywords=("Example",), field=field)) == []
    finally:
        http.close()


def test_follow_list_paginates_until_exact_identity_is_found():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/authors":
            page = int(request.url.params["page"])
            if page == 1:
                results = [
                    {"display_name": "Other", "id": f"https://openalex.org/A9{i:03d}"}
                    for i in range(100)
                ]
            else:
                results = [{"display_name": "Example", "id": "https://openalex.org/A1"}]
            return httpx.Response(200, json={"meta": {"count": 101}, "results": results})
        assert "authorships.author.id:https://openalex.org/A1" in request.url.params["filter"]
        return httpx.Response(200, json={"results": []})

    http = SourceHTTP("openalex", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        assert OpenAlexAdapter(http).search(QuerySpec(keywords=("Example",), field="author")) == []
        assert [request.url.params["page"] for request in requests[:2]] == ["1", "2"]
    finally:
        http.close()


def test_follow_list_rejects_ambiguous_or_unscanned_identity():
    def ambiguous(request):
        if request.url.path == "/authors":
            return httpx.Response(
                200,
                json={
                    "meta": {"count": 2},
                    "results": [
                        {"display_name": "Example", "id": "https://openalex.org/A1"},
                        {"display_name": "example", "id": "https://openalex.org/A2"},
                    ],
                },
            )
        raise AssertionError("ambiguous identity must not query works")

    http = SourceHTTP("openalex", transport=httpx.MockTransport(ambiguous), sleep=lambda _: None)
    try:
        with pytest.raises(SourceError, match="unsupported_query"):
            OpenAlexAdapter(http).search(QuerySpec(keywords=("Example",), field="author"))
    finally:
        http.close()

    def partial(request):
        return httpx.Response(
            200,
            json={
                "meta": {"count": 500},
                "results": [
                    {"display_name": "Example", "id": "https://openalex.org/A1"},
                    *[
                        {"display_name": "Other", "id": f"https://openalex.org/A9{i:03d}"}
                        for i in range(99)
                    ],
                ],
            },
        )

    http = SourceHTTP("openalex", transport=httpx.MockTransport(partial), sleep=lambda _: None)
    try:
        with pytest.raises(SourceError, match="unsupported_query"):
            OpenAlexAdapter(http, identity_max_pages=1).search(
                QuerySpec(keywords=("Example",), field="author")
            )
    finally:
        http.close()


def test_follow_list_requires_identity_total_count():
    http = SourceHTTP(
        "openalex",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"results": [{"display_name": "Example", "id": "https://openalex.org/A1"}]},
            )
        ),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(SourceError, match="invalid_response"):
            OpenAlexAdapter(http).search(QuerySpec(keywords=("Example",), field="author"))
    finally:
        http.close()


@pytest.mark.parametrize("payload", [b"bad", b"[]", b'{"results":null}'])
def test_malformed_response(payload):
    http = SourceHTTP(
        "openalex", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    )
    try:
        with pytest.raises(SourceError, match="invalid_response"):
            OpenAlexAdapter(http).search(QuerySpec(keywords=("one",)))
    finally:
        http.close()
