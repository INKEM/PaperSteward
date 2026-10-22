from __future__ import annotations

import re
from pathlib import Path

import pytest

from papersteward.cli.main import main
from papersteward.core.enums import (
    BucketType,
    DocumentStatus,
    EventType,
    PaperStatus,
    SpecialBucketKind,
)
from papersteward.llm import FakeLLM, LLMProvider, default_registry
from papersteward.schemas import SCHEMA_MODELS, schema_for
from papersteward.storage import Base

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]


def test_paper_and_document_status_freeze() -> None:
    assert [status.value for status in PaperStatus] == [
        "discovered",
        "deduped",
        "rejected",
        "screened",
        "needs_review",
        "enriched",
        "summarized",
        "classified",
        "shelved",
        "scored",
        "queued",
        "snoozed",
        "pushed",
        "reading",
        "done",
        "archived",
    ]
    assert [status.value for status in DocumentStatus] == ["draft", "confirmed", "archived"]
    assert {kind.value for kind in BucketType} == {"topic", "special"}
    assert {kind.value for kind in SpecialBucketKind} == {"review", "classic", "distant"}
    assert EventType.PAPER_DONE == "paper.done"
    assert EventType.PROFILE_CONFIRMED == "profile.confirmed"
    assert EventType.CONFIG_UPDATED == "config.updated"


def test_database_table_and_constraint_freeze() -> None:
    tables = set(Base.metadata.tables)
    # P1 表契约保留；P2 起只允许通过新迁移追加表。
    assert tables >= {
        "archive_snapshot",
        "bucket",
        "config",
        "event",
        "paper",
        "paper_bucket",
        "paper_tag",
        "rating",
        "research_profile",
        "run",
        "score_snapshot",
        "summary",
        "tag",
        "taxonomy",
    }
    assert "uq_paper_tag_primary" in {
        index.name for index in Base.metadata.tables["paper_tag"].indexes
    }
    assert "uq_paper_bucket_filing" in {
        index.name for index in Base.metadata.tables["paper_bucket"].indexes
    }
    assert {column.name for column in Base.metadata.tables["score_snapshot"].primary_key} == {
        "paper_id",
        "score_date",
    }


def test_migration_revision_freeze() -> None:
    versions = ROOT / "migrations" / "versions"
    revision_files = sorted(versions.glob("*.py"))
    assert [path.name for path in revision_files[:2]] == [
        "0001_initial_schema.py",
        "0002_domain_schema.py",
    ]
    latest = revision_files[1].read_text(encoding="utf-8")
    assert re.search(r'revision: str = "0002"', latest)
    assert re.search(r'down_revision: str \| Sequence\[str\] \| None = "0001"', latest)


def test_schema_registry_freeze() -> None:
    assert set(SCHEMA_MODELS) == {
        "classification",
        "relevance",
        "summary",
        "scoring",
        "special_scoring",
        "event",
    }
    assert set(schema_for("classification")["required"]) == {
        "primary_tag",
        "primary_confidence",
    }
    assert set(schema_for("summary")["required"]) == {
        "summary_100",
        "what",
        "how",
        "finding",
        "limitation",
        "evidence",
    }
    assert set(schema_for("scoring")["required"]) == {
        "paper_id",
        "date",
        "profile_version",
        "taxonomy_version",
        "config_version",
        "static",
        "dynamic",
        "quasi",
        "priority",
        "explanation",
    }
    assert set(schema_for("special_scoring")["required"]) == {
        "paper_id",
        "date",
        "profile_version",
        "taxonomy_version",
        "config_version",
        "static",
        "dynamic",
        "quasi",
        "tier",
        "explanation",
    }
    assert "type" in schema_for("event")["properties"]


def test_prompt_registry_and_fake_provider_freeze() -> None:
    registry = default_registry()
    assert registry.names() == ("classification", "summary", "relevance", "repair")
    assert registry.get("classification").version == "classification/v1"
    assert registry.get("summary").schema == "summary"
    assert registry.get("relevance").schema == "relevance"
    assert registry.get("repair").schema is None

    provider = FakeLLM()
    assert isinstance(provider, LLMProvider)


def test_cli_command_surface_freeze(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for command in (
        "doctor",
        "demo",
        "profile",
        "taxonomy",
        "doc",
        "config",
        "schema",
        "llm",
    ):
        assert command in output
