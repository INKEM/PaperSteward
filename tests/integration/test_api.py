from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from papersteward import __version__
from papersteward.api import create_app
from papersteward.core.entities import Paper
from papersteward.core.enums import PaperStatus
from papersteward.storage import (
    PaperRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)


@pytest.fixture
def api_client(schema_engine: Engine) -> Iterator[TestClient]:
    app = create_app(engine=schema_engine)
    with TestClient(app) as client:
        yield client


def _seed(engine: Engine, rows: list[tuple[str, str, datetime]]) -> None:
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        repository = PaperRepository(session)
        for paper_id, title, created_at in rows:
            repository.add(
                Paper(
                    id=paper_id,
                    title=title,
                    status=PaperStatus.DISCOVERED,
                    first_seen_at=created_at,
                )
            )


def test_health_reports_version_and_database(api_client: TestClient) -> None:
    response = api_client.get("/health", headers={"X-Request-ID": "req-health"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-health"
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "environment": "dev",
        "database": "ok",
    }


def test_health_is_degraded_when_database_unavailable(tmp_path: Path) -> None:
    not_a_database = tmp_path / "not-a-database"
    not_a_database.mkdir()
    engine = create_db_engine(f"sqlite+pysqlite:///{not_a_database}")
    app = create_app(engine=engine)
    try:
        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["database"] == "unavailable"
    finally:
        engine.dispose()


def test_unhandled_500_includes_request_id_header(sqlite_engine: Engine) -> None:
    app = create_app(engine=sqlite_engine)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/papers", headers={"X-Request-ID": "req-fail-500"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req-fail-500"
    assert response.json()["error"]["request_id"] == "req-fail-500"


def test_request_id_is_generated_and_invalid_values_are_replaced(
    api_client: TestClient,
) -> None:
    generated = api_client.get("/health")
    assert len(generated.headers["X-Request-ID"]) == 32
    int(generated.headers["X-Request-ID"], 16)

    replaced = api_client.get("/health", headers={"X-Request-ID": "bad id"})
    assert replaced.headers["X-Request-ID"] != "bad id"
    assert len(replaced.headers["X-Request-ID"]) == 32


def test_papers_returns_empty_list_envelope(api_client: TestClient) -> None:
    response = api_client.get("/papers")

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


def test_papers_returns_rows_in_created_order(
    api_client: TestClient,
    schema_engine: Engine,
) -> None:
    base = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    _seed(
        schema_engine,
        [
            ("p002", "Second", base + timedelta(minutes=2)),
            ("p001", "First", base),
            ("p003", "Third", base + timedelta(minutes=1)),
        ],
    )

    response = api_client.get("/papers?limit=2&offset=1")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["id"] for item in body["items"]] == ["p003", "p002"]

    first = body["items"][0]
    assert first["title"] == "Third"
    assert first["status"] == "discovered"
    created_at = datetime.fromisoformat(first["created_at"].replace("Z", "+00:00"))
    assert created_at.utcoffset() == timedelta(0)


def test_papers_invalid_pagination_uses_error_envelope(api_client: TestClient) -> None:
    response = api_client.get("/papers?limit=0")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "query.limit"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_uses_error_envelope_and_request_id(api_client: TestClient) -> None:
    response = api_client.get("/missing", headers={"X-Request-ID": "req-404"})

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "req-404"
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == "req-404"


def test_openapi_exposes_s3_endpoints(api_client: TestClient) -> None:
    response = api_client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/papers" in paths


def test_main_module_exposes_asgi_app() -> None:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        from papersteward import main

        assert main.app.title == "PaperSteward"
        assert main.app.version == __version__
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)
