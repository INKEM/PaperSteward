from __future__ import annotations

import json
from datetime import timedelta

import pytest
from test_special_shelf import NOW, PAPER, _process, _setup

from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    PaperStatus,
    ScoreTier,
    SpecialBucketKind,
)
from papersteward.delivery.special import (
    load_special_candidates,
    load_special_quotas,
    plan_special_rotation,
)
from papersteward.errors import ConflictError
from papersteward.pipeline.shelf import accept_nomination, ensure_special_buckets
from papersteward.scoring.special import (
    DEFAULT_SPECIAL_QUOTAS,
    RUBRIC_VERSION,
    override_special_tier,
)
from papersteward.storage import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    SpecialNominationRepository,
    SpecialTierRepository,
    create_session_factory,
    session_scope,
)


def _accept(session):
    return accept_nomination(
        session, PAPER.id, reviewed_by="fixture-user", note="合成审核", now=NOW
    )


def test_accept_assesses_tier_and_persists_evidence(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        decision = _accept(session)
        assert decision.tier is not None
        assert decision.tier.kind is SpecialBucketKind.REVIEW
        assert decision.tier.tier is ScoreTier.MEDIUM
        assert decision.tier.assigned_by is AssignedBy.AGENT
        assert decision.tier.nomination_id == decision.nomination.id
        assert decision.tier.rubric_version == RUBRIC_VERSION
        assert decision.tier.evidence

        latest = SpecialTierRepository(session).latest(PAPER.id)
        assert latest is not None
        assert latest.id == decision.tier.id
        assert set(latest.dimensions) == {
            "cross_layer_depth",
            "coverage",
            "authority",
            "clarity",
            "timeliness",
        }
        assert latest.breakdown_json["inputs"]["evidence_valid"] == 1
        assert len(SpecialTierRepository(session).history(PAPER.id)) == 1


def test_manual_tier_override_appends_history_and_requires_note(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        _accept(session)
        original = SpecialTierRepository(session).latest(PAPER.id)
        assert original is not None
        with pytest.raises(ConflictError, match="备注"):
            override_special_tier(
                session,
                PAPER.id,
                tier=ScoreTier.HIGH,
                reviewed_by="fixture-user",
                note=" ",
                now=NOW + timedelta(hours=1),
            )
        overridden = override_special_tier(
            session,
            PAPER.id,
            tier=ScoreTier.HIGH,
            reviewed_by="fixture-user",
            note="人工确认跨层价值",
            now=NOW + timedelta(hours=1),
        )
        assert overridden.tier is ScoreTier.HIGH
        assert overridden.assigned_by is AssignedBy.USER
        assert overridden.note == "人工确认跨层价值"
        assert overridden.value == original.value
        assert "user_override" in overridden.reasons
        history = SpecialTierRepository(session).history(PAPER.id)
        assert [item.tier for item in history] == [ScoreTier.MEDIUM, ScoreTier.HIGH]
        assert history[0].assigned_by is AssignedBy.AGENT
        with pytest.raises(ConflictError, match="相同"):
            override_special_tier(
                session,
                PAPER.id,
                tier=ScoreTier.HIGH,
                reviewed_by="fixture-user",
                note="重复",
                now=NOW + timedelta(hours=2),
            )


def test_accept_rolls_back_when_tier_assessment_fails(migrated_engine, monkeypatch):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)

    def fail(*args, **kwargs):
        raise RuntimeError("injected tier failure")

    monkeypatch.setattr("papersteward.pipeline.shelf.assess_special", fail)
    with pytest.raises(RuntimeError, match="injected"), session_scope(factory) as session:
        accept_nomination(session, PAPER.id, reviewed_by="fixture-user", note="合成审核", now=NOW)
    with session_scope(factory) as session:
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.CLASSIFIED
        filing = [
            item
            for item in PaperBucketRepository(session).list_for_paper(PAPER.id)
            if item.role is BucketRole.FILING
        ][0]
        assert filing.bucket_id == "b-t02"
        pending, total = SpecialNominationRepository(session).list_pending()
        assert total == 2
        assert SpecialTierRepository(session).latest(PAPER.id) is None


def test_special_buckets_get_default_weekly_quotas(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        buckets = ensure_special_buckets(session)
        assert {kind: bucket.quota for kind, bucket in buckets.items()} == {
            SpecialBucketKind.REVIEW: 3,
            SpecialBucketKind.DISTANT: 2,
            SpecialBucketKind.CLASSIC: 1,
        }
        assert load_special_quotas(session) == dict(DEFAULT_SPECIAL_QUOTAS)
        # 已有桶保留人工配额，未配置的补默认值。
        repository = BucketRepository(session)
        repository.set_quota("b-special-distant", 0)
        assert load_special_quotas(session)[SpecialBucketKind.DISTANT] == 0


def test_load_candidates_and_plan_one_slot(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        _accept(session)
        candidates = load_special_candidates(session, as_of=NOW.date())
        assert [candidate.kind for candidate in candidates] == [SpecialBucketKind.REVIEW]
        plan = plan_special_rotation(
            candidates,
            day=NOW.date(),
            quotas=load_special_quotas(session),
        )
        assert len(plan.slots) == 1
        assert plan.slots[0].kind is SpecialBucketKind.REVIEW
        assert plan.slots[0].tier is ScoreTier.MEDIUM
        assert plan.used[SpecialBucketKind.REVIEW] == 1
        assert "empty:classic" in plan.skipped
        assert "empty:distant" in plan.skipped


def test_shelf_cli_assess_tier_and_plan(migrated_engine, monkeypatch, capsys):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        _accept(session)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    clock = iter([NOW + timedelta(hours=2), NOW + timedelta(hours=3)])
    monkeypatch.setattr("papersteward.scoring.special.utc_now", lambda: next(clock))
    get_settings.cache_clear()
    try:
        assert main(["shelf", "assess", PAPER.id, "--json"]) == 0
        assessed = json.loads(capsys.readouterr().out)
        assert assessed["tier"] == "medium"
        assert assessed["rubric_version"] == RUBRIC_VERSION

        assert (
            main(
                [
                    "shelf",
                    "tier",
                    PAPER.id,
                    "--tier",
                    "high",
                    "--by",
                    "fixture-user",
                    "--note",
                    "人工确认",
                    "--json",
                ]
            )
            == 0
        )
        overridden = json.loads(capsys.readouterr().out)
        assert overridden["tier"] == "high"
        assert overridden["assigned_by"] == "user"

        assert main(["shelf", "plan", "--date", str(NOW.date()), "--json"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["date"] == str(NOW.date())
        assert len(plan["slots"]) == 1
        assert plan["slots"][0]["tier"] == "high"
        assert plan["quotas"]["review"] == 3
    finally:
        get_settings.cache_clear()
