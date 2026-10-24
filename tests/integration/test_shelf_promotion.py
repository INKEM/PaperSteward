from __future__ import annotations

import pytest
from test_special_shelf import NOW, PAPER, _process, _setup

from conftest import activate
from papersteward.core.enums import BucketRole, BucketType, EventType, PaperStatus
from papersteward.errors import ConflictError
from papersteward.pipeline.shelf import accept_nomination, promote_to_topic
from papersteward.scoring.queue import create_snapshots, read_queue
from papersteward.storage import (
    PaperBucketRepository,
    PaperRepository,
    create_session_factory,
    session_scope,
)
from papersteward.storage.repositories import EventRepository


def test_promote_special_shelf_to_topic_and_rejoin_queue(client, migrated_engine):
    activate(client, quiet_start=None, quiet_end=None)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        outcome = _process(session)
        assert outcome.status is PaperStatus.CLASSIFIED
        with pytest.raises(ConflictError, match="shelved"):
            promote_to_topic(session, PAPER.id, reviewed_by="fixture-user", note="过早", now=NOW)
        accept_nomination(
            session,
            PAPER.id,
            reviewed_by="fixture-user",
            note="进入特殊桶",
            now=NOW,
        )
        assert PaperRepository(session).get(PAPER.id).status is PaperStatus.SHELVED
        promotion = promote_to_topic(
            session,
            PAPER.id,
            reviewed_by="fixture-user",
            note="提升回主题桶",
            now=NOW,
        )
        assert promotion.paper.status is PaperStatus.CLASSIFIED
        assert promotion.bucket.bucket_type is BucketType.TOPIC
        assert promotion.bucket.tag_code == "T02"
        assert promotion.static.score_date == NOW.date()
        filings = [
            item
            for item in PaperBucketRepository(session).list_for_paper(PAPER.id)
            if item.role is BucketRole.FILING
        ]
        assert [item.bucket_id for item in filings] == ["b-t02"]
        assert any(
            event.event_type is EventType.PAPER_BUCKET_MOVED
            for event in EventRepository(session).list_for_paper(PAPER.id)
        )
        with pytest.raises(ConflictError, match="shelved"):
            promote_to_topic(session, PAPER.id, reviewed_by="fixture-user", note="重复", now=NOW)

        snapshots = create_snapshots(session, NOW.date(), now=NOW)
        assert snapshots.created == 1
        queue = read_queue(session, NOW.date(), now=NOW)
        assert [item.paper_id for item in queue.items] == [PAPER.id]
        assert queue.items[0].explanation.scores["S"] == promotion.static.static_score
