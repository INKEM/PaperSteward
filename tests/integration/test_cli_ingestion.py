from pathlib import Path

from sqlalchemy import func, select

from papersteward.cli.main import main
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import RunRow

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/sources/basic.json"


def test_cli_preflight_and_fixture_do_not_bypass_g4(migrated_engine, monkeypatch, capsys):
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    assert main(["ingest", "--source", "arxiv"]) == 1
    assert "--live" in capsys.readouterr().err
    assert main(["ingest", "--source", "fake", "--fixture", str(FIXTURE)]) == 1
    assert "g4" in capsys.readouterr().err
    assert main(["runs", "show", "--last"]) == 1
    assert "未找到" in capsys.readouterr().err
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert session.scalar(select(func.count()).select_from(RunRow)) == 0
