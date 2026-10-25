"""阅读回顾新增表的仓储；事务边界由调用方（服务/API）持有。"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import func, select

from papersteward.core.entities import ReadingTimeRevision
from papersteward.storage.mappers import (
    reading_time_revision_from_row,
    reading_time_revision_to_row,
)
from papersteward.storage.models import ReadingReportRow, ReadingTimeRevisionRow
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


class ReadingReportRepository(_Repository):
    """已保存的报告快照；行即存储形态，正文契约解析在服务层完成。"""

    def add(self, row: ReadingReportRow) -> ReadingReportRow:
        self._session.add(row)
        self._flush()
        return row

    def get(self, report_id: str) -> ReadingReportRow | None:
        return self._session.get(ReadingReportRow, report_id)

    def get_by_run(self, run_id: str) -> ReadingReportRow | None:
        return self._session.scalars(
            select(ReadingReportRow).where(ReadingReportRow.run_id == run_id)
        ).first()

    def get_by_fingerprint(self, fingerprint: str) -> ReadingReportRow | None:
        return self._session.scalars(
            select(ReadingReportRow).where(ReadingReportRow.input_fingerprint == fingerprint)
        ).first()

    def list(self, *, limit: int, offset: int) -> tuple[list[ReadingReportRow], int]:
        """按生成时间倒序分页；返回 (行, 总数)。"""
        total = self._session.scalar(select(func.count()).select_from(ReadingReportRow)) or 0
        rows = self._session.scalars(
            select(ReadingReportRow)
            .order_by(ReadingReportRow.generated_at.desc(), ReadingReportRow.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), int(total)

    def delete(self, report_id: str) -> bool:
        row = self._session.get(ReadingReportRow, report_id)
        if row is None:
            return False
        self._session.delete(row)
        self._flush()
        return True


__all__ = ["ReadingReportRepository", "ReadingTimeRevisionRepository"]
