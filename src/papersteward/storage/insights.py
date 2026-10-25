"""阅读回顾新增表的仓储；事务边界由调用方（服务/API）持有。"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select

from papersteward.core.entities import ReadingTimeRevision
from papersteward.storage.mappers import (
    reading_time_revision_from_row,
    reading_time_revision_to_row,
)
from papersteward.storage.models import ReadingTimeRevisionRow
from papersteward.storage.repositories import _Repository


class ReadingTimeRevisionRepository(_Repository):
    """阅读时间声明版本；只追加与读取，不提供 update/delete。"""

    def add(self, revision: ReadingTimeRevision) -> ReadingTimeRevision:
        self._session.add(reading_time_revision_to_row(revision))
        self._flush()
        return revision

    def list_for_paper(self, paper_id: str) -> list[ReadingTimeRevision]:
        statement = (
            select(ReadingTimeRevisionRow)
            .where(ReadingTimeRevisionRow.paper_id == paper_id)
            .order_by(ReadingTimeRevisionRow.revision)
        )
        return [
            reading_time_revision_from_row(row) for row in self._session.scalars(statement).all()
        ]

    def list_for_papers(self, paper_ids: Collection[str]) -> dict[str, list[ReadingTimeRevision]]:
        """批量按论文取全部版本；每篇内部按 revision 升序，与逐篇版一致。"""
        ids = list(paper_ids)
        if not ids:
            return {}
        rows = self._session.scalars(
            select(ReadingTimeRevisionRow)
            .where(ReadingTimeRevisionRow.paper_id.in_(ids))
            .order_by(ReadingTimeRevisionRow.paper_id, ReadingTimeRevisionRow.revision)
        ).all()
        by_paper: dict[str, list[ReadingTimeRevision]] = {}
        for row in rows:
            by_paper.setdefault(row.paper_id, []).append(reading_time_revision_from_row(row))
        return by_paper

    def find_by_idempotency_key(self, key: str) -> ReadingTimeRevision | None:
        row = self._session.scalars(
            select(ReadingTimeRevisionRow).where(ReadingTimeRevisionRow.idempotency_key == key)
        ).first()
        return None if row is None else reading_time_revision_from_row(row)


__all__ = ["ReadingTimeRevisionRepository"]
