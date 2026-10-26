"""真实 Uvicorn、无模型配置、禁止外网的阅读回顾主要闭环。"""

import os
import runpy
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_walking_skeleton import BIN, ROOT, _free_port, _run_command, _wait_for_health

from papersteward.storage import create_db_engine


def _wait_report(client: httpx.Client, run_id: str) -> str:
    for _ in range(100):
        response = client.get(f"/ui/runs/{run_id}", follow_redirects=False)
        location = response.headers.get("location", "")
        if "/reading-insights/reports/" in location:
            return location
        if "任务失败" in response.text:
            pytest.fail(response.text)
        time.sleep(0.05)
    pytest.fail("回顾未完成")


@pytest.mark.e2e
def test_offline_reading_insights_journey(tmp_path: Path):
    database = tmp_path / "insights.db"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PAPERSTEWARD_")}
    env.update(
        PAPERSTEWARD_DATABASE_URL=f"sqlite+pysqlite:///{database}",
        PAPERSTEWARD_DATA_DIR=str(tmp_path / "data"),
        PAPERSTEWARD_LLM_PROVIDER="fake",
        PAPERSTEWARD_LLM_MODEL="",
        PAPERSTEWARD_LLM_API_KEY="",
        INSIGHTS_DENIED_LOG=str(tmp_path / "denied-network.log"),
    )
    _run_command([str(BIN / "alembic"), "upgrade", "head"], env)
    engine = create_db_engine(env["PAPERSTEWARD_DATABASE_URL"])
    try:
        helpers = runpy.run_path(str(ROOT / "tests/integration/conftest.py"))
        helpers["activate_workspace"](engine)
    finally:
        engine.dispose()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    log_path = tmp_path / "insights-uvicorn.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(BIN / "python"), str(ROOT / "tests/e2e/insights_server.py"), str(port)],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(base_url + "/health", process, log_path)
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
                headers = {"origin": base_url}
                imported = client.post(
                    "/ui/import/confirm",
                    headers=headers,
                    data={
                        "title": "离线回顾验收论文",
                        "score": "4",
                        "note": "私人反馈原文",
                        "read_time_precision": "day",
                        "read_time_day": today,
                    },
                )
                assert imported.status_code == 303, imported.text
                paper_url = imported.headers["location"]
                paper_id = paper_url.split("/papers/", 1)[1].split("?", 1)[0]
                assert "阅读时间修订记录" in client.get(paper_url).text
                overview = client.get("/ui/reading-insights?kind=week")
                assert "阅读明细（1）" in overview.text and "补录明细（1）" in overview.text
                request = {"period": "week", "anchor_date": today, "mode": "revised_now"}
                submitted = client.post(
                    "/ui/reading-insights/reports",
                    headers=headers,
                    data={**request, "run_id": "e2e-review-1"},
                )
                assert submitted.status_code == 303
                old_url = _wait_report(client, "e2e-review-1")
                frozen = client.get(old_url + "?format=json").text
                assert "私人反馈原文" not in frozen
                revised = client.post(
                    f"/papers/{paper_id}/actions",
                    json={
                        "kind": "score",
                        "score": 2,
                        "request_id": "e2e-score",
                    },
                )
                assert revised.status_code == 200
                client.post(
                    "/ui/reading-insights/reports",
                    headers=headers,
                    data={**request, "run_id": "e2e-review-2"},
                )
                new_url = _wait_report(client, "e2e-review-2")
                assert new_url != old_url
                assert client.get(old_url + "?format=json").text == frozen
                assert "相关数据已有变化" in client.get(old_url).text
                assert (
                    client.get(new_url + "?format=json").json()["content"]["readings"][0][
                        "feedback"
                    ][-1]["score"]
                    == 2
                )
                created = client.post(
                    "/ui/reading-insights/assets",
                    headers=headers,
                    data={
                        "kind": "problem",
                        "title": "仍待检验的问题",
                        "body": "私人问题原文",
                        "sources": paper_id,
                        "source_paper": paper_id,
                        "idempotency_key": "e2e-asset",
                    },
                )
                assert (
                    created.status_code == 303
                    and f"/ui/papers/{paper_id}" in created.headers["location"]
                )
                history = client.get("/ui/reading-insights?kind=all")
                assert "当前手工资产（1）" in history.text and "仍待检验的问题" in history.text
                assert "私人问题原文" not in client.get("/ui/reading-insights/export/download").text
                personal = client.get(
                    "/ui/reading-insights/export/download?scope=personal&confirm_private=yes"
                )
                assert personal.status_code == 200 and "私人问题原文" in personal.text
                assert personal.json()["assets"][0]["revisions"][0]["status"] == "open"
                assert not Path(env["INSIGHTS_DENIED_LOG"]).exists()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
