from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_api_dry_run import client as client
from test_custom_views import DAY, NOW, _prepare, _seed

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.enums import EventType, PaperStatus, ScoringChangeStatus
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.scoring.config import ScoringConfig, config_value_json
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
    top = change["preview"]["items"][0]
    assert "new_rank_score" in top
    scores = {key: value["score"] for key, value in top["components"].items()}
    expected_static = scores["R"] * 0.50 + scores["A"] * 0.20 + scores["E"] * 0.30
    expected_dynamic = scores["F"] * 0.60 + scores["H"] * 0.40
    expected_priority = round(
        expected_static * 0.70 + expected_dynamic * 0.20 + scores["U"] * 0.10,
        1,
    )
    assert top["new_priority"] == pytest.approx(expected_priority, abs=0.11)
    assert top["new_rank_score"] == pytest.approx(expected_priority, abs=0.11)

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
    preview_items = {item["paper_id"]: item for item in change["preview"]["items"]}
    queue_items = {item["paper_id"]: item for item in new_queue["items"]}
    for paper_id in ("high-r", "high-e"):
        assert preview_items[paper_id]["new_priority"] == pytest.approx(
            queue_items[paper_id]["priority"], abs=0.11
        )
        assert preview_items[paper_id]["new_rank_score"] == pytest.approx(
            queue_items[paper_id]["rank_score"], abs=0.11
        )

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
    for override in (
        {"gates.max_daily_push": 99},
        {"stability.rank_ema": 0.1},
        {"dynamic.half_life_days.fast": 1},
        {"quasi_dynamic.tag_weight": 0.6},
    ):
        rejected = client.post(
            "/config/scoring/preview",
            json={"overrides": override, "score_date": str(DAY)},
        )
        assert rejected.status_code == 400, (override, rejected.text)
        assert "线性权重" in rejected.json()["error"]["message"]
    with _session(migrated_engine) as session:
        base = ConfigRepository(session).get("scoring", "1.0.0")
        assert base is not None
        full = config_value_json(ScoringConfig.model_validate(base.value_json))
    full["version"] = "1.0.1"
    full["gates"]["max_daily_push"] = 99
    assert (
        client.post(
            "/config/scoring/preview",
            json={"config": full, "score_date": str(DAY)},
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


def test_changes_total_is_not_page_size(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    for version in ("2.0.0", "2.0.1"):
        response = client.post(
            "/config/scoring/preview",
            json={
                "overrides": OVERRIDES,
                "score_date": str(DAY),
                "version": version,
            },
        )
        assert response.status_code == 201, response.text
    page = client.get("/config/scoring/changes", params={"limit": 1}).json()
    assert len(page["items"]) == 1
    assert page["total"] == 2


def test_bound_context_local_day_uses_runtime_timezone(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    moment = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    with _session(migrated_engine) as session:
        _, binding = bound_context(session, moment, enforce_schedule=False)
        assert runtime_local_day(session, moment, binding).isoformat() == "2026-10-22"


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

        assert (
            main(
                [
                    "config",
                    "preview",
                    "--set",
                    "static.relevance=0.50",
                    "--set",
                    "static.extra_value=0.30",
                    "--version",
                    "2.0.0",
                    "--date",
                    str(DAY),
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert main(["config", "changes", "--limit", "1", "--json"]) == 0
        history = json.loads(capsys.readouterr().out)
        assert len(history["items"]) == 1
        assert history["total"] == 2
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
        statuses = {item["status"] for item in json.loads(capsys.readouterr().out)["items"]}
        assert "confirmed" in statuses and "previewed" in statuses

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


def test_late_process_after_confirmation_gets_effective_snapshot(
    client, migrated_engine, monkeypatch
):
    from test_pipeline_ingest import activate

    activate(client, quiet_start=None, quiet_end=None)
    monkeypatch.setattr("papersteward.api.queue.utc_now", lambda: NOW)
    with _session(migrated_engine) as session:
        _seed(session, "high-r", relevance=90, authority=50, extra=0)
        _seed(session, "high-e", relevance=65, authority=50, extra=100)
        _seed(session, "low-r", relevance=40, authority=50, extra=100)
    response = client.post("/scores/snapshot", json={"score_date": str(DAY)})
    assert response.status_code == 200, response.text
    preview = client.post(
        "/config/scoring/preview",
        json={"overrides": OVERRIDES, "score_date": str(DAY)},
    )
    assert preview.status_code == 201, preview.text
    change = preview.json()
    confirmed = client.post(
        f"/config/scoring/changes/{change['id']}/confirm",
        json={"confirmed_by": "fixture-user", "note": "F2 late process"},
    )
    assert confirmed.status_code == 200, confirmed.text
    effective = DAY + timedelta(days=1)
    late_now = datetime.now(UTC)
    effective_now = late_now + timedelta(days=1)

    from test_special_shelf import (
        CONFIG,
        PROFILE,
        TAXONOMY,
        _buckets,
    )

    from papersteward.core.entities import Bucket, Paper
    from papersteward.core.enums import BucketType
    from papersteward.llm import FakeLLM, LLMGateway
    from papersteward.pipeline.embeddings import HashingEmbeddingProvider
    from papersteward.pipeline.process import process_paper
    from papersteward.storage.repositories import BucketRepository, PaperRepository

    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/process/p7_topic_a.json").read_text()
    )
    gateway = LLMGateway(
        FakeLLM(
            [
                json.dumps(payload[key], ensure_ascii=False)
                for key in (
                    "relevance",
                    "summary",
                    "classification",
                    "special_nomination",
                )
            ],
            model_id="fake-llm",
        ),
        clock=lambda: NOW,
    )
    with _session(migrated_engine) as session:
        if BucketRepository(session).get("b-t02") is None:
            BucketRepository(session).add(
                Bucket(
                    id="b-t02",
                    name="Topic T02",
                    bucket_type=BucketType.TOPIC,
                    tag_code="T02",
                )
            )
        late_paper = Paper(
            id="late-f2",
            title="P7 Topic High Relevance",
            abstract=(
                "P7 synthetic long-document QA. Sparse retrieval and cross-layer "
                "reranking improve grounding. We report EM gains of 2-4 points, "
                "but offline clustering adds latency."
            ),
            status=PaperStatus.SCREENED,
            first_seen_at=NOW,
        )
        PaperRepository(session).add(late_paper)
        outcome = process_paper(
            session,
            late_paper,
            profile=PROFILE,
            taxonomy=TAXONOMY,
            topic_buckets=_buckets(),
            config=CONFIG,
            gateway=gateway,
            embedding_provider=HashingEmbeddingProvider(),
            now=late_now,
            score_date=DAY,
        )
        assert outcome.static.score_date == DAY
        assert outcome.static.config_version == "1.0.0"
        latest = StaticScoreSnapshotRepository(session).latest(late_paper.id)
        assert latest is not None
        assert latest.score_date == effective
        assert latest.config_version == "1.0.1"
        created = create_snapshots(session, effective, now=effective_now)
        assert created.created >= 3
        queue = read_queue(session, effective, now=effective_now, limit=200)
        assert late_paper.id in {item.paper_id for item in queue.items}


def test_late_promotion_after_confirmation_gets_effective_snapshot(
    client, migrated_engine, monkeypatch
):
    from test_shelf_view import _seed_special
    from test_special_shelf import NOW as SPECIAL_NOW

    from papersteward.core.entities import Bucket, StaticScoreSnapshot
    from papersteward.core.enums import BucketType, SpecialBucketKind
    from papersteward.pipeline.shelf import promote_to_topic
    from papersteward.storage.repositories import BucketRepository

    _prepare(client, migrated_engine, monkeypatch)
    preview = client.post(
        "/config/scoring/preview",
        json={"overrides": OVERRIDES, "score_date": str(DAY)},
    )
    assert preview.status_code == 201, preview.text
    change = preview.json()
    confirmed = client.post(
        f"/config/scoring/changes/{change['id']}/confirm",
        json={"confirmed_by": "fixture-user", "note": "F2 late promote"},
    )
    assert confirmed.status_code == 200, confirmed.text
    effective = DAY + timedelta(days=1)

    with _session(migrated_engine) as session:
        BucketRepository(session).add(
            Bucket(
                id="b-t02",
                name="Topic T02",
                bucket_type=BucketType.TOPIC,
                tag_code="T02",
            )
        )
        _seed_special(
            session,
            "promote-late",
            SpecialBucketKind.REVIEW,
            title="Late promotion",
        )
        StaticScoreSnapshotRepository(session).append(
            StaticScoreSnapshot(
                paper_id="promote-late",
                score_date=DAY,
                relevance=70.0,
                authority=50.0,
                extra_value=50.0,
                static_score=63.0,
                confidence=0.9,
                config_version="1.0.0",
                profile_version="1.0",
                taxonomy_version="1.0",
                input_hash="a" * 64,
                breakdown_json={},
                created_at=SPECIAL_NOW,
            )
        )
        promotion = promote_to_topic(
            session,
            "promote-late",
            reviewed_by="fixture-user",
            note="F2 late promote",
            now=SPECIAL_NOW,
        )
        assert promotion.paper.status.value == "classified"
        latest = StaticScoreSnapshotRepository(session).latest("promote-late")
        assert latest is not None
        assert latest.score_date == effective
        assert latest.config_version == "1.0.1"

        _seed_special(
            session,
            "promote-future",
            SpecialBucketKind.REVIEW,
            title="Future promotion",
        )
        StaticScoreSnapshotRepository(session).append(
            StaticScoreSnapshot(
                paper_id="promote-future",
                score_date=effective,
                relevance=70.0,
                authority=50.0,
                extra_value=50.0,
                static_score=60.0,
                confidence=0.9,
                config_version="1.0.1",
                profile_version="1.0",
                taxonomy_version="1.0",
                input_hash="b" * 64,
                breakdown_json={},
                created_at=SPECIAL_NOW,
            )
        )
        future = promote_to_topic(
            session,
            "promote-future",
            reviewed_by="fixture-user",
            note="future promote",
            now=SPECIAL_NOW,
        )
        assert future.static.score_date == effective
        assert future.static.config_version == "1.0.1"
        assert (
            StaticScoreSnapshotRepository(session).latest("promote-future").score_date == effective
        )


from papersteward.scoring.queue import create_snapshots, read_queue  # noqa: E402


def test_default_preview_day_uses_runtime_timezone(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    moment = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    monkeypatch.setattr("papersteward.api.governance.utc_now", lambda: moment)
    response = client.post(
        "/config/scoring/preview",
        json={"overrides": OVERRIDES},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["preview_day"] == "2026-10-22"
    assert body["effective_on"] == "2026-10-23"


def test_cli_default_preview_day_uses_runtime_timezone(
    client, migrated_engine, monkeypatch, capsys
):
    _prepare(client, migrated_engine, monkeypatch)
    moment = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    monkeypatch.setattr("papersteward.cli.config.utc_now", lambda: moment)
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
                    "--json",
                ]
            )
            == 0
        )
        body = json.loads(capsys.readouterr().out)
        assert body["preview_day"] == "2026-10-22"
        assert body["effective_on"] == "2026-10-23"
    finally:
        get_settings.cache_clear()
