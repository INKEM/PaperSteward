from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from test_api_dry_run import client as client
from test_pipeline_ingest import activate

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.entities import (
    Bucket,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    StaticScoreSnapshot,
    Summary,
)
from papersteward.core.enums import (
    BucketRole,
    BucketType,
    PaperStatus,
    SummaryValidationStatus,
    TagRole,
)
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import ScoreSnapshotRow
from papersteward.storage.repositories import (
    BucketRepository,
    ConfigRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    StaticScoreSnapshotRepository,
    SummaryRepository,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
DAY = NOW.date()


def _seed(session, key: str, *, relevance: float, authority: float, extra: float) -> None:
    if BucketRepository(session).get("b-t01") is None:
        BucketRepository(session).add(Bucket("b-t01", "Topic", BucketType.TOPIC, tag_code="T01"))
    PaperRepository(session).add(
        Paper(
            key,
            f"Paper {key}",
            status=PaperStatus.CLASSIFIED,
            first_seen_at=NOW,
            published_at=NOW,
        )
    )
    PaperTagRepository(session).add(PaperTag(key, "T01", TagRole.PRIMARY, "1.0"))
    PaperBucketRepository(session).add(PaperBucket(key, "b-t01", BucketRole.FILING))
    SummaryRepository(session).save(
        Summary(
            key,
            "做方法并验证结论",
            "what",
            "how",
            "finding",
            "limitation",
            evidence=("evidence",),
            validation_status=SummaryValidationStatus.PASSED,
            provenance=Provenance("fixture", "summary/v2", NOW, "0" * 64),
        )
    )
    StaticScoreSnapshotRepository(session).append(
        StaticScoreSnapshot(
            paper_id=key,
            score_date=DAY,
            relevance=relevance,
            authority=authority,
            extra_value=extra,
            static_score=round(0.65 * relevance + 0.20 * authority + 0.15 * extra, 1),
            confidence=0.8,
            config_version="1.0.0",
            profile_version="1.0",
            taxonomy_version="1.0",
            input_hash=hashlib.sha256(key.encode()).hexdigest(),
            created_at=NOW,
            breakdown_json={},
        )
    )


def _prepare(client, migrated_engine, monkeypatch) -> None:
    activate(client)
    monkeypatch.setattr("papersteward.api.queue.utc_now", lambda: NOW)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _seed(session, "high-r", relevance=90, authority=50, extra=0)
        _seed(session, "high-e", relevance=65, authority=50, extra=100)
        _seed(session, "low-r", relevance=40, authority=50, extra=100)
    response = client.post("/scores/snapshot", json={"score_date": str(DAY)})
    assert response.status_code == 200, response.text


def test_presets_save_load_and_delete(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    presets = client.get("/views/presets").json()
    assert presets["total"] == 4
    assert [item["name"] for item in presets["items"]] == [
        "balanced",
        "frontier",
        "beginner",
        "reproducible",
    ]
    assert presets["items"][3]["weights"]["E"] == 40

    created = client.post(
        "/views",
        json={
            "name": "my-repro",
            "weights": {"R": 35, "E": 40, "F": 15, "A": 10},
        },
    )
    assert created.status_code == 201, created.text
    view = created.json()
    assert abs(sum(view["weights"].values()) - 1) < 1e-9
    assert view["weights"]["E"] == 0.4
    assert "*E" in view["formula"]
    assert client.get("/views").json()["total"] == 1
    assert client.get(f"/views/{view['id']}").json()["name"] == "my-repro"

    duplicate = client.post("/views", json={"name": "MY-REPRO", "preset": "balanced"})
    assert duplicate.status_code == 409
    preset_name = client.post("/views", json={"name": "reproducible", "preset": "balanced"})
    assert preset_name.status_code == 409
    invalid = client.post("/views", json={"name": "bad", "weights": {"R": 0}})
    assert invalid.status_code == 400

    assert client.delete(f"/views/{view['id']}").status_code == 204
    assert client.get(f"/views/{view['id']}").status_code == 404


def test_preview_reorders_without_writing_config_or_snapshots(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        config_count = len(ConfigRepository(session).versions("scoring"))
        snapshot_count = session.scalar(select(func.count()).select_from(ScoreSnapshotRow))
    result = client.post(
        "/views/preview",
        json={"score_date": str(DAY), "preset": "reproducible", "limit": 10},
    )
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview["view"]["name"] == "reproducible"
    assert preview["candidate_total"] == 2
    assert preview["changed"] == 2
    assert [item["paper_id"] for item in preview["items"]] == ["high-e", "high-r"]
    top = preview["items"][0]
    assert top["rank"] == 1 and top["default_rank"] == 2 and top["delta_rank"] == 1
    assert top["view_score"] > preview["items"][1]["view_score"]
    assert set(top["components"]) == {"R", "A", "E", "F", "H", "U"}
    assert "value" not in top and "rank_score" not in top
    with session_scope(factory) as session:
        assert len(ConfigRepository(session).versions("scoring")) == config_count
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == snapshot_count


def test_preview_errors_and_saved_view_loading(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    assert (
        client.post("/views/preview", json={"preset": "balanced", "weights": {"R": 1}}).status_code
        == 400
    )
    assert client.post("/views/preview", json={"weights": {"R": 0}}).status_code == 400
    assert client.post("/views/preview", json={"preset": "unknown"}).status_code == 404
    assert client.post("/views/preview", json={"view_id": "missing"}).status_code == 404

    created = client.post(
        "/views",
        json={"name": "my-repro", "weights": {"R": 35, "E": 40, "F": 15, "A": 10}},
    ).json()
    loaded = client.post(
        "/views/preview",
        json={"score_date": str(DAY), "view_id": created["id"]},
    )
    assert loaded.status_code == 200
    assert loaded.json()["view"]["name"] == "my-repro"
    assert loaded.json()["items"][0]["paper_id"] == "high-e"

    default = client.post("/views/preview", json={"score_date": str(DAY)})
    assert default.status_code == 200
    assert default.json()["view"]["name"] == "balanced"


def test_cli_views_preview_save_list_and_delete(client, migrated_engine, monkeypatch, capsys):
    _prepare(client, migrated_engine, monkeypatch)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        assert main(["view", "presets", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 4

        assert (
            main(
                [
                    "view",
                    "preview",
                    "--preset",
                    "reproducible",
                    "--date",
                    str(DAY),
                    "--json",
                ]
            )
            == 0
        )
        preview = json.loads(capsys.readouterr().out)
        assert preview["items"][0]["paper_id"] == "high-e"

        assert (
            main(
                [
                    "view",
                    "save",
                    "--name",
                    "my-repro",
                    "--weights",
                    "R:35,E:40,F:15,A:10",
                    "--json",
                ]
            )
            == 0
        )
        saved = json.loads(capsys.readouterr().out)
        assert saved["weights"]["E"] == 0.4

        assert main(["view", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 1
        assert main(["view", "show", "my-repro", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["name"] == "my-repro"

        assert main(["view", "preview", "--weights", "R:100", "--date", str(DAY), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["paper_id"] == "high-r"

        assert main(["view", "delete", "my-repro", "--json"]) == 0
        assert "my-repro" in capsys.readouterr().out
        assert main(["view", "show", "my-repro"]) == 1
        assert "不存在" in capsys.readouterr().err
        with pytest.raises(SystemExit) as excinfo:
            main(["view", "preview", "--preset", "unknown"])
        assert excinfo.value.code == 2
    finally:
        get_settings.cache_clear()


def test_page_views_previews_and_shows_saved_views(client, migrated_engine, monkeypatch):
    _prepare(client, migrated_engine, monkeypatch)
    page = client.get(f"/ui/views?preset=reproducible&date={DAY}")
    assert page.status_code == 200
    html = page.text
    assert "复现导向" in html
    assert "P_view" in html
    assert "不改变全局权重与快照" in html
    assert html.index("Paper high-e") < html.index("Paper high-r")

    client.post(
        "/views",
        json={"name": "my-repro", "weights": {"R": 35, "E": 40, "F": 15, "A": 10}},
    )
    saved = client.get(f"/ui/views?name=my-repro&date={DAY}")
    assert saved.status_code == 200
    assert "my-repro" in saved.text

    invalid = client.get("/ui/views?preset=unknown")
    assert invalid.status_code == 200
    assert "不存在" in invalid.text
