from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from conftest import activate
from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.entities import (
    Bucket,
    NominationEvidence,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    SpecialNominationRecord,
    StaticScoreSnapshot,
    Summary,
)
from papersteward.core.enums import (
    BucketRole,
    BucketType,
    PaperStatus,
    SpecialBucketKind,
    SpecialNominationStatus,
    SummaryValidationStatus,
    TagRole,
)
from papersteward.core.errors import InvariantViolation
from papersteward.scoring.queue import create_snapshots, explain_score, read_queue
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import PaperRow, ScoreSnapshotRow
from papersteward.storage.repositories import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ReviewRepository,
    ScoreSnapshotRepository,
    SpecialNominationRepository,
    StaticScoreSnapshotRepository,
    SummaryRepository,
)

NOW = datetime(2026, 10, 22, 8, tzinfo=UTC)
DAY = NOW.date()


def seed(session, key="p", r=80, special=False):
    if BucketRepository(session).get("b-t01") is None:
        BucketRepository(session).add(Bucket("b-t01", "Topic", BucketType.TOPIC, tag_code="T01"))
        BucketRepository(session).add(
            Bucket("b-review", "Review", BucketType.SPECIAL, special_kind=SpecialBucketKind.REVIEW)
        )
    PaperRepository(session).add(
        Paper(key, key, status=PaperStatus.CLASSIFIED, first_seen_at=NOW, published_at=NOW)
    )
    PaperTagRepository(session).add(PaperTag(key, "T01", TagRole.PRIMARY, "1.0"))
    PaperBucketRepository(session).add(
        PaperBucket(key, "b-review" if special else "b-t01", BucketRole.FILING)
    )
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
            provenance=Provenance("fixture", "v1", NOW, "0" * 64),
        )
    )
    StaticScoreSnapshotRepository(session).append(
        StaticScoreSnapshot(
            paper_id=key,
            score_date=DAY,
            relevance=r,
            authority=50,
            extra_value=50,
            static_score=round(0.65 * r + 0.35 * 50, 1),
            confidence=0.5,
            config_version="1.0.0",
            profile_version="1.0",
            taxonomy_version="1.0",
            input_hash="0" * 64,
            created_at=NOW,
            breakdown_json={},
        )
    )


def test_gate_and_api_validation(client, migrated_engine):
    with session_scope(create_session_factory(migrated_engine)) as session:
        with pytest.raises(InvariantViolation, match="初始化确认"):
            create_snapshots(session, DAY, now=NOW)
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == 0
    response = client.get("/queue?date=2026-10-22", headers={"X-Request-ID": "queue-test"})
    assert response.status_code == 409 and response.json()["error"]["request_id"] == "queue-test"
    assert client.get("/queue?date=bad").status_code == 422
    assert client.get("/queue?date=2026-10-22&limit=0").status_code == 422


