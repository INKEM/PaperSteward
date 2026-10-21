from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from papersteward.core.entities import Paper
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.fixtures import FakeSourceAdapter
from papersteward.storage import create_db_engine, create_session_factory, session_scope
from papersteward.storage.repositories import PaperRepository

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.executable).parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_command(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "命令失败："
            + " ".join(command)
            + f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _wait_for_health(
    url: str,
    process: subprocess.Popen[bytes],
    log_path: Path,
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"uvicorn 提前退出：\n{log_path.read_text(errors='replace')}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    pytest.fail(f"等待 /health 超时：\n{log_path.read_text(errors='replace')}")


@pytest.mark.e2e
def test_walking_skeleton(tmp_path: Path) -> None:
    database = tmp_path / "papersteward.db"
    env = os.environ.copy()
    env["PAPERSTEWARD_DATABASE_URL"] = f"sqlite+pysqlite:///{database}"
    env["PAPERSTEWARD_DATA_DIR"] = str(tmp_path / "data")
    env["PAPERSTEWARD_LOG_LEVEL"] = "WARNING"

    _run_command([str(BIN / "alembic"), "upgrade", "head"], env)
    _run_command([str(BIN / "papersteward"), "demo", "seed"], env)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "uvicorn.log"

    with log_path.open("wb") as log_file:
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
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(f"{base_url}/health", process, log_path)
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                health = json.load(response)
            with urllib.request.urlopen(f"{base_url}/papers", timeout=2) as response:
                papers = json.load(response)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    assert health["status"] == "ok"
    assert health["database"] == "ok"
    assert papers["total"] == 1
    assert papers["items"][0]["id"] == "p001"


@pytest.mark.e2e
def test_fake_source_candidate_to_papers_api(tmp_path: Path) -> None:
    """S1 兼容性演示：投影/写库仅在测试中完成，尚无生产 ingest 服务。"""
    env = os.environ.copy()
    env["PAPERSTEWARD_DATABASE_URL"] = f"sqlite+pysqlite:///{tmp_path / 'source.db'}"
    env["PAPERSTEWARD_DATA_DIR"] = str(tmp_path)
    _run_command([str(BIN / "alembic"), "upgrade", "head"], env)
    adapter = FakeSourceAdapter(ROOT / "tests/fixtures/sources/basic.json")
    candidate = adapter.search(QuerySpec(keywords=("retrieval",), max_results=1))[0]
    engine = create_db_engine(env["PAPERSTEWARD_DATABASE_URL"])
    try:
        with session_scope(create_session_factory(engine)) as session:
            data = candidate.model_dump(
                exclude={"provenance", "language", "document_type", "citation_count"}
            )
            PaperRepository(session).add(
                Paper(
                    id="fixture-source-001", first_seen_at=candidate.provenance.retrieved_at, **data
                )
            )
    finally:
        engine.dispose()
    base_url = f"http://127.0.0.1:{_free_port()}"
    log_path = tmp_path / "source-uvicorn.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [
                str(BIN / "uvicorn"),
                "papersteward.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                base_url.rsplit(":", 1)[1],
            ],
            cwd=ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(f"{base_url}/health", process, log_path)
            with urllib.request.urlopen(f"{base_url}/papers", timeout=2) as response:
                papers = json.load(response)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    assert papers["total"] == 1
    assert papers["items"][0]["title"] == candidate.title
    assert papers["items"][0]["status"] == "discovered"
    assert papers["items"][0]["created_at"] == "2026-10-22T00:00:00Z"
