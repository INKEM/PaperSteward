import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_api_onboarding_taxonomy import NOW, payload

from papersteward.api import create_app
from papersteward.cli.main import main
from papersteward.cli.onboarding_client import Client, ClientError
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import ArchiveSnapshotRow, ConfigRow, RatingRow, ScoreSnapshotRow
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ProfileRepository

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def cli(migrated_engine, monkeypatch, capsys):
    monkeypatch.setattr("papersteward.api.onboarding.utc_now", lambda: NOW)
    with TestClient(create_app(engine=migrated_engine)) as api:
        calls = []

        def request(self, method, path, body=None):
            calls.append((method, path, body))
            response = api.request(method, path, json=body)
            if response.status_code >= 400:
                raise ClientError(
                    f"HTTP {response.status_code}: " + response.json()["error"]["message"]
                )
            return response.json()

        monkeypatch.setattr(Client, "request", request)

        def invoke(*args, ok=True):
            capsys.readouterr()
            result = main(["onboarding", "--json", *map(str, args)])
            output = capsys.readouterr()
            if ok:
                assert result == 0, output.err
            else:
                assert result == 1
            return json.loads(output.out) if result == 0 else output.err

        yield invoke, calls


def confirm(cli, gate, path):
    return cli(
        "confirm",
        "--gate",
        gate,
        "--review",
        path,
        "--by",
        "fixture-user",
        "--summary",
        "已审阅并确认",
        "--yes",
    )


