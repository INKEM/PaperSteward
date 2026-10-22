from __future__ import annotations

import json
from datetime import timedelta

from test_api_dry_run import client as client
from test_custom_views import DAY, NOW, _prepare

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.enums import EventType, ScoringChangeStatus
from papersteward.pipeline.context import bound_context
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.repositories import (
    ConfigRepository,
    EventRepository,
    ScoringChangeRepository,
    StaticScoreSnapshotRepository,
)

OVERRIDES = {"static.relevance": 0.50, "static.extra_value": 0.30}


def _session(migrated_engine):
    return session_scope(create_session_factory(migrated_engine))


def test_preview_confirm_activate_and_queue_uses_new_config(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    previewed = client.post(
        "/config/scoring/preview",
        json={"overrides": OVERRIDES, "score_date": str(DAY)},
    )
    assert previewed.status_code == 201, previewed.text
    change = previewed.json()
    assert change["status"] == "previewed"
    assert change["base_version"] == "1.0.0"
    assert change["config_version"] == "1.0.1"
    assert change["preview"]["items"][0]["paper_id"] == "high-e"
    assert change["preview"]["changed"] == 2

    with _session(migrated_engine) as session:
        assert ConfigRepository(session).get("scoring", "1.0.1") is None
        assert ScoringChangeRepository(session).get(change["id"]).status is (  # type: ignore[union-attr]
            ScoringChangeStatus.PREVIEWED
        )
        _, binding = bound_context(session, NOW, day=DAY)
        assert binding["scoring_version"] == "1.0.0"

    confirmed = client.post(
        f"/config/scoring/changes/{change['id']}/confirm",
        json={"confirmed_by": "fixture-user", "note": "审阅 Top-N 影响后确认"},
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["static_recomputed"] == 3

    effective = DAY + timedelta(days=1)
    with _session(migrated_engine) as session:
        assert ConfigRepository(session).get("scoring", "1.0.1") is not None
        activation = ScoringChangeRepository(session).active_for(effective)
        assert activation is not None and activation.id == change["id"]
        _, old_binding = bound_context(session, NOW, day=DAY)
        assert old_binding["scoring_version"] == "1.0.0"
        _, new_binding = bound_context(session, NOW + timedelta(days=1), day=effective)
        assert new_binding["scoring_version"] == "1.0.1"
        assert new_binding["base_scoring_version"] == "1.0.0"
        assert new_binding["scoring_change_id"] == change["id"]
        latest = StaticScoreSnapshotRepository(session).latest("high-r")
        assert latest is not None
        assert latest.score_date == effective
        assert latest.static_score == 55.0
        events = EventRepository(session).recent(limit=20)
        assert any(item.event_type is EventType.CONFIG_ACTIVATED for item in events)

    # 旧日仍用旧配置；生效日使用新配置与重算后的静态快照。
    monkeypatch.setattr("papersteward.api.queue.utc_now", lambda: NOW + timedelta(days=1))
    created = client.post("/scores/snapshot", json={"score_date": str(effective)})
    assert created.status_code == 200, created.text
    old_queue = client.get(f"/queue?date={DAY}").json()
    assert old_queue["items"][0]["paper_id"] == "high-r"
    new_queue = client.get(f"/queue?date={effective}").json()
    assert new_queue["items"][0]["paper_id"] == "high-e"

    duplicate = client.post(
        f"/config/scoring/changes/{change['id']}/confirm",
        json={"confirmed_by": "fixture-user"},
    )
    assert duplicate.status_code == 409
    assert client.get("/config/scoring/changes").json()["total"] == 1
    assert client.get(f"/config/scoring/changes/{change['id']}").json()["status"] == "confirmed"


def test_preview_rejects_invalid_overrides_dates_and_versions(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    assert (
        client.post(
            "/config/scoring/preview",
            json={"overrides": {"static.relevance": 0.40}, "score_date": str(DAY)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/config/scoring/preview",
            json={"overrides": {"unknown.field": 1}, "score_date": str(DAY)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/config/scoring/preview",
            json={
                "overrides": OVERRIDES,
                "score_date": str(DAY),
                "effective_on": str(DAY),
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/config/scoring/preview",
            json={"overrides": OVERRIDES, "score_date": str(DAY), "version": "1.0.0"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/config/scoring/preview",
            json={"overrides": OVERRIDES, "score_date": str(DAY), "version": "2.0.0"},
        ).status_code
        == 201
    )


def test_cli_preview_confirm_and_history(client, migrated_engine, monkeypatch, capsys):
    _prepare(client, migrated_engine, monkeypatch)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        assert (
            main(
                [
                    "config",
                    "preview",
                    "--set",
                    "static.relevance=0.50",
                    "--set",
                    "static.extra_value=0.30",
                    "--date",
                    str(DAY),
                    "--json",
                ]
            )
            == 0
        )
        change = json.loads(capsys.readouterr().out)
        assert change["status"] == "previewed"
        assert change["preview"]["items"][0]["paper_id"] == "high-e"

        assert main(["config", "changes", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 1
        assert main(["config", "show", change["id"], "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["id"] == change["id"]

        assert (
            main(
                [
                    "config",
                    "confirm",
                    change["id"],
                    "--by",
                    "fixture-user",
                    "--note",
                    "确认",
                    "--json",
                ]
            )
            == 0
        )
        confirmed = json.loads(capsys.readouterr().out)
        assert confirmed["status"] == "confirmed"
        assert confirmed["static_recomputed"] == 3

        assert main(["config", "changes", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["status"] == "confirmed"

        assert (
            main(
                [
                    "config",
                    "preview",
                    "--set",
                    "static.relevance=0.40",
                    "--date",
                    str(DAY),
                    "--json",
                ]
            )
            == 1
        )
        assert "错误" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
