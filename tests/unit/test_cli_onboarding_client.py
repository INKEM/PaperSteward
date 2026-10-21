import io
import json
import stat
from urllib.error import HTTPError, URLError

import pytest

from papersteward.cli.main import main
from papersteward.cli.onboarding_client import (
    Client,
    ClientError,
    read_snapshot,
    save_snapshot,
    snapshot,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example",
        "http://127.0.0.1:8000/path",
        "http://user:secret@localhost",
        "http://localhost#fragment",
        "file:///etc/passwd",
        "http://127.0.0.1:bad",
        "http://localhost?token=secret",
    ],
)
def test_nonlocal_or_ambiguous_urls_are_rejected(url):
    with pytest.raises(ClientError, match="本地 API"):
        Client(url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000/", "https://localhost:8443", "http://[::1]:8000"]
)
def test_local_url_forms(url):
    assert Client(url).url == url.rstrip("/")


def test_snapshot_checksum_server_binding_and_private_permissions(tmp_path):
    client = Client("http://127.0.0.1:8000")
    value = snapshot(
        client, "profile", {"stage": "profile", "revision": 1}, {"content_md": "完整正文"}
    )
    path = tmp_path / "review.json"
    save_snapshot(str(path), value)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_snapshot(str(path), client) == value
    with pytest.raises(ClientError, match="另一 API"):
        read_snapshot(str(path), Client("http://localhost:8000"))
    data = json.loads(path.read_text())
    data["data"]["content_md"] = "编辑后"
    path.write_text(json.dumps(data))
    with pytest.raises(ClientError, match="已被编辑"):
        read_snapshot(str(path), client)
    with pytest.raises(ClientError):
        save_snapshot(str(path), value)


@pytest.mark.parametrize(
    "status,details",
    [
        (409, {"field": "onboarding.revision"}),
        (422, [{"field": "body.confirmed", "message": "必须确认"}]),
        (500, None),
    ],
)
def test_http_error_envelope_has_actionable_hint_and_request_id(monkeypatch, status, details):
    client = Client("http://127.0.0.1:8000")
    calls = []

    def fail(request, timeout):
        calls.append(request)
        error = {
            "error": {
                "code": "conflict",
                "message": "请审阅",
                "details": details,
                "request_id": "req-test",
            }
        }
        raise HTTPError(
            request.full_url, status, "failure", {}, io.BytesIO(json.dumps(error).encode())
        )

    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(ClientError, match=f"HTTP {status}") as error:
        client.request("POST", "/onboarding/start")
    assert "req-test" in str(error.value) and "自动重试" in str(error.value)
    assert len(calls) == 1
    if status == 422:
        assert "body.confirmed" in str(error.value)


def test_connect_failure_never_retries(monkeypatch):
    client = Client("http://127.0.0.1:8000")
    calls = []

    def fail(request, timeout):
        calls.append(timeout)
        raise URLError("offline")

    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(ClientError, match="启动 uvicorn"):
        client.request("POST", "/onboarding/start")
    assert calls == [10]


def test_confirm_requires_explicit_flag_before_any_request(monkeypatch):
    calls = []
    monkeypatch.setattr(Client, "request", lambda *args: calls.append(args))
    with pytest.raises(SystemExit) as result:
        main(
            [
                "onboarding",
                "confirm",
                "--gate",
                "g1",
                "--review",
                "missing.json",
                "--by",
                "user",
                "--summary",
                "reviewed",
            ]
        )
    assert result.value.code == 2 and not calls


def test_readable_output_does_not_execute_terminal_control_sequences(monkeypatch, capsys):
    def request(self, method, path, body=None):
        return (
            {"stage": "profile", "revision": 1}
            if path == "/onboarding"
            else {"content_md": "# 全文\n\x1b]52;c;payload\x07"}
        )

    monkeypatch.setattr(Client, "request", request)
    assert main(["onboarding", "review", "profile", "--id", "demo", "--version", "1"]) == 0
    output = capsys.readouterr().out
    assert "# 全文" in output and "\x1b" not in output and "\x07" not in output
    assert r"\u001b" in output and r"\u0007" in output
