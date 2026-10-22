from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from papersteward.core.entities import Paper
from papersteward.core.enums import OaStatus
from papersteward.core.errors import InvariantViolation
from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.static import (
    AuthorityInputs,
    ExtraValueInputs,
    build_static_snapshot,
    compute_authority,
    compute_extra_value,
    compute_static_score,
    persist_static_snapshot,
)
from papersteward.storage import (
    PaperRepository,
    StaticScoreSnapshotRepository,
    create_session_factory,
    session_scope,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_scoring_config_file(ROOT / "tests" / "fixtures" / "configs" / "scoring.yaml")
NOW = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
PAPER = Paper(id="static-001", title="Synthetic static score paper", first_seen_at=NOW)


def _snapshot(config_version: str, score_date: date):
    authority = compute_authority(
        AuthorityInputs(
            venue_level="A*",
            author_scores=(1.0, 0.5),
            citation_percentile=0.8,
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
            as_of=date(2026, 10, 22),
        )
    )
    extra = compute_extra_value(
        ExtraValueInputs(
            code=1.0,
            data=0.5,
            reproducibility=0.5,
            rigor=0.5,
            oa_status=OaStatus.OPEN,
        )
    )
    result = compute_static_score(80.0, authority, extra, CONFIG)
    return build_static_snapshot(
        result,
        PAPER,
        score_date=score_date,
        config_version=config_version,
        profile_version="1.0",
        taxonomy_version="1.0",
        created_at=NOW,
    )


def test_static_snapshot_round_trips_and_is_version_frozen(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(PAPER)
        first = _snapshot(CONFIG.version, date(2026, 10, 22))
        persist_static_snapshot(session, first)
        repository = StaticScoreSnapshotRepository(session)
        assert repository.latest(PAPER.id) == first
        assert repository.history(PAPER.id) == [first]

        with pytest.raises(InvariantViolation, match="冻结"):
            persist_static_snapshot(session, _snapshot(CONFIG.version, date(2026, 10, 23)))

        changed = _snapshot("1.0.1", date(2026, 10, 23))
        persist_static_snapshot(session, changed)
        assert [item.static_score for item in repository.history(PAPER.id)] == [80.9, 80.9]
        assert repository.latest(PAPER.id).config_version == "1.0.1"  # type: ignore[union-attr]

        with pytest.raises(InvariantViolation, match="日期"):
            persist_static_snapshot(session, _snapshot("1.0.2", date(2026, 10, 23)))