def test_cli_full_workflow_and_confirmation_never_refreshes(cli, tmp_path, migrated_engine):
    invoke, calls = cli
    invoke("start")
    profile = tmp_path / "profile.json"
    invoke(
        "submit",
        "profile",
        "--id",
        "demo-profile",
        "--file",
        FIXTURES / "profiles/basic.md",
        "--out",
        profile,
    )
    calls.clear()
    assert confirm(invoke, "g1", profile)["stage"] == "taxonomy"
    assert len(calls) == 1 and calls[0][0] == "POST"
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps(payload()["seeds"]))
    taxonomy = tmp_path / "taxonomy.json"
    invoke(
        "submit",
        "taxonomy",
        "--id",
        "onboarding-taxonomy",
        "--file",
        FIXTURES / "taxonomies/onboarding.md",
        "--seeds",
        seeds,
        "--sample-kind",
        "synthetic",
        "--out",
        taxonomy,
    )
    confirm(invoke, "g2", taxonomy)
    assert invoke("import-seeds")["created"] == 20
    fixture = FIXTURES / "calibration/basic.json"
    invoke("submit", "calibration", "--file", fixture)
    assert set(calls[-1][2]) == {"papers", "round_number", "sample_kind", "rubric_version"}
    partial = invoke("label", "agent", "--round", "1", "--file", fixture)
    assert partial["data"]["report"] is None
    calibration = tmp_path / "calibration.json"
    invoke("label", "user", "--round", "1", "--file", fixture, "--out", calibration)
    confirm(invoke, "g3", calibration)
    dry = tmp_path / "dry.json"
    invoke("submit", "dry-run", "--file", FIXTURES / "dry_run/basic.json", "--out", dry)
    rejected = tmp_path / "rejected.json"
    invoke(
        "feedback",
        "--review",
        dry,
        "--by",
        "fixture-user",
        "--issue",
        "too_many",
        "--out",
        rejected,
    )
    assert "409" in invoke(
        "confirm",
        "--gate",
        "g4",
        "--review",
        rejected,
        "--by",
        "user",
        "--summary",
        "尚未认可",
        "--yes",
        ok=False,
    )
    accepted = tmp_path / "accepted.json"
    invoke("feedback", "--review", rejected, "--by", "fixture-user", "--accept", "--out", accepted)
    assert confirm(invoke, "g4", accepted)["stage"] == "active"
    assert invoke("status")["revision"] == 5
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert get_onboarding(session).revision == 5
        assert session.scalar(select(func.count()).select_from(ConfigRow)) == 2
        for table in (RatingRow, ScoreSnapshotRow, ArchiveSnapshotRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0


def test_cli_edit_review_diff_and_save_failure_recovery(cli, tmp_path, migrated_engine):
    invoke, calls = cli
    invoke("start")
    missing = tmp_path / "missing" / "review.json"
    error = invoke(
        "submit",
        "profile",
        "--id",
        "demo-profile",
        "--file",
        FIXTURES / "profiles/basic.md",
        "--out",
        missing,
        ok=False,
    )
    assert "操作已完成" in error and "勿重复提交" in error
    assert len([c for c in calls if c[0] == "POST" and c[1].endswith("/drafts")]) == 1
    recovered = tmp_path / "recovered.json"
    invoke("review", "profile", "--id", "demo-profile", "--version", "1.0", "--out", recovered)
    invoke(
        "submit",
        "profile",
        "--id",
        "demo-profile",
        "--base-version",
        "1.0",
        "--file",
        FIXTURES / "profiles/v2.md",
    )
    view = invoke(
        "review", "profile", "--id", "demo-profile", "--version", "1.1", "--base-version", "1.0"
    )
    assert view["data"]["diff"] and view["data"]["markdown_diff"]
    with session_scope(create_session_factory(migrated_engine)) as session:
        assert len(ProfileRepository(session).list("demo-profile")) == 2
        assert get_onboarding(session).revision == 1


@pytest.mark.parametrize(
    "kind", ["tamper", "server", "gate", "existing", "missing", "bad_json", "wrong_kind", "bias"]
)
def test_cli_failures_do_not_send_mutations(cli, tmp_path, kind):
    invoke, calls = cli
    invoke("start")
    review = tmp_path / "review.json"
    invoke(
        "submit",
        "profile",
        "--id",
        "demo-profile",
        "--file",
        FIXTURES / "profiles/basic.md",
        "--out",
        review,
    )
    calls.clear()
    if kind == "tamper":
        data = json.loads(review.read_text())
        data["data"]["content_md"] = "改过的正文"
        review.write_text(json.dumps(data))
        error = invoke(
            "confirm",
            "--gate",
            "g1",
            "--review",
            review,
            "--by",
            "user",
            "--summary",
            "review",
            "--yes",
            ok=False,
        )
        assert "已被编辑" in error
    elif kind == "server":
        error = invoke(
            "--url",
            "http://localhost:8001",
            "confirm",
            "--gate",
            "g1",
            "--review",
            review,
            "--by",
            "user",
            "--summary",
            "review",
            "--yes",
            ok=False,
        )
        assert "另一 API" in error
    elif kind == "gate":
        error = invoke(
            "confirm",
            "--gate",
            "g4",
            "--review",
            review,
            "--by",
            "user",
            "--summary",
            "review",
            "--yes",
            ok=False,
        )
        assert "不匹配" in error
    elif kind == "existing":
        error = invoke(
            "submit",
            "profile",
            "--id",
            "demo-profile",
            "--file",
            FIXTURES / "profiles/v2.md",
            "--out",
            review,
            ok=False,
        )
        assert "文件已存在" in error
    elif kind == "missing":
        invoke(
            "submit", "profile", "--id", "demo-profile", "--file", tmp_path / "no-file", ok=False
        )
    elif kind == "bad_json":
        file = tmp_path / "bad.json"
        file.write_text("{bad json")
        invoke("submit", "calibration", "--file", file, ok=False)
    elif kind == "wrong_kind":
        invoke("feedback", "--review", review, "--by", "user", "--accept", ok=False)
    else:
        invoke(
            "confirm",
            "--gate",
            "g1",
            "--review",
            review,
            "--by",
            "user",
            "--summary",
            "review",
            "--yes",
            "--accept-deviations",
            ok=False,
        )
    assert not [c for c in calls if c[0] == "POST"]


def test_cli_stale_confirmation_is_rejected_by_api(cli, tmp_path):
    invoke, calls = cli
    invoke("start")
    review = tmp_path / "profile.json"
    invoke(
        "submit",
        "profile",
        "--id",
        "demo-profile",
        "--file",
        FIXTURES / "profiles/basic.md",
        "--out",
        review,
    )
    confirm(invoke, "g1", review)
    calls.clear()
    assert "409" in invoke(
        "confirm",
        "--gate",
        "g1",
        "--review",
        review,
        "--by",
        "user",
        "--summary",
        "重复确认",
        "--yes",
        ok=False,
    )
    assert len(calls) == 1 and calls[0][0] == "POST"
