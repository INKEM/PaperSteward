"""导入已读论文的归档：archive_imported 领域行为与导入快照的读取兼容。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import NOW, activate_workspace
from papersteward.core.entities import ArchiveImportInput, ArchiveSnapshot, Paper, Rating
from papersteward.core.enums import AssignedBy, EventType, PaperStatus, TagRole
from papersteward.core.errors import InvariantViolation
from papersteward.pipeline.context import bound_context
from papersteward.reading.archive import archive_imported, list_archives, read_archive
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import EventRow, PaperTagRow
from papersteward.storage.repositories import (
    ArchiveSnapshotRepository,
    PaperRepository,
    RatingRepository,
)

IMPORT_NOW = NOW + timedelta(hours=2)


@pytest.fixture
def activated_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    activate_workspace(migrated_engine)
    return create_session_factory(migrated_engine)


def seed_import(session: Session, paper_id: str, **paper_kwargs) -> None:
    PaperRepository(session).add(
        Paper(
            paper_id,
            paper_kwargs.pop("title", "外部已读论文"),
            status=PaperStatus.DONE,
            first_seen_at=NOW,
            published_at=NOW,
            **paper_kwargs,
        )
    )
    RatingRepository(session).add(Rating(paper_id, 4, "系统外读完", created_at=NOW))


def test_import_archive_without_tag(activated_factory: sessionmaker[Session]) -> None:
    with session_scope(activated_factory) as session:
        seed_import(session, "imp-a", doi="10.5555/import.1")
        archive_imported(
            session,
            "imp-a",
            now=IMPORT_NOW,
            import_input=ArchiveImportInput(identifier_input="10.5555/import.1"),
        )

    with session_scope(activated_factory) as session:
        paper = PaperRepository(session).get("imp-a")
        assert paper is not None and paper.status is PaperStatus.ARCHIVED
        snapshot = ArchiveSnapshotRepository(session).get("imp-a")
        assert snapshot is not None
        assert snapshot.primary_tag is None
        assert snapshot.filing_bucket_id is None
        assert snapshot.rating == 4
        assert snapshot.note == "系统外读完"
        content = dict(snapshot.content_json)
        assert set(content) == {
            "origin",
            "identifier_input",
            "resolve",
            "metadata",
            "tags",
            "initial_feedback",
        }
        assert content["origin"] == "import"
        assert content["identifier_input"] == "10.5555/import.1"
        assert content["resolve"] is None
        assert content["metadata"]["title"] == "外部已读论文"
        assert content["metadata"]["doi"] == "10.5555/import.1"
        assert content["tags"] == []
        assert content["initial_feedback"] == {"score": 4, "note": "系统外读完"}
        _, binding = bound_context(session, IMPORT_NOW, action="archive")
        assert snapshot.profile_version == binding["profile_version"]
        assert snapshot.taxonomy_version == binding["taxonomy_version"]
        assert snapshot.config_version == binding["scoring_version"]
        events = list(session.scalars(select(EventRow).where(EventRow.paper_id == "imp-a")))
        assert EventType.PAPER_ARCHIVED.value in [event.event_type for event in events]
        done = [event for event in events if event.event_type == EventType.PAPER_DONE.value]
        assert [event.payload_json for event in done] == [{"origin": "import"}]
        assert not list(session.scalars(select(PaperTagRow).where(PaperTagRow.paper_id == "imp-a")))
        record = read_archive(session, "imp-a")
        assert record.primary_tag is None
        assert record.filing_bucket_id is None
        assert record.initial_score == 4
        assert record.content["origin"] == "import"


def test_import_archive_with_primary_tag(activated_factory: sessionmaker[Session]) -> None:
    with session_scope(activated_factory) as session:
        seed_import(session, "imp-b")
        archive_imported(
            session,
            "imp-b",
            now=IMPORT_NOW,
            import_input=ArchiveImportInput(primary_tag="T01"),
        )

    with session_scope(activated_factory) as session:
        tags = list(session.scalars(select(PaperTagRow).where(PaperTagRow.paper_id == "imp-b")))
        assert len(tags) == 1
        assert tags[0].tag_code == "T01"
        assert tags[0].role == TagRole.PRIMARY.value
        assert tags[0].assigned_by == AssignedBy.USER.value
        # 用户标注不带模型溯源（provenance_all_or_none 的全空侧）。
        assert tags[0].provenance_model is None
        snapshot = ArchiveSnapshotRepository(session).get("imp-b")
        assert snapshot is not None and snapshot.primary_tag == "T01"
        content = dict(snapshot.content_json)
        assert len(content["tags"]) == 1
        assert content["tags"][0]["tag_code"] == "T01"
        assert content["tags"][0]["assigned_by"] == "user"
        record = read_archive(session, "imp-b")
        assert record.primary_tag == "T01"


def test_import_archive_rejects_non_done(activated_factory: sessionmaker[Session]) -> None:
    with session_scope(activated_factory) as session:
        PaperRepository(session).add(
            Paper("imp-c", "未读完", status=PaperStatus.CLASSIFIED, first_seen_at=NOW)
        )
        with pytest.raises(InvariantViolation, match="导入归档仅接受已读完"):
            archive_imported(session, "imp-c", now=IMPORT_NOW, import_input=ArchiveImportInput())


def test_import_archive_rejects_existing_snapshot(
    activated_factory: sessionmaker[Session],
) -> None:
    with session_scope(activated_factory) as session:
        seed_import(session, "imp-d")
        ArchiveSnapshotRepository(session).add(
            ArchiveSnapshot(
                paper_id="imp-d",
                archived_at=NOW,
                primary_tag=None,
                filing_bucket_id=None,
                profile_version="1.0",
                taxonomy_version="1.0",
                config_version="1.0.0",
                content_hash="a" * 64,
                content_json={},
            )
        )
        with pytest.raises(InvariantViolation, match="归档已存在且不可覆盖"):
            archive_imported(session, "imp-d", now=IMPORT_NOW, import_input=ArchiveImportInput())


def test_import_archive_requires_rating_with_score(
    activated_factory: sessionmaker[Session],
) -> None:
    with session_scope(activated_factory) as session:
        PaperRepository(session).add(
            Paper("imp-e", "无评分", status=PaperStatus.DONE, first_seen_at=NOW)
        )
        with pytest.raises(InvariantViolation, match="导入必须附 1–5 的阅读评分"):
            archive_imported(session, "imp-e", now=IMPORT_NOW, import_input=ArchiveImportInput())
        RatingRepository(session).add(Rating("imp-e", None, "未打分", created_at=NOW))
        with pytest.raises(InvariantViolation, match="导入必须附 1–5 的阅读评分"):
            archive_imported(session, "imp-e", now=IMPORT_NOW, import_input=ArchiveImportInput())


def test_list_archives_handles_imported_without_tag(
    activated_factory: sessionmaker[Session],
) -> None:
    with session_scope(activated_factory) as session:
        seed_import(session, "imp-f", title="Frozen Import Paper")
        archive_imported(session, "imp-f", now=IMPORT_NOW, import_input=ArchiveImportInput())

    with session_scope(activated_factory) as session:
        result = list_archives(session, query="frozen import")
        assert result["total"] == 1
        assert result["items"][0].primary_tag is None
        assert result["items"][0].content["origin"] == "import"
        # 无主标签/无归属桶的导入论文不误命中标签与桶筛选。
        assert list_archives(session, tag="T01")["total"] == 0
        assert list_archives(session, bucket="b-missing")["total"] == 0
        # 评分筛选可命中导入论文（IMP-06）。
        assert list_archives(session, min_score=4)["total"] == 1
        assert list_archives(session, min_score=5)["total"] == 0
