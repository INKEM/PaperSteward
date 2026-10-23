"""LLM 设置 API 与运行时配置解析的合同测试。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from papersteward.api import create_app
from papersteward.config import get_settings
from papersteward.core.entities import Config, utc_now
from papersteward.core.enums import AssignedBy
from papersteward.llm.runtime import (
    LLMSettingsPayload,
    resolve_llm_config,
    save_llm_config,
)
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.repositories import ConfigRepository


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    app = create_app(engine=migrated_engine)
    with TestClient(app) as test_client:
        yield test_client


def _save_via_api(client: TestClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test-1234",
        "model": "test-model",
        "timeout_s": 45,
        "consent": True,
    }
    payload.update(overrides)
    return client.put("/settings/llm", json=payload).json()


def test_llm_settings_default_unconfigured(client: TestClient) -> None:
    status = client.get("/settings/llm").json()
    assert status["configured"] is False
    assert status["source"] == "env"
    assert status["ready"] is False
    assert status["problem"]


def test_llm_settings_save_and_read_back(client: TestClient) -> None:
    status = _save_via_api(client)
    assert status["configured"] is True
    assert status["source"] == "db"
    assert status["ready"] is True
    assert status["base_url"] == "https://api.example.com/v1"
    assert status["model"] == "test-model"
    assert status["api_key_set"] is True
    assert status["api_key_source"] == "db"
    assert status["api_key_hint"] == "****1234"
    assert status["problem"] is None
    assert status["updated_at"] is not None

    reread = client.get("/settings/llm").json()
    assert reread["configured"] is True
    assert reread["model"] == "test-model"


def test_llm_settings_consent_required(client: TestClient) -> None:
    response = client.put(
        "/settings/llm",
        json={
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-1234",
            "model": "test-model",
            "consent": False,
        },
    )
    assert response.status_code == 422


def test_llm_settings_http_requires_loopback(client: TestClient) -> None:
    response = client.put(
        "/settings/llm",
        json={
            "base_url": "http://api.example.com/v1",
            "api_key": "sk-test-1234",
            "model": "test-model",
            "consent": True,
        },
    )
    assert response.status_code == 422


def test_llm_settings_empty_key_reuses_saved(client: TestClient, migrated_engine: Engine) -> None:
    _save_via_api(client)
    status = _save_via_api(client, api_key="", model="another-model")
    assert status["api_key_set"] is True
    assert status["model"] == "another-model"

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        config = resolve_llm_config(session, get_settings())
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "sk-test-1234"


def test_remote_endpoint_requires_api_key(client: TestClient, migrated_engine: Engine) -> None:
    response = client.put(
        "/settings/llm",
        json={
            "base_url": "https://api.example.com/v1",
            "api_key": "",
            "model": "test-model",
            "consent": True,
        },
    )
    assert response.status_code == 400
    assert "必须填写 API 密钥" in response.json()["error"]["message"]
    assert client.get("/settings/llm").json()["configured"] is False


def test_loopback_endpoint_allows_empty_key(client: TestClient) -> None:
    response = client.put(
        "/settings/llm",
        json={
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "",
            "model": "local-model",
            "consent": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ready"] is True
    assert response.json()["api_key_set"] is False


def test_db_config_without_key_reuses_matching_env_key(
    client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("PAPERSTEWARD_LLM_API_KEY", "sk-env-key")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MODEL", "env-model")
    get_settings.cache_clear()
    try:
        with session_scope(create_session_factory(migrated_engine)) as session:
            ConfigRepository(session).add(
                Config(
                    key="llm_config",
                    version="v-old-without-key",
                    value_json={
                        "provider": "openai_compatible",
                        "base_url": "https://env.example.com/v1",
                        "api_key": None,
                        "model": "env-model",
                        "timeout_s": 30,
                        "consent": True,
                    },
                    updated_by=AssignedBy.USER,
                    updated_at=utc_now(),
                )
            )
        status = client.get("/settings/llm").json()
        assert status["configured"] is True
        assert status["ready"] is True
        assert status["api_key_set"] is True
        assert status["api_key_source"] == "env"
        with session_scope(create_session_factory(migrated_engine)) as session:
            config = resolve_llm_config(session, get_settings())
            assert config.api_key is not None
            assert config.api_key.get_secret_value() == "sk-env-key"
    finally:
        get_settings.cache_clear()


def test_settings_ui_explains_key_requirement(client: TestClient) -> None:
    page = client.get("/ui/settings")
    assert "非本机地址必填" in page.text
    _save_via_api(client)
    page = client.get("/ui/settings")
    assert "已保存，留空则沿用" in page.text


def test_llm_settings_ui_saves_and_keeps_key_masked(client: TestClient) -> None:
    response = client.post(
        "/ui/settings/llm",
        data={
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-1234",
            "model": "test-model",
            "timeout_s": "45",
            "consent": "yes",
            "next": "/ui/setup",
        },
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/setup?saved=1")
    page = client.get("/ui/settings")
    assert "sk-test-1234" not in page.text
    assert "****1234" not in page.text  # 密钥提示只在 API 中返回，不进入 HTML
    assert "test-model" in page.text


def test_llm_settings_test_endpoint_reports_failure(client: TestClient) -> None:
    """不可达端点返回 ok=False，而不是 500。"""
    response = client.post(
        "/settings/llm/test",
        json={
            "base_url": "https://127.0.0.1:9/v1",
            "api_key": "sk-x",
            "model": "m",
            "timeout_s": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["message"]


def test_resolver_prefers_db_and_falls_back_to_env(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("PAPERSTEWARD_LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MODEL", "env-model")
    monkeypatch.setenv("PAPERSTEWARD_LLM_API_KEY", "sk-env")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        factory = create_session_factory(migrated_engine)
        with session_scope(factory) as session:
            config = resolve_llm_config(session, settings)
            assert config.source == "env"
            assert config.authorized is False
            assert config.model == "env-model"

            save_llm_config(
                session,
                LLMSettingsPayload(
                    base_url="https://db.example.com/v1",
                    api_key="sk-db",
                    model="db-model",
                    consent=True,
                ),
                settings,
            )
        with session_scope(factory) as session:
            config = resolve_llm_config(session, settings)
            assert config.source == "db"
            assert config.authorized is True
            assert config.model == "db-model"
            assert config.ready is True
    finally:
        get_settings.cache_clear()
