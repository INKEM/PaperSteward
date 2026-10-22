from __future__ import annotations

import json
from html.parser import HTMLParser

from test_api_dry_run import client as client

from papersteward.api.setup_pages import starter

HEADERS = {"origin": "http://testserver", "content-type": "application/x-www-form-urlencoded"}


class Inputs(HTMLParser):
    def __init__(self, html, action=None):
        super().__init__()
        self.values = {}
        self.action = action
        self.active = action is None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and self.action is not None:
            self.active = values.get("action") == self.action
        if self.active and tag == "input" and "name" in values and values.get("type") != "checkbox":
            self.values[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.action is not None:
            self.active = False


def post(client, path, data):
    return client.post(path, data=data, headers=HEADERS)


def draft(client, kind):
    data = {
        "identity": "demo-profile" if kind == "profile" else "onboarding-taxonomy",
        "content_md": starter(kind + ".md"),
        "base_version": "",
    }
    if kind == "taxonomy":
        data.update(sample_kind="synthetic", seeds=starter("seeds.json"))
    response = post(client, f"/ui/setup/{kind}/draft", data)
    assert response.status_code == 200, response.text
    return response


def complete_g2(client):
    assert post(client, "/ui/setup/start", {}).status_code == 200
    for kind in ("profile", "taxonomy"):
        review = draft(client, kind)
        values = Inputs(review.text).values
        values["confirmed"] = "yes"
        confirmed = post(client, f"/ui/setup/{kind}/confirm", values)
        assert confirmed.status_code == 200, confirmed.text
    assert client.get("/onboarding").json()["stage"] == "calibration"


def test_web_g1_g2_starter_review_and_confirmation(client):
    first = client.get("/ui/today")
    assert first.url.path == "/ui/setup"
    assert "default-src 'self'" in first.headers["content-security-policy"]
    complete_g2(client)
    assert client.get("/queue?date=2026-10-22").status_code == 409
    assert client.get("/ui/static/setup.js").status_code == 200


def test_web_confirmation_does_not_refresh_stale_revision_or_hash(client):
    post(client, "/ui/setup/start", {})
    review = draft(client, "profile")
    values = Inputs(review.text).values
    values.update(confirmed="yes", expected_revision="999")
    assert post(client, "/ui/setup/profile/confirm", values).status_code == 409
    values.update(expected_revision="1", content_hash="0" * 64)
    assert post(client, "/ui/setup/profile/confirm", values).status_code == 409
    assert client.get("/onboarding").json()["stage"] == "profile"


def test_web_invalid_cross_site_large_and_unconfirmed_input(client):
    assert client.post("/ui/setup/start", data={}).status_code == 409
    assert (
        client.post(
            "/ui/setup/start", data={}, headers={"origin": "https://evil.example"}
        ).status_code
        == 409
    )
    post(client, "/ui/setup/start", {})
    invalid = post(
        client,
        "/ui/setup/profile/draft",
        {"identity": "p", "content_md": "<script>alert(1)</script>"},
    )
    assert invalid.status_code == 400
    assert "<script>alert" not in invalid.text
    review = draft(client, "profile")
    values = Inputs(review.text).values
    assert post(client, "/ui/setup/profile/confirm", values).status_code == 422
    assert (
        post(client, "/ui/setup/profile/draft", {"content_md": "x" * (512 * 1024)}).status_code
        == 400
    )


def test_starter_seed_list_is_synthetic():
    seeds = json.loads(starter("seeds.json"))
    assert len(seeds) == 20 and all(item["paper_id"].startswith("syn-") for item in seeds)


def test_taxonomy_edit_preserves_uploaded_seeds_and_sample_kind(client):
    post(client, "/ui/setup/start", {})
    review = draft(client, "profile")
    values = Inputs(review.text).values
    values["confirmed"] = "yes"
    assert post(client, "/ui/setup/profile/confirm", values).status_code == 200
    seeds = json.loads(starter("seeds.json"))
    seeds[0]["title"] = "用户种子标题"
    response = post(
        client,
        "/ui/setup/taxonomy/draft",
        {
            "identity": "onboarding-taxonomy",
            "content_md": starter("taxonomy.md"),
            "sample_kind": "user",
            "seeds": json.dumps(seeds),
        },
    )
    assert response.status_code == 200
    edit = client.get("/ui/setup?kind=taxonomy&identity=onboarding-taxonomy&version=1.0&edit=true")
    assert "用户种子标题" in edit.text
    assert '<option value="user" selected>' in edit.text


def complete_g3(client):
    complete_g2(client)
    report = post(client, "/ui/setup/calibration/example", {})
    assert report.status_code == 200, report.text
    assert "仅验证流程" in report.text or "只验证流程" in report.text
    values = Inputs(report.text, "/ui/setup/calibration/confirm").values
    values["confirmed"] = "yes"
    assert post(client, "/ui/setup/calibration/confirm", values).status_code == 200
    assert client.get("/onboarding").json()["stage"] == "dry_run"


def runtime_form():
    return {
        "version": "1",
        "push_time": "08:00",
        "timezone": "Asia/Shanghai",
        "external_model_data": "disabled",
        "target_shares": json.dumps({f"T{i:02}": 0.2 for i in range(1, 6)}),
    }


def test_web_g3_g4_requires_review_feedback_and_exact_evidence(client):
    complete_g3(client)
    preview = post(client, "/ui/setup/dry-run/create", runtime_form())
    assert preview.status_code == 200, preview.text
    assert "试运行日报预览" in preview.text and "仅流程验证" in preview.text
    assert client.get("/onboarding").json()["stage"] == "dry_run"
    feedback = Inputs(preview.text, "/ui/setup/dry-run/feedback").values
    accepted = post(client, "/ui/setup/dry-run/feedback", feedback)
    assert accepted.status_code == 200, accepted.text
    confirm = Inputs(accepted.text, "/ui/setup/dry-run/confirm").values
    confirm["confirmed"] = "yes"
    assert (
        post(
            client, "/ui/setup/dry-run/confirm", {**confirm, "expected_revision": "99"}
        ).status_code
        == 409
    )
    assert (
        post(
            client, "/ui/setup/dry-run/confirm", {**confirm, "feedback_hash": "0" * 64}
        ).status_code
        == 409
    )
    result = post(client, "/ui/setup/dry-run/confirm", confirm)
    assert result.status_code == 200, result.text
    assert client.get("/onboarding").json()["stage"] == "active"
    assert "不能处理真实论文" in result.text


def test_web_custom_calibration_and_invalid_dry_run_input(client):
    complete_g2(client)
    data = json.loads(starter("calibration.json"))
    agent = data.pop("agent_batch")
    user = data.pop("user_batch")
    assert post(client, "/ui/setup/calibration/create", {"payload": "{"}).status_code == 422
    assert (
        post(client, "/ui/setup/calibration/create", {"payload": json.dumps(data)}).status_code
        == 200
    )
    assert (
        post(
            client,
            "/ui/setup/calibration/agent",
            {"round_number": "1", "payload": json.dumps(agent)},
        ).status_code
        == 200
    )
    assert client.get("/onboarding/calibration/1").json()["report"] is None
    report = post(
        client, "/ui/setup/calibration/user", {"round_number": "1", "payload": json.dumps(user)}
    )
    confirm = Inputs(report.text, "/ui/setup/calibration/confirm").values
    confirm["confirmed"] = "yes"
    assert (
        post(
            client, "/ui/setup/calibration/confirm", {**confirm, "report_hash": "0" * 64}
        ).status_code
        == 409
    )
    assert post(client, "/ui/setup/calibration/confirm", confirm).status_code == 200
    invalid = post(client, "/ui/setup/dry-run/create", {**runtime_form(), "timezone": "INVALID"})
    assert invalid.status_code == 422
    assert client.get("/onboarding").json()["stage"] == "dry_run"
