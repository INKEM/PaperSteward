"""真实 Uvicorn 的 G1–G4；执行边界调用真实仓储，绝不把 404 视为门禁通过。"""

import json
import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.engine import Engine
from test_walking_skeleton import BIN, ROOT, _free_port, _run_command, _wait_for_health

from papersteward.core.entities import Paper
from papersteward.core.enums import PaperStatus
from papersteward.core.errors import InvariantViolation
from papersteward.storage import create_db_engine, create_session_factory, session_scope
from papersteward.storage.models import PaperRow
from papersteward.storage.repositories import EventRepository, PaperRepository

pytestmark = pytest.mark.e2e
FIXTURES = ROOT / "tests/fixtures"
NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[httpx.Client, Engine]]:
    database = tmp_path / "onboarding.db"
    env = {
        **os.environ,
        "PAPERSTEWARD_DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "PAPERSTEWARD_DATA_DIR": str(tmp_path / "data"),
        "PAPERSTEWARD_LOG_LEVEL": "WARNING",
    }
    _run_command([str(BIN / "alembic"), "upgrade", "head"], env)
    engine = create_db_engine(env["PAPERSTEWARD_DATABASE_URL"])
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    logs = tmp_path / "uvicorn.log"
    with logs.open("wb") as output:
        process = subprocess.Popen(
            [
                str(BIN / "uvicorn"),
                "papersteward.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(f"{base}/health", process, logs)
            with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
                yield client, engine
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            engine.dispose()


def post(client, path, data=None, status=200):
    response = client.post(path, json=data, headers={"X-Request-ID": "onboarding-e2e"})
    assert response.status_code == status, (path, response.status_code, response.text)
    assert response.headers["X-Request-ID"] == "onboarding-e2e"
    return response.json()


def assert_execution_closed(engine, stage):
    with session_scope(create_session_factory(engine)) as session:
        for key, target in [
            ("enqueue-probe", PaperStatus.QUEUED),
            ("push-probe", PaperStatus.PUSHED),
            ("archive-probe", PaperStatus.ARCHIVED),
        ]:
            with pytest.raises(InvariantViolation, match="G4") as error:
                PaperRepository(session).transition(
                    key, target, event_id=f"probe-{stage}-{target}", occurred_at=NOW
                )
            assert error.value.field == "onboarding.g4"
            assert stage in str(error.value)
            assert EventRepository(session).get(f"probe-{stage}-{target}") is None


def test_g1_to_g4_fixture_and_execution_gates(server):
    client, engine = server
    factory = create_session_factory(engine)
    # 模拟已有未读/已读数据，隔离门禁与生产采集/评分算法。只在临时测试库设状态。
    with session_scope(factory) as session:
        for key, status in [
            ("enqueue-probe", PaperStatus.SCORED),
            ("push-probe", PaperStatus.QUEUED),
            ("archive-probe", PaperStatus.DONE),
        ]:
            PaperRepository(session).add(Paper(id=key, title=key, first_seen_at=NOW))
            session.get(PaperRow, key).status = status.value
    assert_execution_closed(engine, "seeds")
    dry = json.loads((FIXTURES / "dry_run/basic.json").read_text())
    post(client, "/onboarding/dry-runs", dry, status=409)
    post(client, "/onboarding/start")
    assert_execution_closed(engine, "profile")
    profile = post(
        client,
        "/onboarding/profile/drafts",
        {"profile_id": "demo-profile", "content_md": (FIXTURES / "profiles/basic.md").read_text()},
        201,
    )
    post(
        client,
        "/onboarding/profile/demo-profile/1.0/confirm",
        {
            "confirmed": True,
            "confirmed_by": "fixture-user",
            "change_summary": "确认合成档案",
            "expected_revision": 1,
            "content_hash": "0" * 64,
        },
        409,
    )
    assert client.get("/onboarding").json()["stage"] == "profile"
    post(
        client,
        "/onboarding/profile/demo-profile/1.0/confirm",
        {
            "confirmed": True,
            "confirmed_by": "fixture-user",
            "change_summary": "确认合成档案",
            "expected_revision": 1,
            "content_hash": profile["content_hash"],
        },
    )
    assert_execution_closed(engine, "taxonomy")
    taxonomy = post(
        client,
        "/onboarding/taxonomy/drafts",
        {
            "taxonomy_id": "onboarding-taxonomy",
            "content_md": (FIXTURES / "taxonomies/onboarding.md").read_text(),
            "sample_kind": "synthetic",
            "seeds": [
                {
                    "paper_id": f"syn-T{tag:02}-{i}",
                    "title": f"合成 {tag}/{i}",
                    "primary_tag": f"T{tag:02}",
                }
                for tag in range(1, 6)
                for i in range(1, 5)
            ],
        },
        201,
    )
    post(
        client,
        "/onboarding/taxonomy/onboarding-taxonomy/1.0/confirm",
        {
            "confirmed": True,
            "confirmed_by": "fixture-user",
            "change_summary": "确认分类法",
            "expected_revision": 2,
            "content_hash": taxonomy["content_hash"],
            "review_hash": taxonomy["review_hash"],
        },
    )
    assert_execution_closed(engine, "calibration")
    post(client, "/onboarding/seeds/import")
    calibration = json.loads((FIXTURES / "calibration/basic.json").read_text())
    request = {
        key: calibration[key] for key in ("round_number", "papers", "sample_kind", "rubric_version")
    }
    post(client, "/onboarding/calibration/rounds", request, 201)
    partial = post(client, "/onboarding/calibration/1/agent-labels", calibration["agent_batch"])
    assert partial["report"] is None
    view = post(client, "/onboarding/calibration/1/user-labels", calibration["user_batch"])
    post(
        client,
        "/onboarding/calibration/1/confirm",
        {
            "confirmed": True,
            "confirmed_by": "fixture-user",
            "change_summary": "认可合成校准报告",
            "expected_revision": 3,
            "report_hash": view["report_hash"],
        },
    )
    assert_execution_closed(engine, "dry_run")
    preview = post(client, "/onboarding/dry-runs", dry, 201)
    assert preview["report"]["sample_notice"] == "仅流程验证"
    assert "太多 / 太少 / 不准 / 分类不对" in preview["report"]["markdown"]
    approval = {
        "confirmed": True,
        "confirmed_by": "fixture-user",
        "change_summary": "认可参数和日报",
        "expected_revision": 4,
        "report_hash": preview["report_hash"],
        "feedback_hash": "0" * 64,
    }
    post(client, "/onboarding/dry-runs/1/confirm", approval, 409)
    rejected = post(
        client,
        "/onboarding/dry-runs/1/feedback",
        {
            "report_hash": preview["report_hash"],
            "reviewed_by": "fixture-user",
            "accepted": False,
            "issues": ["too_many"],
        },
    )
    post(
        client,
        "/onboarding/dry-runs/1/confirm",
        {**approval, "feedback_hash": rejected["feedback_hash"]},
        409,
    )
    assert_execution_closed(engine, "dry_run")
    dry["version"] = 2
    dry["parameters"]["push_time"] = "09:00"
    dry["items"].pop()
    preview = post(client, "/onboarding/dry-runs", dry, 201)
    accepted = post(
        client,
        "/onboarding/dry-runs/2/feedback",
        {
            "report_hash": preview["report_hash"],
            "reviewed_by": "fixture-user",
            "accepted": True,
            "issues": [],
        },
    )
    state = post(
        client,
        "/onboarding/dry-runs/2/confirm",
        {
            **approval,
            "report_hash": preview["report_hash"],
            "feedback_hash": accepted["feedback_hash"],
        },
    )
    assert state == {"stage": "active", "revision": 5, "next_gate": None}
    assert client.get("/onboarding").json() == state
    assert client.get("/onboarding/dry-runs/2").json() == accepted
    with factory() as session:
        at = EventRepository(session).recent(limit=1)[0].created_at
        for key, target in [
            ("enqueue-probe", PaperStatus.QUEUED),
            ("push-probe", PaperStatus.PUSHED),
            ("archive-probe", PaperStatus.ARCHIVED),
        ]:
            assert (
                PaperRepository(session)
                .transition(key, target, event_id=f"probe-active-{target}", occurred_at=at)
                .status
                is target
            )
        session.rollback()  # 验证放行不产生真实发送或永久测试归档。
    with session_scope(factory) as session:
        assert PaperRepository(session).get("push-probe").status is PaperStatus.QUEUED
        assert all(
            p.status is PaperStatus.DISCOVERED
            for p in PaperRepository(session).list()
            if p.id.startswith(("cal-", "syn-"))
        )
