"""阅读回顾新增表的仓储；事务边界由调用方（服务/API）持有。"""

from __future__ import annotations

import builtins
from collections.abc import Collection
from typing import Any

from sqlalchemy import func, select

from papersteward.core.entities import ReadingTimeRevision
from papersteward.storage.mappers import (
    reading_time_revision_from_row,
    reading_time_revision_to_row,
)
from papersteward.storage.models import (
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingAssetSourceRow,
    ReadingDerivedCopyRow,
    ReadingReportContextRow,
    ReadingReportRow,
    ReadingStructuredFeedbackRow,
    ReadingTimeRevisionRow,
    ReadingWriteReceiptRow,
)
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

    def save_context(self, report_id: str, payload: dict[str, Any], series_key: str | None) -> None:
        self._session.add(
            ReadingReportContextRow(
                report_id=report_id, context_json=payload, series_key=series_key
            )
        )
        self._flush()

    def contexts_for_reports(self, report_ids: builtins.list[str]) -> dict[str, dict[str, Any]]:
        if not report_ids:
            return {}
        return {
            row.report_id: row.context_json
            for row in self._session.scalars(
                select(ReadingReportContextRow).where(
                    ReadingReportContextRow.report_id.in_(report_ids)
                )
            )
        }

    def list_series(
        self, series_key: str, *, limit: int, offset: int
    ) -> builtins.list[ReadingReportRow]:
        return list(
            self._session.scalars(
                select(ReadingReportRow)
                .join(
                    ReadingReportContextRow,
                    ReadingReportContextRow.report_id == ReadingReportRow.id,
                )
                .where(ReadingReportContextRow.series_key == series_key)
                .order_by(ReadingReportRow.generated_at.desc(), ReadingReportRow.id)
                .offset(offset)
                .limit(limit)
            )
        )

    def delete(self, report_id: str) -> bool:
        row = self._session.get(ReadingReportRow, report_id)
        if row is None:
            return False
        self._session.delete(row)
        self._flush()
        return True


class ReadingWriteReceiptRepository(_Repository):
    """写入回执；查询按幂等键，插入由调用方与业务写入同事务提交。"""

    def find(self, key: str) -> ReadingWriteReceiptRow | None:
        return self._session.scalars(
            select(ReadingWriteReceiptRow).where(ReadingWriteReceiptRow.idempotency_key == key)
        ).first()

    def add(self, row: ReadingWriteReceiptRow) -> None:
        self._session.add(row)
        self._flush()


class ReadingDerivedCopyRepository(_Repository):
    """私人派生副本登记；只追加与按来源/目标查询，报告删除级联清理。"""

    def add_many(self, rows: list[ReadingDerivedCopyRow]) -> None:
        for row in rows:
            self._session.add(row)
        self._flush()

    def for_source(self, source_kind: str, source_id: str) -> list[ReadingDerivedCopyRow]:
        """某来源（资产版本）的全部登记行；按派生对象与字段类别排序。"""
        return list(
            self._session.scalars(
                select(ReadingDerivedCopyRow)
                .where(
                    ReadingDerivedCopyRow.source_kind == source_kind,
                    ReadingDerivedCopyRow.source_id == source_id,
                )
                .order_by(
                    ReadingDerivedCopyRow.derived_id,
                    ReadingDerivedCopyRow.field_kind,
                )
            ).all()
        )

    def lineage_for_report(self, report_id: str) -> list[ReadingDerivedCopyRow]:
        """以某报告为派生目标的登记行（含 sanitized 谱系与私人字段）。"""
        return list(
            self._session.scalars(
                select(ReadingDerivedCopyRow)
                .where(ReadingDerivedCopyRow.derived_id == report_id)
                .order_by(ReadingDerivedCopyRow.source_kind, ReadingDerivedCopyRow.source_id)
            ).all()
        )


class ReadingStructuredFeedbackRepository(_Repository):
    """结构化阅读理由版本；只追加与按论文读取，不提供 update/delete。"""

    def add(self, row: ReadingStructuredFeedbackRow) -> ReadingStructuredFeedbackRow:
        self._session.add(row)
        self._flush()
        return row

    def list_for_paper(self, paper_id: str) -> list[ReadingStructuredFeedbackRow]:
        statement = (
            select(ReadingStructuredFeedbackRow)
            .where(ReadingStructuredFeedbackRow.paper_id == paper_id)
            .order_by(ReadingStructuredFeedbackRow.revision)
        )
        return list(self._session.scalars(statement).all())

    def list_for_papers(
        self, paper_ids: Collection[str]
    ) -> dict[str, list[ReadingStructuredFeedbackRow]]:
        """批量按论文取全部版本；每篇内部按 revision 升序，与逐篇版一致。"""
        ids = list(paper_ids)
        if not ids:
            return {}
        rows = self._session.scalars(
            select(ReadingStructuredFeedbackRow)
            .where(ReadingStructuredFeedbackRow.paper_id.in_(ids))
            .order_by(ReadingStructuredFeedbackRow.paper_id, ReadingStructuredFeedbackRow.revision)
        ).all()
        by_paper: dict[str, list[ReadingStructuredFeedbackRow]] = {}
        for row in rows:
            by_paper.setdefault(row.paper_id, []).append(row)
        return by_paper


__all__ = [
    "ReadingAssetRepository",
    "ReadingDerivedCopyRepository",
    "ReadingReportRepository",
    "ReadingStructuredFeedbackRepository",
    "ReadingTimeRevisionRepository",
    "ReadingWriteReceiptRepository",
]