def test_snapshot_queue_explanation_and_idempotency(client, migrated_engine, monkeypatch):
    activate(client)
    monkeypatch.setattr("papersteward.api.queue.utc_now", lambda: NOW)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session)
        seed(session, "low", r=54.9)
        seed(session, "special", special=True)
        seed(session, "review")
        ReviewRepository(session).open("review", ["low_confidence"], {}, now=NOW)
        seed(session, "snoozed")
        session.get(PaperRow, "snoozed").status = "snoozed"
    assert client.get("/queue?date=2026-10-22").json()["total"] == 0
    first = client.post("/scores/snapshot", json={"score_date": str(DAY)})
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 2
    excluded = {x["paper_id"]: x for x in first.json()["excluded"]}
    assert excluded["special"] == {
        "paper_id": "special",
        "reasons": ["special_bucket"],
        "excluded_from_priority": True,
    }
    assert "below_relevance_gate" in excluded["low"]["reasons"]
    assert "review" not in excluded
    response = client.get("/queue?date=2026-10-22&limit=1")
    item = response.json()["items"][0]
    assert item["paper_id"] == "p" and response.json()["total"] == 2
    explanation = item["explanation"]
    assert set(explanation["components"]) == {"R", "A", "E", "F", "H", "U"}
    assert explanation["scores"]["U"] == explanation["dynamic"]["hunger"] == 50
    assert sum(c["weight"] for c in explanation["components"].values()) == pytest.approx(1)
    assert client.get("/scores/p/explain?date=2026-10-22").json() == explanation
    assert "scores" not in client.get("/scores/special/explain").json()
    assert client.get("/scores/absent/explain").status_code == 404
    second = client.post("/scores/snapshot", json={"score_date": str(DAY)}).json()
    assert second["created"] == 0 and second["existing"] == 2
    assert client.get("/queue?date=2026-10-22").json()["items"][0] == item
    with session_scope(factory) as session:
        assert PaperRepository(session).get("p").status is PaperStatus.QUEUED
        assert len(ScoreSnapshotRepository(session).history("p")) == 1
        assert len(StaticScoreSnapshotRepository(session).history("p")) == 1
        # 特殊MEMBERSHIP徽章不影响主题归属的排序。
        PaperBucketRepository(session).add(PaperBucket("p", "b-review", BucketRole.MEMBERSHIP))
        assert read_queue(session, DAY, now=NOW).total == 2


def test_atomic_failure_outer_rollback_and_history(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session, "a")
        seed(session, "b")
    original = ScoreSnapshotRepository.append

    def broken(self, snapshot):
        if snapshot.paper_id == "b":
            raise RuntimeError("injected")
        return original(self, snapshot)

    with session_scope(factory) as session:
        with monkeypatch.context() as m:
            m.setattr(ScoreSnapshotRepository, "append", broken)
            with pytest.raises(RuntimeError):
                create_snapshots(session, DAY, now=NOW)
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == 0
        assert PaperRepository(session).get("a").status is PaperStatus.CLASSIFIED
    with factory() as session:
        create_snapshots(session, DAY, now=NOW)
        session.rollback()
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(ScoreSnapshotRow)) == 0
        create_snapshots(session, DAY, now=NOW)
        tomorrow = DAY + timedelta(days=1)
        result = create_snapshots(session, tomorrow, now=NOW + timedelta(days=1))
        assert result.created == 2
        latest = explain_score(session, "a", now=NOW + timedelta(days=1))
        assert not latest.stability["reset"]
        assert explain_score(session, "a", now=NOW, day=DAY).score_date == DAY
        with pytest.raises(InvariantViolation, match="未来"):
            create_snapshots(session, tomorrow + timedelta(days=1), now=NOW)


def test_cli_reads_and_generates_without_network(client, migrated_engine, monkeypatch, capsys):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    monkeypatch.setattr("papersteward.cli.ranking.utc_now", lambda: NOW)
    get_settings.cache_clear()
    try:
        for args in (
            ["scores", "snapshot", "--date", str(DAY)],
            ["queue", "--date", str(DAY)],
            ["score", "explain", "p"],
        ):
            assert main([*args, "--json"]) == 0
            assert "score_date" in capsys.readouterr().out
        assert main(["queue", "--date", str(DAY), "--limit", "0"]) == 1
        assert "错误" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def _pending_nomination(paper_id: str) -> SpecialNominationRecord:
    return SpecialNominationRecord(
        id=f"nom-{paper_id}",
        paper_id=paper_id,
        kind=SpecialBucketKind.REVIEW,
        status=SpecialNominationStatus.PENDING,
        confidence=0.8,
        reason="合成提名",
        evidence=(NominationEvidence("evidence", True),),
        review_layers=("method", "evaluation"),
        classic_basis=(),
        bridge_memo=None,
        reasons=(),
        provenance=Provenance("fixture", "special_nomination/v1", NOW, "0" * 64),
        nominated_at=NOW,
    )


