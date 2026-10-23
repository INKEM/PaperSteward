from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from test_walking_skeleton import BIN, _free_port, _wait_for_health

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXTRACTION = json.loads((FIXTURES / "llm/profile_extraction.valid.json").read_text())
CHANGED = json.loads(json.dumps(EXTRACTION))
CHANGED["profile"]["positioning"]["summary"] = "第二版定位：评测与可复现性优先。"


class _MockLLM(BaseHTTPRequestHandler):
    calls = 0
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        prompt = payload["messages"][0]["content"]
        if prompt.strip() == "ping":
            content = "pong"
        else:
            with type(self).lock:
                index = min(type(self).calls, 1)
                type(self).calls += 1
            content = json.dumps(CHANGED if index else EXTRACTION, ensure_ascii=False)
        body = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def _start_mock_llm() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _wait_drafts(client: httpx.Client, version: str) -> dict:
    for _ in range(200):
        drafts = client.get("/onboarding/drafts").json()
        if drafts["profile"] is not None and drafts["profile"]["version"] == version:
            return drafts
        time.sleep(0.1)
    raise AssertionError(f"draft {version} not generated")


def _wait_setup_text(client: httpx.Client, needle: str) -> None:
    """草案已落库但任务尚未标记完成的瞬间，设置页仍显示进度；轮询等审阅出现。"""
    for _ in range(200):
        if needle in client.get("/ui/setup").text:
            return
        time.sleep(0.1)
    raise AssertionError(f"setup page never showed {needle!r}")


@pytest.mark.e2e
def test_browser_only_setup_from_api_connection_to_today(tmp_path: Path) -> None:
    mock, llm_base = _start_mock_llm()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "start.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PAPERSTEWARD_")}
    env.update(PAPERSTEWARD_LOG_LEVEL="WARNING", PAPERSTEWARD_LLM_PROVIDER="fake")
    headers = {"Origin": url, "Content-Type": "application/x-www-form-urlencoded"}
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                str(BIN / "papersteward"),
                "start",
                "--data-dir",
                str(tmp_path / "data"),
                "--port",
                str(port),
                "--no-browser",
            ],
            cwd=tmp_path,  # 不读取仓库里的 .env；全程只通过浏览器配置
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(url + "/health", process, log_path)
            with httpx.Client(
                base_url=url, headers=headers, follow_redirects=True, trust_env=False
            ) as client:
                assert client.get("/ui/today").url.path == "/ui/setup"
                setup = client.get("/ui/setup")
                assert "连接模型" in setup.text and 'name="consent"' in setup.text

                saved = client.post(
                    "/ui/settings/llm",
                    data={
                        "base_url": llm_base,
                        "api_key": "sk-e2e",
                        "model": "mock-model",
                        "timeout_s": "30",
                        "consent": "yes",
                        "next": "/ui/setup",
                    },
                )
                assert saved.status_code == 200
                assert "模型已连接" in saved.text
                status = client.get("/settings/llm").json()
                assert status["configured"] is True and status["ready"] is True

                tested = client.post(
                    "/settings/llm/test",
                    headers={"Content-Type": "application/json"},
                    json={
                        "base_url": llm_base,
                        "api_key": "sk-e2e",
                        "model": "mock-model",
                        "timeout_s": 10,
                    },
                )
                assert tested.status_code == 200 and tested.json()["ok"] is True

                material = client.post(
                    "/ui/setup/materials",
                    data={
                        "name": "研究简介",
                        "content": "我研究长文档检索、RAG 与忠实性评测，关注可复现实验。",
                        "from_file": "0",
                    },
                )
                assert material.status_code == 200
                generated = client.post("/ui/setup/generate", data={"run_id": "ext-e2e"})
                # 单页面流程：提交后留在设置页原地显示进度，不跳转独立进度页。
                assert generated.status_code == 200 and generated.url.path == "/ui/setup"
                drafts = _wait_drafts(client, "0.1")
                _wait_setup_text(client, "渲染视图")

                feedback = client.post(
                    "/ui/setup/feedback",
                    data={"run_id": "ext-e2e-2", "comment": "更强调评测与可复现性"},
                )
                assert feedback.status_code == 200
                drafts = _wait_drafts(client, "0.2")
                _wait_setup_text(client, "与上一版的差异")
                page = client.get("/ui/setup")
                assert "第二版定位" in page.text

                profile, taxonomy = drafts["profile"], drafts["taxonomy"]
                confirmed = client.post(
                    "/ui/setup/confirm",
                    data={
                        "confirmed": "yes",
                        "confirmed_by": "e2e-user",
                        "change_summary": "审阅通过",
                        "expected_revision": "1",
                        "profile_id": profile["id"],
                        "profile_version": profile["version"],
                        "profile_hash": profile["content_hash"],
                        "taxonomy_id": taxonomy["id"],
                        "taxonomy_version": taxonomy["version"],
                        "taxonomy_hash": taxonomy["content_hash"],
                    },
                )
                assert confirmed.status_code == 200
                assert client.get("/onboarding").json() == {"stage": "active", "revision": 2}
                today = client.get("/ui/today")
                assert today.status_code == 200
                assert "阅读队列" in today.text and "挖掘新论文" in today.text
                assert "生成今日日报" not in today.text
                assert "首次设置已完成" in client.get("/ui/setup").text
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            mock.shutdown()
            mock.server_close()
