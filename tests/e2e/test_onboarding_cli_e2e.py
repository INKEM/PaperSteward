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
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    ConfigRow,
    RatingRow,
    ReviewItemRow,
    ScoreSnapshotRow,
    StaticScoreSnapshotRow,
)

pytestmark = pytest.mark.e2e
FIXTURES = ROOT / "tests/fixtures"


def test_cli_g1_to_g4_and_stale_review(server, tmp_path: Path):
    client, engine = server
    env = {
        **os.environ,
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
        "PAPERSTEWARD_DATABASE_URL": str(engine.url),
        "PAPERSTEWARD_DATA_DIR": str(tmp_path),
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

    closed = client.get("/queue?date=2026-10-22")
    assert closed.status_code == 409 and "G4" in closed.json()["error"]["message"]
    assert client.post("/scores/snapshot", json={"score_date": "2026-10-22"}).status_code == 409
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
    dry_input = tmp_path / "dry-input.json"
    dry_data = json.loads((FIXTURES / "dry_run/basic.json").read_text())
    dry_data["parameters"].update(quiet_start=None, quiet_end=None)
    dry_input.write_text(json.dumps(dry_data))
    cli("submit", "dry-run", "--file", dry_input, "--out", dry)
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
    reports = []
    for number in (1, 2):
        result = subprocess.run(
            [
                str(BIN / "papersteward"),
                "ingest",
                "--source",
                "arxiv",
                "--fixture",
                str(FIXTURES / "sources/arxiv.xml"),
                "--query",
                "retrieval",
                "--run-id",
                f"cli-ingest-{number}",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        reports.append(json.loads(result.stdout))
    assert [report["stats_json"]["new"] for report in reports] == [1, 0]
    assert reports[1]["stats_json"]["source_versions_added"] == 0
    result = subprocess.run(
        [str(BIN / "papersteward"), "runs", "show", "--last"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["id"] == "cli-ingest-2"
    papers = client.get("/papers?limit=200").json()["items"]
    paper = next(item for item in papers if item["title"] == "Synthetic Retrieval Study")
    process = subprocess.run(
        [
            str(BIN / "papersteward"),
            "process",
            "--paper",
            paper["id"],
            "--llm-fixture",
            str(FIXTURES / "process/e2e.json"),
            "--json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    outcome = json.loads(process.stdout)[0]
    assert outcome["status"] == "needs_review"
    assert outcome["needs_review"] is True
    assert "classification:low_primary_confidence" in outcome["reasons"]

    review_list = subprocess.run(
        [str(BIN / "papersteward"), "review", "list", "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert review_list.returncode == 0, (review_list.stdout, review_list.stderr)
    listing = json.loads(review_list.stdout)
    assert listing["total"] == 1 and listing["items"][0]["paper_id"] == paper["id"]

    approved = client.post(
        f"/review/{paper['id']}/approve",
        json={"reviewed_by": "fixture-user", "note": "端到端审核"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["paper_status"] == "classified"
    refreshed = {item["id"]: item for item in client.get("/papers?limit=200").json()["items"]}
    assert refreshed[paper["id"]]["status"] == "classified"
    assert client.get("/review").json()["total"] == 0
    with session_scope(create_session_factory(engine)) as session:
        assert session.scalar(select(func.count()).select_from(ConfigRow)) == 2
        for table in (RatingRow, ScoreSnapshotRow, ArchiveSnapshotRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0
        assert session.scalar(select(func.count()).select_from(StaticScoreSnapshotRow)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewItemRow)) == 1

    # P5：真实CLI生成日快照，HTTP读取队列/解释，重复生成不再次EMA。
    score_date = outcome["static"]["score_date"]
    snapshots = subprocess.run(
        [str(BIN / "papersteward"), "scores", "snapshot", "--date", score_date, "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert snapshots.returncode == 0, (snapshots.stdout, snapshots.stderr)
    assert json.loads(snapshots.stdout)["created"] == 1, snapshots.stdout
    queue = client.get("/queue", params={"date": score_date}).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["paper_id"] == paper["id"]
    assert item["explanation"]["scores"]["U"] == 50
    assert item["explanation"]["dynamic"]["hunger"] == 50
    assert item["explanation"]["components"]["R"]["confidence"] == 0.86
    assert (
        client.get(f"/scores/{paper['id']}/explain", params={"date": score_date}).json()
        == item["explanation"]
    )
    repeated = client.post("/scores/snapshot", json={"score_date": score_date})
    assert repeated.status_code == 200 and repeated.json()["existing"] == 1
    assert repeated.json()["created"] == 0
    assert client.get("/queue", params={"date": score_date}).json() == queue
    for args in (["queue", "--date", score_date], ["score", "explain", paper["id"]]):
        result = subprocess.run(
            [str(BIN / "papersteward"), *args, "--json"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)
    with session_scope(create_session_factory(engine)) as session:
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == 1
        assert session.scalar(select(func.count()).select_from(ArchiveSnapshotRow)) == 0
