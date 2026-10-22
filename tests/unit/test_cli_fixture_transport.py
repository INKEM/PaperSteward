import json

import httpx
import pytest

from papersteward.cli.ingestion import _fixture_transport


def test_raw_fixture_is_replayed_for_every_request(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_text("<feed/>", encoding="utf-8")
    with httpx.Client(transport=_fixture_transport(path)) as client:
        assert client.get("https://example.invalid/").content == b"<feed/>"
        assert client.get("https://example.invalid/").content == b"<feed/>"


def test_response_envelope_is_served_in_order_and_never_repeats(tmp_path):
    path = tmp_path / "pages.json"
    path.write_text(json.dumps({"responses": [{"results": []}, "<feed/>"]}), encoding="utf-8")
    with httpx.Client(transport=_fixture_transport(path)) as client:
        first = client.get("https://example.invalid/")
        second = client.get("https://example.invalid/")
        exhausted = client.get("https://example.invalid/")
    assert json.loads(first.content) == {"results": []}
    assert second.content == b"<feed/>"
    assert exhausted.status_code == 400 and b"exhausted" in exhausted.content


@pytest.mark.parametrize("responses", [[], [1], [None]])
def test_invalid_response_envelope_is_rejected(tmp_path, responses):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"responses": responses}), encoding="utf-8")
    with pytest.raises(ValueError, match="fixture responses"):
        _fixture_transport(path)
