from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.delivery.contracts import ArchiveRecord
from papersteward.delivery.export import export_archives


def record():
    return ArchiveRecord(
        paper_id="p",
        archived_at=datetime(2026, 10, 22, tzinfo=UTC),
        primary_tag="T01",
        secondary_tags=["检索"],
        filing_bucket_id="b-t01",
        content_hash="0" * 64,
        content={
            "metadata": {
                "title": "Frozen {Paper}",
                "authors": ["A Author", "B Author"],
                "venue": "ACL",
                "published_at": "2026-01-01",
                "doi": "10.0000/fixture",
                "url": "https://example.org/paper",
                "pdf_url": "https://example.org/p.pdf",
            }
        },
        content_available=True,
        initial_score=4,
        initial_note="原备注",
        score=5,
        note="新备注",
        revision_history=[{"score": 4, "note": "原备注"}, {"score": 5, "note": "新备注"}],
    )


@pytest.mark.parametrize("format", ["bibtex", "csv", "markdown"])
def test_archive_export_golden(format):
    with Path(f"tests/fixtures/archive/basic.{format}").open(newline="") as stream:
        assert export_archives([record()], format) == stream.read()


def test_csv_formula_and_bibtex_markdown_escaping():
    r = record()
    r.content["metadata"]["title"] = "=cmd()"
    r.note = "@SUM(1)"
    csv = export_archives([r], "csv")
    assert "'=cmd()" in csv and "'@SUM(1)" in csv
    r.content["metadata"]["title"] = "x}\n@evil{x"
    assert "x\\} @evil\\{x" in export_archives([r], "bibtex")
    r.note = "<script>bad</script>"
    assert "<script>" not in export_archives([r], "markdown")
