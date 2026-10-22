"""真实 papersteward 子进程 + Uvicorn，全部使用合成 fixture/临时库。"""

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_onboarding_gates import server as server
from test_walking_skeleton import BIN, ROOT

from papersteward.core.entities import Enrichment, EnrichmentSignals
from papersteward.core.enums import OaStatus
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    ConfigRow,
    RatingRow,
    ReviewItemRow,
    ScoreSnapshotRow,
    StaticScoreSnapshotRow,
)
from papersteward.storage.repositories import (
    ConfigRepository,
    EnrichmentRepository,
    ScoringChangeRepository,
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

    # P6：真实HTMX表单协议→日报→完成/评分/备注→冻结归档→CLI导出。
    headers = {"HX-Request": "true", "X-PaperSteward-UI": "1"}
    assert "生成今日日报" in client.get("/ui/today").text
    generated = client.post("/ui/digests", headers=headers)
    assert generated.status_code == 200 and generated.headers["HX-Redirect"] == "/ui/today"
    today = client.get("/ui/today")
    assert "Synthetic Retrieval Study" in today.text and 'hx-post="/ui/actions/' in today.text
    assert "default-src 'self'" in today.headers["Content-Security-Policy"]
    assert client.get("/ui/static/htmx.js").status_code == 200
    assert "评分解释" in client.get(f"/ui/papers/{paper['id']}").text
    request_id = re.search(r'name="request_id" value="([a-f0-9]+)"', today.text)[1]
    form = {
        "kind": "done",
        "mode": "deep",
        "score": "4",
        "note": "闭环备注",
        "request_id": request_id,
    }
    done = client.post(f"/ui/actions/{paper['id']}", data=form, headers=headers)
    assert done.status_code == 200 and "archived" in done.text
    assert client.post(f"/ui/actions/{paper['id']}", data=form, headers=headers).text == done.text
    record = client.get(f"/archive/{paper['id']}").json()
    assert record["score"] == 4 and record["note"] == "闭环备注"
    assert record["content"]["score"]["score_date"] == "2026-11-01"
    assert record["content"]["metadata"]["title"] == "Synthetic Retrieval Study"
    assert "闭环备注" in client.get("/ui/archive").text
    exported = tmp_path / "archive.bib"
    export = subprocess.run(
        [
            str(BIN / "papersteward"),
            "archive",
            "export",
            "--format",
            "bibtex",
            "--out",
            str(exported),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert export.returncode == 0, export.stderr
    assert "闭环备注" in exported.read_text()
    with session_scope(create_session_factory(engine)) as session:
        assert session.scalar(select(func.count()).select_from(ArchiveSnapshotRow)) == 1

    # P7-S6：真实 CLI/HTTP 串起特殊桶、自定义视图与全局权重治理。
    def run_cli(*args, expected: int = 0, env_override: dict[str, str] | None = None):
        result = subprocess.run(
            [str(BIN / "papersteward"), *map(str, args)],
            cwd=ROOT,
            env=env_override or env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == expected, (args, result.stdout, result.stderr)
        return json.loads(result.stdout) if expected == 0 else result.stderr

    report = run_cli(
        "ingest",
        "--source",
        "fake",
        "--fixture",
        FIXTURES / "sources/p7.json",
        "--query",
        "retrieval",
        "--run-id",
        "p7-ingest",
        "--json",
    )
    assert report["stats_json"]["new"] == 5, report
    indexed = {
        item["title"]: item["id"] for item in client.get("/papers?limit=200").json()["items"]
    }
    high_r = indexed["P7 Topic High Relevance"]
    high_e = indexed["P7 Topic High Extra"]
    review_paper = indexed["P7 Special Shelf One"]
    distant_paper = indexed["P7 Special Shelf Two"]
    classic_paper = indexed["P7 Special Shelf Three"]

    with session_scope(create_session_factory(engine)) as session:
        EnrichmentRepository(session).save(
            Enrichment(
                paper_id=high_e,
                signals=EnrichmentSignals(
                    venue_level="a*",
                    citation_count=1000,
                    code_url="https://example.invalid/p7/code",
                    data_url="https://example.invalid/p7/data",
                    oa_status=OaStatus.OPEN,
                    confidence=0.9,
                    source="fixture",
                ),
                created_at=datetime(2026, 11, 1, 8, tzinfo=UTC),
                updated_at=datetime(2026, 11, 1, 8, tzinfo=UTC),
            )
        )

    for title, fixture in (
        ("P7 Topic High Relevance", "p7_topic_a.json"),
        ("P7 Topic High Extra", "p7_topic_b.json"),
        ("P7 Special Shelf One", "p7_special.json"),
        ("P7 Special Shelf Two", "p7_special.json"),
        ("P7 Special Shelf Three", "p7_special.json"),
    ):
        outcomes = run_cli(
            "process",
            "--paper",
            indexed[title],
            "--llm-fixture",
            FIXTURES / "process" / fixture,
            "--json",
        )
        outcome = outcomes[0]
        assert outcome["status"] == "classified", outcome
        assert outcome["needs_review"] is False, outcome

    pending = run_cli("shelf", "review", "--json")
    assert pending["total"] == 9, pending
    for paper_id, kind in (
        (review_paper, "review"),
        (distant_paper, "distant"),
        (classic_paper, "classic"),
    ):
        accepted = run_cli(
            "shelf",
            "accept",
            paper_id,
            "--kind",
            kind,
            "--by",
            "fixture-user",
            "--note",
            "P7-S6 e2e",
            "--json",
        )
        assert accepted["tier"] is not None, accepted

    shelves = client.get("/shelves").json()
    assert [item["total"] for item in shelves["items"]] == [1, 1, 1], shelves
    distant = client.get("/shelves/distant").json()["items"][0]
    assert distant["bridge_memo"]["source_mechanism"]
    assert "value" not in distant and "rank_score" not in distant
    classic = client.get("/shelves/classic").json()["items"][0]
    assert classic["classic_basis"] == ["foundational"]
    assert "桥接假设" in client.get("/ui/shelves").text
    distant_text = subprocess.run(
        [str(BIN / "papersteward"), "shelf", "distant", "--explain"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert distant_text.returncode == 0, distant_text.stderr
    assert "桥接假设" in distant_text.stdout
    plan = run_cli("shelf", "plan", "--date", score_date, "--json")
    assert len(plan["slots"]) <= 2

    snapshot = run_cli("scores", "snapshot", "--date", score_date, "--json")
    assert snapshot["created"] == 2, snapshot
    topic_queue = client.get("/queue", params={"date": score_date}).json()
    assert topic_queue["total"] == 2, topic_queue
    queued_ids = {item["paper_id"] for item in topic_queue["items"]}
    assert queued_ids == {high_r, high_e}

    preset = run_cli("view", "preview", "--preset", "reproducible", "--date", score_date, "--json")
    assert preset["view"]["name"] == "reproducible"
    assert "P_view" in preset["formula"]
    reorder = run_cli("view", "preview", "--weights", "R:100", "--date", score_date, "--json")
    assert reorder["items"][0]["paper_id"] == high_r, reorder
    assert reorder["changed"] >= 1
    run_cli(
        "view",
        "save",
        "--name",
        "s6-r",
        "--weights",
        "R:100",
        "--json",
    )
    loaded = run_cli("view", "preview", "--name", "s6-r", "--date", score_date, "--json")
    assert loaded["items"][0]["paper_id"] == high_r
    assert "P_view" in client.get(f"/ui/views?date={score_date}").text

    promoted = run_cli(
        "shelf",
        "promote",
        review_paper,
        "--by",
        "fixture-user",
        "--note",
        "P7-S6 提升回主题桶",
        "--json",
    )
    assert promoted["paper_status"] == "classified", promoted
    assert promoted["bucket_id"] == "b-t02"
    assert promoted["score_date"] == score_date
    assert client.get("/shelves/review").json()["total"] == 0

    change = run_cli(
        "config",
        "preview",
        "--set",
        "static.relevance=0.80",
        "--set",
        "static.authority=0.10",
        "--set",
        "static.extra_value=0.10",
        "--date",
        score_date,
        "--effective-on",
        "2026-11-02",
        "--json",
    )
    assert change["status"] == "previewed"
    assert change["preview"]["items"][0]["paper_id"] == high_r
    with session_scope(create_session_factory(engine)) as session:
        assert ConfigRepository(session).get("scoring", "1.0.1") is None
    confirmed = run_cli(
        "config",
        "confirm",
        change["id"],
        "--by",
        "fixture-user",
        "--note",
        "P7-S6 e2e",
        "--json",
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["static_recomputed"] == 5, confirmed
    with session_scope(create_session_factory(engine)) as session:
        assert ConfigRepository(session).get("scoring", "1.0.1") is not None
        activation = ScoringChangeRepository(session).active_for(
            datetime(2026, 11, 2, tzinfo=UTC).date()
        )
        assert activation is not None and activation.id == change["id"]
    assert run_cli("config", "changes", "--json")["items"][0]["status"] == "confirmed"

    effective_clock = tmp_path / "effective-clock"
    effective_clock.mkdir()
    (effective_clock / "sitecustomize.py").write_text(
        "from datetime import UTC, datetime\n"
        "import papersteward.core.entities as entities\n"
        "entities.utc_now = lambda: datetime(2026, 11, 2, 8, tzinfo=UTC)\n"
    )
    effective_env = {**env, "PYTHONPATH": str(effective_clock)}
    assert (
        run_cli(
            "scores",
            "snapshot",
            "--date",
            "2026-11-02",
            "--json",
            env_override=effective_env,
        )["created"]
        == 3
    )
    new_queue = client.get("/queue", params={"date": "2026-11-02"}).json()
    assert new_queue["total"] == 3
    assert new_queue["items"][0]["paper_id"] == high_r, new_queue
    assert {item["paper_id"] for item in new_queue["items"]} == {
        high_r,
        high_e,
        review_paper,
    }
    old_queue = client.get("/queue", params={"date": score_date}).json()
    assert old_queue["total"] == 2
    assert old_queue["items"][0]["paper_id"] == high_e, old_queue