def test_pending_special_nomination_does_not_block_priority(client, migrated_engine, monkeypatch):
    activate(client)
    monkeypatch.setattr("papersteward.api.queue.utc_now", lambda: NOW)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session, "pending-special")
        SpecialNominationRepository(session).upsert(_pending_nomination("pending-special"))
    response = client.post("/scores/snapshot", json={"score_date": str(DAY)})
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    assert client.get("/queue?date=2026-10-22").json()["total"] == 1
    explanation = client.get("/scores/pending-special/explain").json()
    assert "components" in explanation


def test_quality_flags_low_confidence_and_analysis_evidence_do_not_block_queue(
    client, migrated_engine
):
    from papersteward.storage.models import PaperTagRow, SummaryRow
    from papersteward.storage.repositories import PaperAnalysisRepository

    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "quality")
        session.get(SummaryRow, "quality").validation_status = "needs_review"
        tag = session.scalars(select(PaperTagRow).where(PaperTagRow.paper_id == "quality")).one()
        tag.confidence = 0.4
        flags = ("classification:low_primary_confidence", "relevance:boundary")
        PaperAnalysisRepository(session).save(
            "quality",
            {
                "relevance": {"rubric": {"confidence": 0.63}},
                "classification": {"primary_tag": "T01"},
            },
            flags,
            now=NOW,
        )
        ReviewRepository(session).open("quality", ["legacy"], {}, now=NOW)
        session.flush()
        assert create_snapshots(session, DAY, now=NOW).created == 1
        queue = read_queue(session, DAY, now=NOW)
        assert queue.total == 1
        assert queue.items[0].explanation.components["R"].confidence == 0.63
        assert PaperAnalysisRepository(session).get("quality").quality_flags == flags


def test_completely_missing_summary_is_excluded(client, migrated_engine):
    from papersteward.storage.models import SummaryRow

    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "missing")
        session.delete(session.get(SummaryRow, "missing"))
        session.flush()
        report = create_snapshots(session, DAY, now=NOW)
        assert report.created == 0
        assert report.excluded[0].reasons == ["summary_unavailable"]


def test_quota_fill_exempts_only_relevance_gate_and_respects_floor(client, migrated_engine):
    """保底填充：按 R 降序补足、下限截断；特殊桶等其他淘汰理由不豁免。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "fill-54", r=54)
        seed(session, "fill-41", r=41)
        seed(session, "too-low", r=30)
        seed(session, "blocked", r=52, special=True)
        report = create_snapshots(session, DAY, now=NOW, min_queue_yield=4, relevance_floor=40.0)
        # 只有 below_relevance_gate 一个理由的论文可被填充：54 → 41，30 低于下限，
        # special_bucket 不豁免。
        assert report.quota_filled == 2
        assert report.created == 2
        excluded = {item.paper_id: item.reasons for item in report.excluded}
        assert excluded["too-low"] == ["below_relevance_gate"]
        assert excluded["blocked"] == ["special_bucket"]
        queued = read_queue(session, DAY, now=NOW)
        assert queued.total == 2
        assert [item.paper_id for item in queued.items] == ["fill-54", "fill-41"]
        assert all("quota_fill" in item.explanation.reasons for item in queued.items)


def test_quota_fill_shortfall_reported_when_all_below_floor(client, migrated_engine):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "low", r=39)
        report = create_snapshots(session, DAY, now=NOW, min_queue_yield=1, relevance_floor=40.0)
        assert report.quota_filled == 0
        assert report.created == 0
        assert report.excluded[0].reasons == ["below_relevance_gate"]
        assert read_queue(session, DAY, now=NOW).total == 0


def test_default_snapshot_ignores_quota_params(client, migrated_engine):
    """不传 min_queue_yield 的路径（周任务、手动刷新）行为不变。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "gate", r=50)
        report = create_snapshots(session, DAY, now=NOW)
        assert report.quota_filled == 0
        assert report.created == 0
        assert report.excluded[0].reasons == ["below_relevance_gate"]
