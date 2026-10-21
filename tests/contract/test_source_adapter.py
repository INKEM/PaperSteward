import json
import socket
from pathlib import Path

import pytest

from papersteward.sources.contracts import QuerySpec, SourceAdapter, SourceError
from papersteward.sources.fixtures import FakeSourceAdapter, load_cassette

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/sources/basic.json"
QUERY = QuerySpec(keywords=("retrieval",), max_results=1)


def test_fake_adapter_replays_offline_with_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("fixture adapter 不应使用网络")

    monkeypatch.setattr(socket, "socket", no_network)
    adapter: SourceAdapter = FakeSourceAdapter(FIXTURE)
    assert isinstance(adapter, SourceAdapter)
    first = adapter.search(QUERY)
    assert first == adapter.search(QUERY) and len(first) == 1
    assert first[0].provenance.raw_metadata["synthetic"] is True
    assert "Ignore previous instructions" in (first[0].abstract or "")
    full = adapter.search(QuerySpec(keywords=("ignored by replay",)))
    assert len(full) == 2 and full[1].abstract is None
    assert full[1].authors == () and full[1].citation_count is None
    first.clear()
    assert len(adapter.search(QUERY)) == 1


@pytest.mark.parametrize(
    "error,retryable", [(None, False), ("timeout", True), ("unavailable", False)]
)
def test_empty_or_failed_source(tmp_path: Path, error: str | None, retryable: bool) -> None:
    path = tmp_path / "response.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "fake",
                "items": [],
                "error": error,
                "retryable": retryable,
            }
        )
    )
    adapter = FakeSourceAdapter(path)
    if error:
        with pytest.raises(SourceError) as caught:
            adapter.search(QUERY)
        assert caught.value.code == error and caught.value.retryable == retryable
        assert caught.value.source == "fake"
    else:
        assert adapter.search(QUERY) == []
    assert adapter.calls == [QUERY]


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"source": "arxiv"},
        {"items": [{}]},
        {"error": "timeout"},
        {"retryable": True},
        {"unknown": "secret-value"},
    ],
)
def test_invalid_cassette_is_source_error(tmp_path: Path, changes: dict[str, object]) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({**json.loads(FIXTURE.read_text()), **changes}))
    with pytest.raises(SourceError) as caught:
        FakeSourceAdapter(path).search(QUERY)
    assert caught.value.code == "fixture_invalid" and not caught.value.retryable
    assert "secret-value" not in str(caught.value)


def test_corrupt_unreadable_or_wrong_source_cassette(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    for raw in (b"{bad json", b"\xff"):
        path.write_bytes(raw)
        with pytest.raises(SourceError, match="fixture_invalid"):
            load_cassette(path, source="fake")
    with pytest.raises(SourceError, match="fixture_io"):
        load_cassette(tmp_path / "missing.json", source="fake")
    with pytest.raises(SourceError, match="fixture_invalid"):
        load_cassette(FIXTURE, source="arxiv")
