from datetime import timedelta

from test_digest_delivery import prepared
from test_priority_queue import NOW

from papersteward.storage import session_scope
from papersteward.storage.models import DigestRow, PaperRow

HEADERS = {"HX-Request": "true", "X-PaperSteward-UI": "1"}


def test_pages_reading_loop_no_external_assets_and_revision(client, migrated_engine, monkeypatch):
    factory = prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW)
    for path in ("/ui/today", "/ui/queue", "/ui/papers/p", "/ui/archive"):
        response = client.get(path)
        assert response.status_code == 200 and '<html lang="zh-CN">' in response.text
        assert 'src="https://' not in response.text
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/ui/static/htmx.js").status_code == 200
    assert client.get("/ui/static/style.css").status_code == 200
    setup_js = client.get("/ui/static/setup.js")
    assert setup_js.status_code == 200
    assert "testLlm" in setup_js.text and "llm-test-result" in setup_js.text
    body = {
        "kind": "done",
        "mode": "skim",
        "score": "4",
        "note": "本地备注",
        "request_id": "ui-done",
    }
    assert client.post("/ui/actions/p", headers=HEADERS, data=body).status_code == 200
    assert "本地备注" in client.get("/ui/archive").text
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW + timedelta(seconds=1))
    assert (
        client.post(
            "/ui/actions/p",
            headers=HEADERS,
            data={"kind": "note", "note": "新备注", "request_id": "ui-note"},
        ).status_code
        == 200
    )
    assert "新备注" in client.get("/ui/papers/p").text
    with session_scope(factory) as session:
        assert session.get(PaperRow, "p").status == "archived"


def test_history_is_read_only_and_ui_does_not_create_digests(client, migrated_engine, monkeypatch):
    factory = prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW)
    page = client.get(f"/ui/digests/{NOW.date()}")
    assert page.status_code == 200 and "只读保留" in page.text
    assert 'hx-post="/ui/actions' not in page.text
    assert client.post("/ui/digests", headers=HEADERS, data={}).status_code == 404
    with session_scope(factory) as session:
        assert session.get(DigestRow, NOW.date()) is not None


def test_ui_cross_site_and_malformed_forms_rejected(client, migrated_engine, monkeypatch):
    prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW)
    for headers in (
        {},
        {"Origin": "https://other.invalid"},
        {**HEADERS, "Sec-Fetch-Site": "cross-site"},
    ):
        response = client.post(
            "/ui/actions/p", data={"kind": "skip", "request_id": "x"}, headers=headers
        )
        assert response.status_code == 409
    response = client.post(
        "/ui/actions/p", data={"kind": "done", "score": "4.5", "request_id": "bad"}, headers=HEADERS
    )
    assert response.status_code == 422 and 'role="alert"' in response.text
    response = client.post(
        "/ui/actions/p",
        data={"kind": "done", "mode": "skim", "score": "6", "request_id": "bad"},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_ui_untrusted_titles_and_notes_never_become_markup(client, migrated_engine, monkeypatch):
    factory = prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW)
    with session_scope(factory) as session:
        session.get(PaperRow, "p").title = '<img src=x onerror="alert(1)">'
    html = client.get("/ui/papers/p").text
    assert "<img" not in html and "&lt;img" in html
    response = client.post(
        "/ui/actions/p",
        headers=HEADERS,
        data={
            "kind": "done",
            "mode": "skim",
            "score": "",
            "note": "<script>bad</script>",
            "request_id": "safe",
        },
    )
    assert response.status_code == 200
    assert "<script>bad" not in client.get("/ui/archive").text
