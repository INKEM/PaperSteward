"""`papersteward demo seed` 的演示数据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from papersteward.core.entities import Paper
from papersteward.core.enums import PaperStatus
from papersteward.storage import (
    PaperRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)

DEMO_PAPER_ID = "p001"
DEMO_PAPER_TITLE = "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"
DEMO_CREATED_AT = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


@dataclass(frozen=True)
class SeedResult:
    paper_id: str
    created: bool
    total: int


def seed_demo_data(*, engine: Engine | None = None) -> SeedResult:
    """幂等写入演示文献；已存在时不修改、不重复插入。"""
    owns_engine = engine is None
    resolved_engine = engine or create_db_engine()
    try:
        factory = create_session_factory(resolved_engine)
        with session_scope(factory) as session:
            repository = PaperRepository(session)
            created = repository.get(DEMO_PAPER_ID) is None
            if created:
                repository.add(
                    Paper(
                        id=DEMO_PAPER_ID,
                        title=DEMO_PAPER_TITLE,
                        status=PaperStatus.DISCOVERED,
                        first_seen_at=DEMO_CREATED_AT,
                    )
                )
            total = repository.count()
        return SeedResult(paper_id=DEMO_PAPER_ID, created=created, total=total)
    finally:
        if owns_engine:
            resolved_engine.dispose()


__all__ = [
    "DEMO_CREATED_AT",
    "DEMO_PAPER_ID",
    "DEMO_PAPER_TITLE",
    "SeedResult",
    "seed_demo_data",
]
