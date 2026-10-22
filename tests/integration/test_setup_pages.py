from __future__ import annotations

import json
from html.parser import HTMLParser

from test_api_dry_run import client as client

from papersteward.api.setup_pages import starter

HEADERS = {"origin": "http://testserver", "content-type": "application/x-www-form-urlencoded"}


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and "name" in values and values.get("type") != "checkbox":
            self.values[values["name"]] = values.get("value", "")


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
