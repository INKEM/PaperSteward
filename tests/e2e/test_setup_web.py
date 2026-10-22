from __future__ import annotations

import json
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from test_walking_skeleton import BIN, ROOT, _free_port, _wait_for_health

from papersteward.api.setup_pages import starter


class FormValues(HTMLParser):
    def __init__(self, html: str, action: str | None = None):
        super().__init__()
        self.values: dict[str, str] = {}
        self.action = action
        self.active = action is None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and self.action is not None:
            self.active = attributes.get("action") == self.action
        if (
            self.active
            and tag == "input"
            and attributes.get("name")
            and attributes.get("type") != "checkbox"
        ):
            self.values[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.action is not None:
            self.active = False


@pytest.mark.e2e
def test_start_and_browser_setup_gates(tmp_path: Path) -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "start.log"
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
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(url + "/health", process, log_path)
            with httpx.Client(
                base_url=url,
                headers={"Origin": url, "Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=True,
                trust_env=False,
            ) as client:
                assert client.get("/ui/today").url.path == "/ui/setup"
                assert client.post("/ui/setup/start", data={}).status_code == 200
                for kind in ("profile", "taxonomy"):
                    data = {
                        "identity": "demo-profile" if kind == "profile" else "onboarding-taxonomy",
                        "content_md": starter(kind + ".md"),
                    }
                    if kind == "taxonomy":
                        data.update(sample_kind="synthetic", seeds=starter("seeds.json"))
                    view = client.post(f"/ui/setup/{kind}/draft", data=data)
                    assert view.status_code == 200, view.text
                    fields = FormValues(view.text).values
                    fields["confirmed"] = "yes"
                    confirmed = client.post(f"/ui/setup/{kind}/confirm", data=fields)
                    assert confirmed.status_code == 200, confirmed.text
                assert client.get("/onboarding").json()["stage"] == "calibration"
                example = client.post("/ui/setup/calibration/example", data={})
                assert example.status_code == 200, example.text
                fields = FormValues(example.text, "/ui/setup/calibration/confirm").values
                fields["confirmed"] = "yes"
                result = client.post("/ui/setup/calibration/confirm", data=fields)
                assert result.status_code == 200, result.text
                runtime = {
                    "version": "1",
                    "push_time": "08:00",
                    "timezone": "Asia/Shanghai",
                    "external_model_data": "disabled",
                    "target_shares": json.dumps({f"T{i:02}": 0.2 for i in range(1, 6)}),
                }
                preview = client.post("/ui/setup/dry-run/create", data=runtime)
                assert preview.status_code == 200, preview.text
                assert "仅流程验证" in preview.text
                fields = FormValues(preview.text, "/ui/setup/dry-run/feedback").values
                accepted = client.post("/ui/setup/dry-run/feedback", data=fields)
                assert accepted.status_code == 200, accepted.text
                fields = FormValues(accepted.text, "/ui/setup/dry-run/confirm").values
                fields["confirmed"] = "yes"
                result = client.post("/ui/setup/dry-run/confirm", data=fields)
                assert result.status_code == 200, result.text
                assert client.get("/onboarding").json()["stage"] == "active"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
