"""真实 papersteward 子进程 + Uvicorn，全部使用合成 fixture/临时库。"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_onboarding_gates import server as server
from test_walking_skeleton import BIN, ROOT

from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import ArchiveSnapshotRow, ConfigRow, RatingRow, ScoreSnapshotRow

pytestmark = pytest.mark.e2e
FIXTURES = ROOT / "tests/fixtures"


def test_cli_g1_to_g4_and_stale_review(server, tmp_path: Path):
    client, engine = server
    env = {
        **os.environ,
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
    }

    def cli(*args, expected=0):
        result = subprocess.run(
            [
                str(BIN / "papersteward"),
                "onboarding",
                "--url",
                str(client.base_url).rstrip("/"),
                "--json",
                *map(str, args),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == expected, (args, result.stdout, result.stderr)
        return json.loads(result.stdout) if expected == 0 else result.stderr

    def confirm(gate, review, expected=0):
        return cli(
            "confirm",
            "--gate",
            gate,
            "--review",
            review,
            "--by",
            "fixture-user",
            "--summary",
            "审阅合成流程验证",
            "--yes",
            expected=expected,
        )

    assert cli("status")["stage"] == "seeds"
    cli("start")
    profile = tmp_path / "profile.json"
    cli(
        "submit",
        "profile",
        "--id",
        "demo-profile",
        "--file",
        FIXTURES / "profiles/basic.md",
        "--out",
        profile,
    )
    assert "required" in cli(
        "confirm",
        "--gate",
        "g1",
        "--review",
        profile,
        "--by",
        "user",
        "--summary",
        "review",
        expected=2,
    )
    assert cli("status")["stage"] == "profile"
    assert confirm("g1", profile)["stage"] == "taxonomy"
    assert "HTTP 409" in confirm("g1", profile, expected=1)
    seeds = tmp_path / "seeds.json"
    seeds.write_text(
        json.dumps(
            [
                {
                    "paper_id": f"syn-T{tag:02}-{i}",
                    "title": f"合成 {tag}/{i}",
                    "primary_tag": f"T{tag:02}",
                }
                for tag in range(1, 6)
                for i in range(1, 5)
            ]
        )
    )
    taxonomy = tmp_path / "taxonomy.json"
    cli(
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
    confirm("g2", taxonomy)
    cli("import-seeds")
    fixture = FIXTURES / "calibration/basic.json"
    cli("submit", "calibration", "--file", fixture)
    assert cli("label", "agent", "--round", "1", "--file", fixture)["data"]["report"] is None
    calibration = tmp_path / "calibration.json"
    cli("label", "user", "--round", "1", "--file", fixture, "--out", calibration)
    assert confirm("g3", calibration)["stage"] == "dry_run"
    dry = tmp_path / "dry.json"
    cli("submit", "dry-run", "--file", FIXTURES / "dry_run/basic.json", "--out", dry)
    assert "仅流程验证" in json.loads(dry.read_text())["data"]["report"]["markdown"]
    rejected = tmp_path / "rejected.json"
    cli(
        "feedback",
        "--review",
        dry,
        "--by",
        "fixture-user",
        "--issue",
        "inaccurate",
        "--out",
        rejected,
    )
    assert "HTTP 409" in confirm("g4", rejected, expected=1)
    accepted = tmp_path / "accepted.json"
    cli("feedback", "--review", rejected, "--by", "fixture-user", "--accept", "--out", accepted)
    assert confirm("g4", accepted) == {"stage": "active", "revision": 5, "next_gate": None}
    assert cli("status")["stage"] == "active"
    with session_scope(create_session_factory(engine)) as session:
        assert session.scalar(select(func.count()).select_from(ConfigRow)) == 2
        for table in (RatingRow, ScoreSnapshotRow, ArchiveSnapshotRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0
