"""0016 迁移：旧阶段映射、CHECK 重建与已确认文档保留。"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from conftest import PROJECT_ROOT


def alembic_config(engine: Engine) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    connection = engine.connect()
    config.attributes["connection"] = connection
    return config


def upgrade_to(engine: Engine, revision: str) -> None:
    config = alembic_config(engine)
    command.upgrade(config, revision)
    config.attributes["connection"].close()


def downgrade_to(engine: Engine, revision: str) -> None:
    config = alembic_config(engine)
    command.downgrade(config, revision)
    config.attributes["connection"].close()


def seed_legacy(engine: Engine, stage: str, revision: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO onboarding (id, stage, revision, updated_at) "
                "VALUES (1, :stage, :revision, '2026-10-21 08:00:00')"
            ),
            {"stage": stage, "revision": revision},
        )
        connection.execute(
            text(
                "INSERT INTO research_profile "
                "(id, version, status, content_md, parsed_json, confirmed_at, change_log_json) "
                "VALUES ('legacy-profile', '1.0', 'confirmed', '# 旧档案', '{}', "
                "'2026-10-21 08:00:00', '[]')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO taxonomy "
                "(id, version, status, content_md, tags_json, confirmed_at) "
                "VALUES ('legacy-taxonomy', '1.0', 'confirmed', '# 旧分类法', '[]', "
                "'2026-10-21 08:00:00')"
            )
        )


def test_legacy_stages_map_to_setup_and_documents_survive(sqlite_engine: Engine) -> None:
    upgrade_to(sqlite_engine, "0015")
    seed_legacy(sqlite_engine, "calibration", 3)
    upgrade_to(sqlite_engine, "head")
    with sqlite_engine.connect() as connection:
        row = connection.execute(text("SELECT stage, revision FROM onboarding")).one()
        assert (row.stage, row.revision) == ("setup", 0)
        profile = connection.execute(
            text("SELECT content_md, status FROM research_profile WHERE id='legacy-profile'")
        ).one()
        taxonomy = connection.execute(
            text("SELECT content_md, status FROM taxonomy WHERE id='legacy-taxonomy'")
        ).one()
        assert (profile.content_md, profile.status) == ("# 旧档案", "confirmed")
        assert (taxonomy.content_md, taxonomy.status) == ("# 旧分类法", "confirmed")
        tables = {
            row[0]
            for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "intake_material" in tables and "onboarding_calibration" not in tables


def test_active_stage_keeps_workspace_usable(sqlite_engine: Engine) -> None:
    upgrade_to(sqlite_engine, "0015")
    seed_legacy(sqlite_engine, "active", 5)
    upgrade_to(sqlite_engine, "head")
    with sqlite_engine.connect() as connection:
        row = connection.execute(text("SELECT stage, revision FROM onboarding")).one()
    assert (row.stage, row.revision) == ("active", 2)


@pytest.mark.parametrize(
    "stage,revision", [("profile", 1), ("taxonomy", 2), ("calibration", 3), ("dry_run", 4)]
)
def test_every_unfinished_stage_resets_to_setup(
    sqlite_engine: Engine, stage: str, revision: int
) -> None:
    upgrade_to(sqlite_engine, "0015")
    seed_legacy(sqlite_engine, stage, revision)
    upgrade_to(sqlite_engine, "head")
    with sqlite_engine.connect() as connection:
        row = connection.execute(text("SELECT stage, revision FROM onboarding")).one()
    assert (row.stage, row.revision) == ("setup", 0)


def test_downgrade_restores_old_stage_and_tables(sqlite_engine: Engine) -> None:
    upgrade_to(sqlite_engine, "0015")
    seed_legacy(sqlite_engine, "active", 5)
    upgrade_to(sqlite_engine, "head")
    downgrade_to(sqlite_engine, "0015")
    with sqlite_engine.connect() as connection:
        row = connection.execute(text("SELECT stage, revision FROM onboarding")).one()
        tables = {
            item[0]
            for item in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert (row.stage, row.revision) == ("active", 5)
    assert "onboarding_calibration" in tables and "intake_material" not in tables


def test_upgraded_active_workspace_can_render_today(sqlite_engine: Engine) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from papersteward.api import create_app
    from papersteward.config import Settings
    from papersteward.core.entities import Config
    from papersteward.core.enums import AssignedBy, DocumentStatus
    from papersteward.documents import parse_profile, parse_taxonomy
    from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
    from papersteward.onboarding.parameters import default_runtime_parameters
    from papersteward.onboarding.setup import RUNTIME_KEY, default_scoring_config
    from papersteward.scoring.service import save_scoring_config
    from papersteward.storage import create_session_factory, session_scope
    from papersteward.storage.repositories import (
        ConfigRepository,
        ProfileRepository,
        TaxonomyRepository,
    )

    upgrade_to(sqlite_engine, "0015")
    seed_legacy(sqlite_engine, "active", 5)
    upgrade_to(sqlite_engine, "head")

    now = datetime(2026, 10, 23, tzinfo=UTC)
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    profile = replace(
        parse_profile((fixtures / "profiles/basic.md").read_text()),
        status=DocumentStatus.CONFIRMED,
        confirmed_at=now,
    )
    taxonomy = replace(
        parse_taxonomy((fixtures / "taxonomies/onboarding.md").read_text()),
        status=DocumentStatus.CONFIRMED,
        confirmed_at=now,
    )
    factory = create_session_factory(sqlite_engine)
    settings = Settings(_env_file=None)
    with session_scope(factory) as session:
        ProfileRepository(session).add(profile)
        TaxonomyRepository(session).add(taxonomy)
        save_scoring_config(session, default_scoring_config(), updated_at=now)
        runtime = default_runtime_parameters(
            timezone="Asia/Shanghai", target_shares={f"T{index:02}": 0.2 for index in range(1, 6)}
        )
        ConfigRepository(session).add(
            Config(
                key=RUNTIME_KEY,
                version="v-legacy",
                value_json=runtime.model_dump(mode="json"),
                updated_by=AssignedBy.USER,
                updated_at=now,
            )
        )
        save_llm_config(
            session,
            LLMSettingsPayload(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model="test-model",
                consent=True,
            ),
            settings,
        )
    with TestClient(create_app(settings=settings, engine=sqlite_engine)) as client:
        assert client.get("/onboarding").json() == {"stage": "active", "revision": 2}
        page = client.get("/ui/today")
        assert page.status_code == 200, page.text
        assert "挖掘新论文" in page.text
        assert 'value="long-document retrieval"' in page.text
