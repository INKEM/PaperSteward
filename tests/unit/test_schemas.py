from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from papersteward.api.schemas import PaperRead


def test_paper_read_treats_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 10, 21, 8, 0)  # noqa: DTZ001 - 模拟 SQLite 返回的 naive 值

    paper = PaperRead(id="p001", title="RAG", status="discovered", created_at=naive)

    assert paper.created_at == naive.replace(tzinfo=UTC)
    assert paper.created_at.utcoffset() == timedelta(0)


def test_paper_read_converts_aware_datetime_to_utc() -> None:
    shanghai = timezone(timedelta(hours=8))
    aware = datetime(2026, 10, 21, 16, 0, tzinfo=shanghai)

    paper = PaperRead(id="p001", title="RAG", status="discovered", created_at=aware)

    assert paper.created_at == aware.astimezone(UTC)
    assert paper.created_at.utcoffset() == timedelta(0)
