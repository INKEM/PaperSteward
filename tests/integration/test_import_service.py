"""导入服务编排、去重守卫与 /papers/import JSON 端点。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import NOW, activate, activate_workspace
from papersteward.core.entities import Paper
from papersteward.core.enums import EventType, PaperStatus
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.reading.import_service import guard_duplicate, import_read_paper
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    EventRow,
    PaperRow,
    PaperSourceRow,
    RatingRow,
)
from papersteward.storage.repositories import ArchiveSnapshotRepository

IMPORT_NOW = NOW + timedelta(hours=2)


@pytest.fixture
def activated_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    activate_workspace(migrated_engine)
    return create_session_factory(migrated_engine)


def _request(**overrides: object) -> ImportReadPaperInput:
    values: dict[str, object] = {
        "title": "外部已读论文",
        "score": 4,
        "note": "系统外读完",
        "doi": "10.5555/import.1",
    }
    values.update(overrides)
    return ImportReadPaperInput.model_validate(values)


def _seed_paper(session: Session, paper_id: str, title: str, doi: str) -> None:
    from papersteward.storage.repositories import PaperRepository

    PaperRepository(session).add(
        Paper(paper_id, title, status=PaperStatus.CLASSIFIED, first_seen_at=NOW, doi=doi)
    )


def test_import_read_paper_happy_path(activated_factory: sessionmaker[Session]) -> None:
    with session_scope(activated_factory) as session:
        paper_id = import_read_paper(
            session,
            _request(primary_tag="T01", identifier_input="10.5555/import.1"),
            now=IMPORT_NOW,
        )

    with session_scope(activated_factory) as session:
        paper = session.get(PaperRow, paper_id)
        assert paper is not None
        assert paper.status == PaperStatus.ARCHIVED.value
        assert paper.doi == "10.5555/import.1"
        source = session.scalar(select(PaperSourceRow).where(PaperSourceRow.paper_id == paper_id))
        assert source is not None
        assert source.source == "manual_import"
        assert source.source_id == "10.5555/import.1"
        assert source.candidate_json["provenance"]["source"] == "manual_import"
        rating = session.scalar(select(RatingRow).where(RatingRow.paper_id == paper_id))
        assert rating is not None
        assert rating.score == 4
        assert rating.tags_at_read_json == ["T01"]
        assert rating.read_mode is None
        snapshot = session.get(ArchiveSnapshotRow, paper_id)
        assert snapshot is not None
        assert snapshot.primary_tag == "T01"
        assert snapshot.filing_bucket_id is None
        content = dict(snapshot.content_json)
        assert content["origin"] == "import"
        assert content["identifier_input"] == "10.5555/import.1"
        assert content["resolve"] is None
        assert content["initial_feedback"] == {"score": 4, "note": "系统外读完"}
        events = list(session.scalars(select(EventRow).where(EventRow.paper_id == paper_id)))
        assert EventType.PAPER_ARCHIVED.value in [event.event_type for event in events]
        done = [event for event in events if event.event_type == EventType.PAPER_DONE.value]
        assert [event.payload_json for event in done] == [{"origin": "import"}]


def test_import_read_paper_rejects_malformed_identifier(
    activated_factory: sessionmaker[Session],
) -> None:
    with session_scope(activated_factory) as session:
        with pytest.raises(InvalidRequestError, match="DOI"):
            import_read_paper(session, _request(doi="not-a-doi"), now=IMPORT_NOW)
        with pytest.raises(InvalidRequestError, match="arXiv ID"):
            import_read_paper(session, _request(doi=None, arxiv_id="bad id!"), now=IMPORT_NOW)
        assert session.scalars(select(PaperRow)).first() is None


def test_guard_duplicate_blocks_archived_with_date(
    activated_factory: sessionmaker[Session],
) -> None:
    with session_scope(activated_factory) as session:
        import_read_paper(session, _request(), now=IMPORT_NOW)

    with session_scope(activated_factory) as session:
        with pytest.raises(ConflictError) as exc:
            import_read_paper(session, _request(title="换个标题也一样"), now=IMPORT_NOW)
        message = exc.value.message
        assert "该论文已归档" in message
        assert IMPORT_NOW.date().isoformat() in message
        assert exc.value.details is not None
        assert exc.value.details["detail_url"].startswith("/ui/papers/")


def test_guard_duplicate_other_status_and_title_key(
    activated_factory: sessionmaker[Session],
) -> None:
    with session_scope(activated_factory) as session:
        _seed_paper(session, "p-seed", "已存在的论文", doi="10.5555/existing.1")
        # DOI 键命中
        with pytest.raises(ConflictError, match="当前状态：已归类"):
            guard_duplicate(session, title="无关标题", doi="10.5555/existing.1")
        # title 规范化键命中（标点与多余空白被规范化抹平）
        with pytest.raises(ConflictError, match="当前状态：已归类"):
            guard_duplicate(session, title="已存在的论文!", doi="10.9999/other")
        # 无命中放行
        guard_duplicate(session, title="全新论文", doi="10.5555/fresh.1")


def test_import_read_paper_is_atomic_on_midway_failure(
    activated_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: object, snapshot: object) -> None:
        raise RuntimeError("模拟快照写入失败")

    monkeypatch.setattr(ArchiveSnapshotRepository, "add", boom)
    with (
        session_scope(activated_factory) as session,
        pytest.raises(RuntimeError, match="模拟快照写入失败"),
    ):
        import_read_paper(session, _request(), now=IMPORT_NOW)

    with session_scope(activated_factory) as session:
        assert session.scalars(select(PaperRow)).first() is None
        assert session.scalars(select(RatingRow)).first() is None
        assert session.scalars(select(PaperSourceRow)).first() is None
        assert session.scalars(select(ArchiveSnapshotRow)).first() is None


def test_import_api_happy_duplicate_and_validation(client, migrated_engine) -> None:
    activate(client)
    body = {"title": "API 导入论文", "score": 5, "doi": "10.5555/api.1"}
    first = client.post("/papers/import", json=body)
    assert first.status_code == 201
    data = first.json()
    assert data["paper_id"]
    assert data["detail_url"] == f"/ui/papers/{data['paper_id']}"

    duplicate = client.post("/papers/import", json=body)
    assert duplicate.status_code == 409
    assert "该论文已归档" in duplicate.json()["error"]["message"]

    invalid_score = client.post("/papers/import", json={**body, "doi": "10.5555/api.2", "score": 6})
    assert invalid_score.status_code == 422

    malformed = client.post("/papers/import", json={"title": "x 论文", "score": 3, "doi": "oops"})
    assert malformed.status_code == 400
    assert "DOI" in malformed.json()["error"]["message"]


def test_import_api_requires_active_workspace(client, migrated_engine) -> None:
    response = client.post("/papers/import", json={"title": "未激活导入", "score": 3})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
