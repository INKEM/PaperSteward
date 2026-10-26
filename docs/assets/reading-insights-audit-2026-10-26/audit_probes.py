"""第一阶段补充验收：只用虚构数据和新建临时库，不修改业务代码。

在仓库根目录运行：
.venv/bin/python docs/assets/reading-insights-audit-2026-10-26/audit_probes.py

输出 reproduced=true 表示在审计基线上复现问题，并不表示功能正确。
修复后预期对应值变为 false；执行失败也不能算修复通过。
"""

from __future__ import annotations

import json
import runpy
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests/integration"))

from test_reading_insight_projection import seed_archive, seed_paper, system_content

from papersteward.api.insights_pages import _parse_asset_revision
from papersteward.jobs.ui_flow import ReviewParams
from papersteward.reading.insights.assets import (
    append_revision,
    create_asset,
    delete_asset,
    delete_preview,
    get_asset_view,
)
from papersteward.reading.insights.contracts import (
    AssetCreateInput,
    ReadTimeDeclaration,
)
from papersteward.reading.insights.export import export_report
from papersteward.reading.insights.reports import (
    _effective_facts,
    list_series_reports,
    prepare_report,
    read_report,
)
from papersteward.reading.insights.timeline import record_reading_time
from papersteward.storage import Base, create_db_engine, create_session_factory, session_scope
from papersteward.storage.delivery import content_hash
from papersteward.storage.models import ReadingReportRow

AT = datetime(2026, 10, 22, 2, tzinfo=UTC)
NOW = datetime(2026, 10, 26, 2, tzinfo=UTC)
activate_workspace = runpy.run_path(str(ROOT / "tests/integration/conftest.py"))[
    "activate_workspace"
]


def seed(factory, paper_id="a", tag="T01", when=AT):
    with session_scope(factory) as session:
        seed_paper(session, paper_id, first_seen_at=when)
        seed_archive(
            session,
            paper_id,
            archived_at=when,
            primary_tag=tag,
            content=system_content(title=f"审计虚构论文-{paper_id}", completed_at=when),
            ratings=[(when, 4, "虚构反馈")],
        )


def make_asset(session, *, paper_id="a", key="asset-key", title="虚构私人研究标题", rating=None):
    return create_asset(
        session,
        AssetCreateInput.model_validate(
            {
                "kind": "problem",
                "title": title,
                "body": "虚构私人正文",
                "sources": [{"paper_id": paper_id, "rating_at": rating}],
                "idempotency_key": key,
            }
        ),
        now=AT + timedelta(hours=1),
    )


def prepared(factory, *, tag=None, now=NOW, timezone="Asia/Shanghai", period="week"):
    values = {"period": period, "mode": "revised_now", "tag": tag}
    if period == "week":
        values["anchor_date"] = date(2026, 10, 22)
    with session_scope(factory) as session:
        return prepare_report(
            session,
            ReviewParams.model_validate(values),
            collected_at=now,
            timezone_name=timezone,
        )


def store(factory, value, report_id, generated=NOW):
    content = value.content.model_dump(mode="json")
    with session_scope(factory) as session:
        session.add(
            ReadingReportRow(
                id=report_id,
                report_kind=value.period.kind.value,
                period_label=value.period.label,
                period_start=value.period.start_date,
                period_end=value.period.end_date_inclusive,
                timezone=value.period.timezone,
                review_mode=value.query.mode.value,
                knowledge_cutoff=value.knowledge_cutoff,
                generated_at=generated,
                contract=value.content.contract,
                input_fingerprint=content_hash({"fixture": report_id}),
                content_json=content,
                content_hash=content_hash(content),
                run_id=f"fixture-{report_id}",
            )
        )


def scope_probe(factory):
    seed(factory)
    seed(factory, "b", "T02")
    with session_scope(factory) as session:
        make_asset(session, paper_id="b")
    report = prepared(factory, tag="tv-test::T01")
    return {
        "reproduced": report.content.metrics.asset_assets == 1 and len(report.content.assets) == 1,
        "topic_readings": report.content.metrics.readings_total,
        "unrelated_asset_count": report.content.metrics.asset_assets,
        "unrelated_asset_refs": len(report.content.assets),
    }