class ReadingAssetRepository(_Repository):
    """手工资产的仓储：身份、版本与来源一次写入；只追加与删除。"""

    def create(
        self,
        asset: ReadingAssetRow,
        revision: ReadingAssetRevisionRow,
        sources: list[ReadingAssetSourceRow],
    ) -> None:
        # 无 relationship 时按依赖分段 flush，保证父表先落库。
        self._session.add(asset)
        self._flush()
        self._session.add(revision)
        self._flush()
        for source in sources:
            self._session.add(source)
        self._flush()

    def add_revision(
        self, revision: ReadingAssetRevisionRow, sources: list[ReadingAssetSourceRow]
    ) -> None:
        self._session.add(revision)
        self._flush()
        for source in sources:
            self._session.add(source)
        self._flush()

    def get_asset(self, asset_id: str) -> ReadingAssetRow | None:
        return self._session.get(ReadingAssetRow, asset_id)

    def get_by_idempotency(self, key: str) -> ReadingAssetRow | None:
        return self._session.scalars(
            select(ReadingAssetRow).where(ReadingAssetRow.idempotency_key == key)
        ).first()

    def max_revision(self, asset_id: str) -> int:
        return (
            self._session.scalar(
                select(func.max(ReadingAssetRevisionRow.revision)).where(
                    ReadingAssetRevisionRow.asset_id == asset_id
                )
            )
            or 0
        )

    def revision(self, asset_id: str, revision: int) -> ReadingAssetRevisionRow | None:
        return self._session.get(
            ReadingAssetRevisionRow, {"asset_id": asset_id, "revision": revision}
        )

    def revisions(self, asset_id: str) -> list[ReadingAssetRevisionRow]:
        return list(
            self._session.scalars(
                select(ReadingAssetRevisionRow)
                .where(ReadingAssetRevisionRow.asset_id == asset_id)
                .order_by(ReadingAssetRevisionRow.revision)
            ).all()
        )

    def all_revisions(self) -> list[ReadingAssetRevisionRow]:
        return list(
            self._session.scalars(
                select(ReadingAssetRevisionRow).order_by(
                    ReadingAssetRevisionRow.recorded_at, ReadingAssetRevisionRow.asset_id
                )
            ).all()
        )

    def sources(self, asset_id: str, revision: int) -> list[ReadingAssetSourceRow]:
        return list(
            self._session.scalars(
                select(ReadingAssetSourceRow)
                .where(
                    ReadingAssetSourceRow.asset_id == asset_id,
                    ReadingAssetSourceRow.revision == revision,
                )
                .order_by(ReadingAssetSourceRow.id)
            ).all()
        )

    def list_current(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        paper_id: str | None = None,
        keyword: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[ReadingAssetRow, ReadingAssetRevisionRow]], int]:
        """按最新版本视图筛选资产；来源/关键词只作用于当前版本。"""
        latest = (
            select(func.max(ReadingAssetRevisionRow.revision))
            .where(ReadingAssetRevisionRow.asset_id == ReadingAssetRow.id)
            .correlate(ReadingAssetRow)
            .scalar_subquery()
        )
        statement = select(ReadingAssetRow, ReadingAssetRevisionRow).join(
            ReadingAssetRevisionRow,
            (ReadingAssetRevisionRow.asset_id == ReadingAssetRow.id)
            & (ReadingAssetRevisionRow.revision == latest),
        )
        conditions = []
        if kind:
            conditions.append(ReadingAssetRow.kind == kind)
        if status:
            conditions.append(ReadingAssetRevisionRow.status == status)
        if paper_id:
            source_exists = (
                select(ReadingAssetSourceRow.id)
                .where(
                    ReadingAssetSourceRow.asset_id == ReadingAssetRow.id,
                    ReadingAssetSourceRow.revision == ReadingAssetRevisionRow.revision,
                    ReadingAssetSourceRow.paper_id == paper_id,
                )
                .exists()
            )
            conditions.append(source_exists)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(
                ReadingAssetRevisionRow.title.like(pattern, escape="\\")
                | ReadingAssetRevisionRow.body.like(pattern, escape="\\")
            )
        if conditions:
            statement = statement.where(*conditions)
        total = (
            self._session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        rows = self._session.execute(
            statement.order_by(ReadingAssetRevisionRow.recorded_at.desc(), ReadingAssetRow.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return [(row[0], row[1]) for row in rows], int(total)

    def assets_for_paper(
        self, paper_id: str
    ) -> list[tuple[ReadingAssetRow, ReadingAssetRevisionRow]]:
        """引用了某篇论文（当前版本来源）的资产，按整理时间倒序。"""
        latest = (
            select(func.max(ReadingAssetRevisionRow.revision))
            .where(ReadingAssetRevisionRow.asset_id == ReadingAssetRow.id)
            .correlate(ReadingAssetRow)
            .scalar_subquery()
        )
        source_exists = (
            select(ReadingAssetSourceRow.id)
            .where(
                ReadingAssetSourceRow.asset_id == ReadingAssetRow.id,
                ReadingAssetSourceRow.revision == ReadingAssetRevisionRow.revision,
                ReadingAssetSourceRow.paper_id == paper_id,
            )
            .exists()
        )
        rows = self._session.execute(
            select(ReadingAssetRow, ReadingAssetRevisionRow)
            .join(
                ReadingAssetRevisionRow,
                (ReadingAssetRevisionRow.asset_id == ReadingAssetRow.id)
                & (ReadingAssetRevisionRow.revision == latest),
            )
            .where(source_exists)
            .order_by(ReadingAssetRevisionRow.recorded_at.desc())
        ).all()
        return [(row[0], row[1]) for row in rows]

    def delete_asset(self, asset_id: str) -> bool:
        row = self._session.get(ReadingAssetRow, asset_id)
        if row is None:
            return False
        self._session.delete(row)
        self._flush()
        return True
