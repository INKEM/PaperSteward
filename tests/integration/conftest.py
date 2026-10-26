"""P0-S2 集成测试 fixture：所有数据库都落在 pytest 临时目录。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from papersteward.storage import Base, create_db_engine, create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """空 SQLite 文件库；不自动建表，迁移测试可从零开始。"""
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def schema_engine(sqlite_engine: Engine) -> Engine:
    """已按当前模型建表的 SQLite 文件库，供仓储测试使用。"""
    Base.metadata.create_all(sqlite_engine)
    return sqlite_engine


@pytest.fixture
def migrated_engine(sqlite_engine: Engine) -> Engine:
    """已执行 Alembic upgrade head 的临时 SQLite 文件库。"""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    with sqlite_engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return sqlite_engine


@pytest.fixture
def session_factory(schema_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(schema_engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# —— 新引导流程共享夹具 ——————————————————————————————————————————————

NOW = datetime(2026, 10, 21, 8, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def activate_workspace(
    engine: Engine,
    *,
    now: datetime = NOW,
    runtime_overrides: dict[str, object] | None = None,
) -> dict[str, str]:
    """绕过 LLM 直接激活工作区：草案入库 → DRAFT → 确认。返回草案标识。"""
    from papersteward.onboarding.intake import ensure_draft_stage
    from papersteward.onboarding.profile import save_draft
    from papersteward.onboarding.setup import RUNTIME_KEY, confirm_setup
    from papersteward.onboarding.taxonomy import save_taxonomy_draft
    from papersteward.storage.repositories import ConfigRepository

    profile_md = (FIXTURES / "profiles/basic.md").read_text(encoding="utf-8")
    taxonomy_md = (FIXTURES / "taxonomies/onboarding.md").read_text(encoding="utf-8")
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        ensure_draft_stage(session, occurred_at=now)
        profile = save_draft(session, profile_md, "demo-profile")
        taxonomy = save_taxonomy_draft(session, taxonomy_md, "onboarding-taxonomy")
        state = confirm_setup(
            session,
            confirmed_by="fixture-user",
            change_summary="测试激活",
            expected_revision=1,
            profile_id=profile["id"],
            profile_version=profile["version"],
            profile_hash=profile["content_hash"],
            taxonomy_id=taxonomy["id"],
            taxonomy_version=taxonomy["version"],
            taxonomy_hash=taxonomy["content_hash"],
            timezone="Asia/Shanghai",
            event_id="evt-onboarding-fixture",
            occurred_at=now,
        )
        assert state.stage.value == "active"
        if runtime_overrides:
            from uuid import uuid4

            from papersteward.core.entities import Config
            from papersteward.core.enums import AssignedBy

            row = ConfigRepository(session).latest(RUNTIME_KEY)
            assert row is not None
            from datetime import timedelta

            ConfigRepository(session).add(
                Config(
                    key=RUNTIME_KEY,
                    version=f"v-{uuid4().hex[:12]}",
                    value_json={**dict(row.value_json), **runtime_overrides},
                    updated_by=AssignedBy.USER,
                    updated_at=row.updated_at + timedelta(seconds=1),
                )
            )
    return {
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "taxonomy_id": taxonomy["id"],
        "taxonomy_version": taxonomy["version"],
    }


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """默认 API 测试客户端；事件时间使用真实时钟以保证追加顺序。"""
    from fastapi.testclient import TestClient

    from papersteward.api import create_app

    with TestClient(create_app(engine=migrated_engine)) as test_client:
        yield test_client


def activate(client: TestClient, **overrides: object) -> dict[str, str]:
    """兼容旧签名的激活入口：``activate(client, **runtime_overrides)``。"""
    return activate_workspace(client.app.state.engine, runtime_overrides=overrides or None)


@pytest.fixture
def insights_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    """已激活工作区的会话工厂；阅读回顾（R1-01/02）集成测试共用。"""
    activate_workspace(migrated_engine)
    return create_session_factory(migrated_engine)
