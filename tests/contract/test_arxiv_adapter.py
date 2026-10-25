from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from papersteward.sources.arxiv import ArxivAdapter, compile_query, parse_feed
from papersteward.sources.contracts import QuerySpec, SourceError
from papersteward.sources.http import SourceHTTP

BODY = (Path(__file__).resolve().parents[1] / "fixtures/sources/arxiv.xml").read_bytes()
NOW = datetime(2026, 10, 22, tzinfo=UTC)


def test_atom_mapping_and_raw_provenance():
    (candidate,) = parse_feed(BODY, NOW)
    assert candidate.title == "Synthetic Retrieval Study"
    assert candidate.arxiv_id == "2610.00001v2"
    assert candidate.doi == "10.0000/fixture" and candidate.authors == ("Fixture Author",)
    assert candidate.published_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert "entry_xml" in candidate.provenance.raw_metadata
    assert candidate.pdf_url and candidate.oa_status.value == "open"


def test_query_compilation_is_literal_and_dates_inclusive():
    query = QuerySpec(
        keywords=("one", "two"),
        operator="all",
        field="author",
        exclude_keywords=("outside",),
        date_from="2026-10-01",
        date_to="2026-10-22",
    )
    assert (
        compile_query(query) == '(au:"one" AND au:"two") ANDNOT all:"outside" '
        "AND submittedDate:[202610010000 TO 202610222359]"
    )
    assert compile_query(QuerySpec(keywords=("one OR all:two",))) == '(all:"one OR all:two")'


@pytest.mark.parametrize(
    "query", [QuerySpec(keywords=('bad"',)), QuerySpec(keywords=("team",), field="team")]
)
def test_unsupported_query(query):
    with pytest.raises(SourceError, match="unsupported_query"):
        compile_query(query)


@pytest.mark.parametrize(
    "body",
    [
        b"bad",
        b"<html/>",
        b"<!DOCTYPE feed><feed/>",
        BODY.replace(b"2026-10-01T00:00:00Z", b"bad"),
        BODY.replace(b"arxiv.org/abs", b"evil.invalid/abs"),
    ],
)
def test_invalid_atom_is_source_error(body):
    with pytest.raises(SourceError, match="invalid_response"):
        parse_feed(body, NOW)


def test_empty_missing_fields_pagination_and_cache():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["User-Agent"].startswith("PaperSteward")
        start = request.url.params["start"]
        return httpx.Response(
            200, content=BODY if start == "0" else b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
        )

    http = SourceHTTP("arxiv", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        adapter = ArxivAdapter(http, now=lambda: NOW, page_size=1)
        query = QuerySpec(keywords=("retrieval",), max_results=2)
        assert len(adapter.search(query)) == 1
        assert adapter.search(query) == adapter.search(query)
        assert http.request_count == 2 and http.cache_hits == 4
    finally:
        http.close()
    minimal = BODY.replace(b"<author><name>Fixture Author</name></author>", b"")
    assert parse_feed(minimal, NOW)[0].authors == ()


@pytest.mark.parametrize("repeat", [False, True])
def test_multiple_pages_or_repeated_page_fails(repeat):
    def handle(request):
        start = int(request.url.params["start"])
        return httpx.Response(
            200,
            content=BODY if repeat or start == 0 else BODY.replace(b"2610.00001", b"2610.00002"),
        )

    http = SourceHTTP("arxiv", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        adapter = ArxivAdapter(http, page_size=1, now=lambda: NOW)
        query = QuerySpec(keywords=("retrieval",), max_results=2)
        if repeat:
            with pytest.raises(SourceError, match="invalid_response"):
                adapter.search(query)
        else:
            assert [p.provenance.source_id for p in adapter.search(query)] == [
                "2610.00001v2",
                "2610.00002v2",
            ]
        assert http.request_count == 2
    finally:
        http.close()


def test_get_by_id_returns_entry_and_none_on_empty_feed():
    requests: list[dict[str, str]] = []
    empty_feed = (
        b'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    )

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        return httpx.Response(200, content=BODY if len(requests) == 1 else empty_feed)

    http = SourceHTTP("arxiv", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        adapter = ArxivAdapter(http, now=lambda: NOW)
        found = adapter.get_by_id("2610.00001")
        assert found is not None and found.arxiv_id == "2610.00001v2"
        assert requests[0]["id_list"] == "2610.00001"
        assert adapter.get_by_id("2610.99999") is None
    finally:
        http.close()
