import json
from pathlib import Path

from sqlalchemy import func, select
from test_api_dry_run import client as client
from test_pipeline_ingest import activate

from papersteward.cli.main import main
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import RunRow

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/sources/basic.json"


def _openalex_work(index: int) -> dict:
    return {
        "id": f"https://openalex.org/W{1000000 + index}",
        "title": f"Synthetic pagination {index}",
        "doi": f"https://doi.org/10.0000/page-{index}",
        "publication_date": "2026-10-01",
        "authorships": [{"author": {"display_name": "Fixture Author"}}],
        "primary_location": {
            "source": {"display_name": "Fixture Venue"},
            "landing_page_url": f"https://example.invalid/page-{index}",
        },
        "open_access": {"is_oa": True},
        "type": "article",
        "language": "en",
    }


def test_cli_fixture_serves_multiple_pages_in_order(
    client, migrated_engine, monkeypatch, tmp_path, capsys
):
    activate(client, quiet_start=None, quiet_end=None)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    works = [_openalex_work(index) for index in range(101)]
    fixture = tmp_path / "openalex-pages.json"
    fixture.write_text(
        json.dumps(
            {
                "responses": [
                    {"meta": {"count": 101}, "results": works[:100]},
                    {"meta": {"count": 101}, "results": works[100:]},
                ]
            }
        )
    )
    assert (
        main(
            [
                "ingest",
                "--source",
                "openalex",
                "--fixture",
                str(fixture),
                "--query",
                "retrieval",
                "--max-results",
                "101",
                "--run-id",
                "multi-page",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["id"] == "multi-page" and report["status"] == "succeeded"
    assert report["stats_json"]["candidates"] == 101
    assert report["stats_json"]["deduplicated"] == 0
    assert report["stats_json"]["new"] == 101
    assert report["stats_json"]["sources"][0]["requests"] == 2


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
