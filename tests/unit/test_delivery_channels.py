from pathlib import Path

import pytest
from pydantic import ValidationError

from papersteward.config import Settings
from papersteward.delivery.channels import send_smtp
from papersteward.delivery.contracts import Digest


def test_smtp_tls_auth_and_message_are_offline(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, **kwargs):
            calls.append("tls")

        def login(self, username, password):
            calls.append((username, password))

        def send_message(self, message):
            calls.append(message)

    monkeypatch.setattr("papersteward.delivery.channels.smtplib.SMTP", FakeSMTP)
    digest = Digest.model_validate_json(Path("tests/fixtures/digest/basic.json").read_text())
    settings = Settings(
        _env_file=None,
        smtp_host="example.org",
        smtp_from="a@example.org",
        smtp_to="b@example.org",
        smtp_username="user",
        smtp_password="fixture-password",
        smtp_security="starttls",
    )
    send_smtp(digest, settings)
    assert calls[1:4] == ["ehlo", "tls", "ehlo"]
    assert calls[-1]["Message-ID"] == "<papersteward-2026-10-22@localhost>"
    assert calls[-1].is_multipart()


def test_smtp_configuration_rejects_header_injection_and_nonfinite_timeout():
    for values in ({"smtp_to": "x\nBcc:y"}, {"smtp_timeout_s": float("nan")}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)
