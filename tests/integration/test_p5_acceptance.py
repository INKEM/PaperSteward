import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from test_priority_queue import DAY, NOW, seed

from conftest import activate
from papersteward.core.entities import Rating
from papersteward.core.errors import InvariantViolation
from papersteward.scoring.queue import create_snapshots, explain_score, read_queue
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import (
    EventRow,
    PaperRow,
    PaperTagRow,
    ScoreSnapshotRow,
    StaticScoreSnapshotRow,
    SummaryRow,
)
from papersteward.storage.repositories import RatingRepository, ScoreSnapshotRepository

GOLDEN = json.loads(Path("tests/fixtures/scoring/priority_golden.json").read_text())


def test_snapshot_golden_and_archived_score_is_frozen(client, migrated_engine):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session)
        create_snapshots(session, DAY, now=NOW)
        detail = explain_score(session, "p", now=NOW)
        expected = GOLDEN["snapshot"]
        assert {k: c.score for k, c in detail.components.items()} == {
            k: expected[k] for k in ("R", "A", "E", "F", "H", "U")
        }
        assert {k: detail.scores[k] for k in ("S", "D", "U", "P", "rank_score")} == {
            k: expected[k] for k in ("S", "D", "U", "P", "rank_score")
        }
        before = ScoreSnapshotRepository(session).history("p")
        session.get(PaperRow, "p").status = "archived"  # 模拟P6输入，不新增归档用例。
        session.flush()
        assert (
            create_snapshots(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1)).created
            == 0
        )
        assert read_queue(session, DAY, now=NOW).total == 0
        assert explain_score(session, "p", now=NOW).model_dump() == detail.model_dump()
        assert ScoreSnapshotRepository(session).history("p") == before


def test_pause_allows_reading_but_blocks_execution(client, migrated_engine):
    activate(client, paused=True)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session)
        assert read_queue(session, DAY, now=NOW).total == 0
        with pytest.raises(InvariantViolation, match="暂停"):
            create_snapshots(session, DAY, now=NOW)
        assert list(session.scalars(select(ScoreSnapshotRow))) == []


def test_invalid_summary_and_static_version_are_excluded(client, migrated_engine):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "summary")
        seed(session, "version")
        session.get(SummaryRow, "summary").validation_status = "needs_review"
        session.get(StaticScoreSnapshotRow, ("version", DAY)).config_version = "different"
        session.flush()
        result = create_snapshots(session, DAY, now=NOW)
        reasons = {x.paper_id: x.reasons for x in result.excluded}
        assert "summary_not_passed" in reasons["summary"]
        assert "static_version_mismatch" in reasons["version"]
        assert result.created == 0


def test_same_day_drift_and_out_of_order_rejected(client, migrated_engine):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session)
        # 首次在次日生成；补算前一天不能改变已建立的EMA历史。
        create_snapshots(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        with pytest.raises(InvariantViolation, match="倒序"):
            create_snapshots(session, DAY, now=NOW)
        session.get(StaticScoreSnapshotRow, ("p", DAY)).input_hash = "1" * 64
        session.flush()
        with pytest.raises(InvariantViolation, match="依据已变化"):
            create_snapshots(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        assert len(ScoreSnapshotRepository(session).history("p")) == 1


def test_special_reading_and_weekly_rating_revision_without_notes(client, migrated_engine):
    activate(client)
    statements = []
    event.listen(
        migrated_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: statements.append(statement),
    )
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session)
        seed(session, "special-read", special=True)
        session.get(PaperRow, "special-read").status = "done"
        session.add(
            EventRow(
                id="done-special",
                paper_id="special-read",
                event_type="paper.done",
                created_at=NOW - timedelta(days=14),
                payload_json={},
            )
        )
        RatingRepository(session).add(
            Rating(
                "special-read",
                score=5,
                note="PRIVATE-NOTE",
                tags_at_read=("T01",),
                created_at=NOW - timedelta(days=14),
            )
        )
        RatingRepository(session).add(
            Rating(
                "special-read",
                score=1,
                note="PRIVATE-REVISION",
                tags_at_read=("T01",),
                created_at=NOW - timedelta(days=1),
            )
        )
        session.flush()
        statements.clear()
        create_snapshots(session, DAY, now=NOW)
        detail = explain_score(session, "p", now=NOW)
        assert detail.dynamic["total_completed"] == 1 and detail.dynamic["hunger"] == 10
        assert detail.quasi["ratings_count"] == 1 and detail.quasi["preference"] == 50.5
        assert "PRIVATE" not in detail.model_dump_json()
        rating_selects = [s for s in statements if "FROM rating" in s]
        assert rating_selects and all("rating.note" not in s for s in rating_selects)


def test_low_confidence_and_stale_tag_versions_cannot_bypass_quality_gate(client, migrated_engine):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "low-confidence")
        seed(session, "stale-tag")
        session.get(PaperTagRow, ("low-confidence", "T01", "primary")).confidence = 0.4
        session.get(PaperTagRow, ("stale-tag", "T01", "primary")).taxonomy_version = "obsolete"
        session.flush()
        result = create_snapshots(session, DAY, now=NOW)
        reasons = {x.paper_id: x.reasons for x in result.excluded}
        assert "unapproved_low_confidence_classification" in reasons["low-confidence"]
        assert "classification_version_mismatch" in reasons["stale-tag"]
        assert result.created == 0
