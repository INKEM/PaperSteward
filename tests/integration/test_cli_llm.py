from __future__ import annotations

import json

import pytest

from papersteward.cli.main import main


def test_llm_ping_fake(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["llm", "ping", "--fake"])
    captured = capsys.readouterr()

    assert code == 0
    assert "OK llm fake-llm: pong" in captured.out


def test_llm_ping_fake_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["llm", "ping", "--fake", "--json"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["provider"] == "fake"
    assert payload["usage"]["calls"] == 1


def test_llm_ping_without_fake_explains_p1_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["llm", "ping"])
    captured = capsys.readouterr()

    assert code == 1
    assert "FakeLLM" in captured.err
    assert "--fake" in captured.err