def fingerprint_probe(factory):
    seed(factory)
    before = prepared(factory, tag="tv-test::T01", period="all")
    seed(factory, "unrelated", "T02", NOW + timedelta(hours=1))
    after = prepared(factory, tag="tv-test::T01", period="all", now=NOW + timedelta(hours=2))
    equal = _effective_facts(before.content) == _effective_facts(after.content)
    return {
        "reproduced": equal and before.fingerprint != after.fingerprint,
        "scoped_facts_equal": equal,
        "fingerprints_equal": before.fingerprint == after.fingerprint,
    }


def series_probe(factory):
    seed(factory)
    seed(factory, "b", "T02")
    first = prepared(factory, tag="tv-test::T01")
    store(factory, first, "target")
    store(factory, prepared(factory, tag="tv-test::T02"), "other-topic")
    store(factory, prepared(factory, tag="tv-test::T01", timezone="UTC"), "other-timezone")
    with session_scope(factory) as session:
        detail = read_report(session, "target")
        mixed = [item.report_id for item in list_series_reports(session, detail)]
    filler = prepared(factory, period="all")
    for index in range(51):
        store(factory, filler, f"filler-{index}", NOW + timedelta(minutes=index + 1))
    with session_scope(factory) as session:
        hidden = list_series_reports(session, read_report(session, "target"))
    return {
        "reproduced": "other-topic" in mixed and "other-timezone" in mixed and not hidden,
        "mixed_series_ids": mixed,
        "series_count_after_51_unrelated_reports": len(hidden),
    }


def idempotency_probe(factory):
    seed(factory)
    seed(factory, "b", "T02")
    with session_scope(factory) as session:
        original = make_asset(session)
        changed = make_asset(session, title="不同的标题但相同幂等键")
        declaration = ReadTimeDeclaration(precision="day", day=date(2026, 10, 21))
        record_reading_time(
            session, "a", declaration, now=NOW, expected_revision=0, idempotency_key="same-date-key"
        )
        second = record_reading_time(
            session, "b", declaration, now=NOW, expected_revision=0, idempotency_key="same-date-key"
        )
    return {
        "reproduced": original.asset_id == changed.asset_id and second.current_revision == 0,
        "asset_changed_payload_silently_replayed": changed.revision.title == original.revision.title,
        "second_paper_success_result_revision": second.current_revision,
    }


def provenance_probe(factory):
    seed(factory)
    with session_scope(factory) as session:
        original = make_asset(session, rating=AT)
        request = _parse_asset_revision(
            {
                "expected_revision": "1",
                "title": "只修改标题",
                "body": original.revision.body,
                "status": "open",
                "sources": "a",
            }
        )
        append_revision(session, original.asset_id, request, now=NOW)
        current = get_asset_view(session, original.asset_id)
        old = current.model_dump(mode="json")["history"][0]
    return {
        "reproduced": current.revision.sources[0].rating_at is None and "body" not in old,
        "feedback_version_lost_after_ui_revision": current.revision.sources[0].rating_at is None,
        "history_only_has_summary_fields": sorted(old),
    }


def private_title_probe(factory):
    seed(factory)
    with session_scope(factory) as session:
        asset = make_asset(session)
    value = prepared(factory)
    store(factory, value, "private-title-report")
    with session_scope(factory) as session:
        preview = delete_preview(session, asset.asset_id)
        delete_asset(session, asset.asset_id)
        report = read_report(session, "private-title-report")
        private_copy = export_report(report, "json", include_private=True)
    return {
        "reproduced": not preview["reports_with_body"] and "虚构私人研究标题" in private_copy,
        "deletable_private_copy_report_ids": preview["reports_with_body"],
        "title_still_in_saved_report_after_asset_deleted": "虚构私人研究标题" in private_copy,
    }


def main():
    probes = {
        "A01_topic_scope": scope_probe,
        "A02_scoped_fingerprint": fingerprint_probe,
        "A03_report_series": series_probe,
        "A04_idempotency": idempotency_probe,
        "A05_asset_provenance_history": provenance_probe,
        "A06_private_title_copies": private_title_probe,
    }
    results = {}
    with TemporaryDirectory(prefix="ps-r1-audit-probes-", dir="/dev/shm") as directory:
        for name, probe in probes.items():
            engine = create_db_engine(f"sqlite+pysqlite:///{directory}/{name}.db")
            try:
                Base.metadata.create_all(engine)
                activate_workspace(engine)
                results[name] = probe(create_session_factory(engine))
            finally:
                engine.dispose()
    print(json.dumps({"baseline": "3a351b4", "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
