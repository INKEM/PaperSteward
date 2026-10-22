from __future__ import annotations

import json

import pytest

from papersteward.cli.main import main
from papersteward.llm import FakeLLM


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


def test_llm_ping_requires_fake_or_live(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["llm", "ping"])
    captured = capsys.readouterr()

    assert code == 1
    assert "--fake" in captured.err
    assert "--live" in captured.err


def test_llm_ping_rejects_both_modes(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["llm", "ping", "--fake", "--live"])
    captured = capsys.readouterr()

    assert code == 1
    assert "之一" in captured.err


def test_llm_ping_fake_works_without_live_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "openai_compatible")

    code = main(["llm", "ping", "--fake"])
    captured = capsys.readouterr()

    assert code == 0
    assert "OK llm fake-llm: pong" in captured.out


def test_llm_ping_live_requires_non_fake_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "fake")

    code = main(["llm", "ping", "--live"])
    captured = capsys.readouterr()

    assert code == 1
    assert "provider=fake" in captured.err


def test_llm_ping_live_reports_missing_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "openai_compatible")

    code = main(["llm", "ping", "--live"])
    captured = capsys.readouterr()

    assert code == 1
    assert "PAPERSTEWARD_LLM_MODEL" in captured.err


def test_llm_ping_live_uses_configured_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MODEL", "test-model")
    provider = FakeLLM(["pong"], model_id="test-model")
    monkeypatch.setattr(
        "papersteward.cli.llm.build_llm_provider",
        lambda settings, *, live: provider,
    )

    code = main(["llm", "ping", "--live", "--json"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["provider"] == "openai_compatible"
    assert payload["model_id"] == "test-model"
    assert payload["text"] == "pong"
