from __future__ import annotations

from types import SimpleNamespace

import pytest

from papersteward.cli.main import main
from papersteward.cli.processing import run
from papersteward.config import get_settings
from papersteward.llm.errors import LLMProviderError


@pytest.fixture(autouse=True)
def _clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_process_live_with_fake_provider_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "fake")
    code = main(["process", "--paper", "p1", "--live"])
    captured = capsys.readouterr()
    assert code == 1
    assert "provider=fake" in captured.err
    assert "Traceback" not in captured.err


def test_process_batch_with_single_fixture_is_rejected(tmp_path, capsys):
    code = main(["process", "--llm-fixture", str(tmp_path / "fixture.json")])
    captured = capsys.readouterr()
    assert code == 1
    assert "只支持 --paper" in captured.err
    assert "Traceback" not in captured.err


def test_llm_provider_errors_are_reported_without_traceback(monkeypatch, capsys):
    def fail(args):
        raise LLMProviderError("upstream unavailable", retryable=False)

    monkeypatch.setattr("papersteward.cli.processing._run_process", fail)
    code = run(SimpleNamespace(command="process"))
    captured = capsys.readouterr()
    assert code == 1
    assert "upstream unavailable" in captured.err
    assert "Traceback" not in captured.err
