"""R2-00A：F01–F03 的正确性回归，范围、源版本、指纹与报告身份统一。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from test_reading_insight_projection import seed_archive, seed_paper, system_content

from papersteward.core.entities import Run
from papersteward.delivery.contracts import Action
from papersteward.errors import InvalidRequestError
from papersteward.jobs.ui_flow import ReviewParams
from papersteward.reading.actions import apply_action
from papersteward.reading.insights.assets import append_revision, create_asset
from papersteward.reading.insights.contracts import (
    AssetCreateInput,
    AssetRevisionInput,
)
from papersteward.reading.insights.reports import (
    ensure_same_series,
    list_series_reports,
    prepare_report,
    read_report,
    report_input_status,
    save_report,
)
from papersteward.reading.insights.scope import series_key
from papersteward.storage import session_scope
from papersteward.storage.models import ReadingReportContextRow, RunRow
from papersteward.storage.repositories import RunRepository

AT = datetime(2026, 10, 22, 2, tzinfo=UTC)
NOW = datetime(2026, 10, 26, 2, tzinfo=UTC)


def seed(factory, paper_id="a", tag="T01", *, at=AT):
    with session_scope(factory) as session:
        seed_paper(session, paper_id, first_seen_at=at)
        seed_archive(
            session,
            paper_id,
            archived_at=at,
            primary_tag=tag,
            content=system_content(title=f"范围样例-{paper_id}", completed_at=at),
            ratings=[(at, 4, "初始")],
        )


def asset(factory, ids, *, key="asset", at=AT):
    with session_scope(factory) as session:
        return create_asset(
            session,
            AssetCreateInput(
                kind="problem",
                title=key,
                body=f"{key}私人正文",
                sources=[{"paper_id": pid} for pid in ids],
                idempotency_key=key.encode().hex(),
            ),
            now=at,
        )


def prepare(
    factory,
    *,
    tag="tv-test::T01",
    at=NOW,
    timezone="Asia/Shanghai",
    period="week",
    mode="revised_now",
    bodies=False,
    anchor="2026-10-22",
):
    params = ReviewParams(
        period=period,
        mode=mode,
        tag=tag,
        anchor_date=anchor if period in {"week", "month"} else None,
    )
    with session_scope(factory) as session:
        return prepare_report(
            session, params, collected_at=at, timezone_name=timezone, include_asset_bodies=bodies
        )


def store(factory, value, key="saved", *, at=NOW):
    with session_scope(factory, immediate=True) as session:
        RunRepository(session).add(Run(key, "ui.review", started_at=at))
        row = save_report(session, value, run_id=key, now=at)
        assert row is not None
        return row.id


def test_f01_topic_filters_asset_events_refs_and_private_bodies(insights_factory, client):
    seed(insights_factory)
    seed(insights_factory, "b", "T02")
    unrelated = asset(insights_factory, ["b"], key="T02秘密")
    selected = asset(insights_factory, ["a"], key="T01秘密")
    value = prepare(insights_factory, bodies=True)
    assert value.content.metrics.readings_total == 1
    assert value.content.metrics.asset_events == value.content.metrics.asset_assets == 1
    assert [ref.asset_id for ref in value.content.assets] == [selected.asset_id]
    assert unrelated.asset_id not in value.context.model_dump_json()
    assert "T02秘密" not in value.content.model_dump_json()
    assert value.content.assets[0].body == "T01秘密私人正文"
    api = client.get(
        "/reading-insights/overview?kind=week&anchor_date=2026-10-22&mode=revised_now&tag=tv-test::T01"
    ).json()
    assert api["metrics"]["asset_events"] == 1
    assert api["assets"][0]["asset_id"] == selected.asset_id
    page = client.get(
        "/ui/reading-insights?kind=week&anchor_date=2026-10-22&mode=revised_now&tag=tv-test::T01"
    )
    assert "T01秘密" in page.text and "T02秘密" not in page.text
    assert '<option value="tv-test::T01" selected>历史标签 tv-test::T01</option>' in page.text


def test_asset_version_sources_do_not_drift_and_mixed_assets_count_once(insights_factory, client):
    seed(insights_factory)
    seed(insights_factory, "b", "T02")
    mixed = asset(insights_factory, ["a", "b"], key="跨主题")
    with session_scope(insights_factory) as session:
        append_revision(
            session,
            mixed.asset_id,
            AssetRevisionInput(
                expected_revision=1,
                title="转入T02",
                body="别的主题正文",
                status="open",
                sources=[{"paper_id": "b"}],
            ),
            now=AT + timedelta(hours=1),
        )
    first = prepare(insights_factory, bodies=True)
    assert first.content.metrics.asset_events == 1
    assert first.content.assets[0].revision == 1 and first.content.assets[0].cross_topic is True
    assert first.content.assets[0].body == "跨主题私人正文"
    second = prepare(insights_factory, tag="tv-test::T02", bodies=True)
    assert second.content.metrics.asset_events == 2 and second.content.metrics.asset_assets == 1
    assert second.content.assets[0].revision == 2 and second.content.assets[0].cross_topic is False
    page = client.get("/ui/reading-insights?kind=all&tag=tv-test::T01")
    assert "跨主题来源" in page.text and "转入T02" not in page.text
    assert prepare(insights_factory, at=AT + timedelta(minutes=30)).content.assets[0].revision == 1


def test_f02_outside_reading_feedback_asset_changes_do_not_change_fingerprint(insights_factory):
    seed(insights_factory)
    seed(insights_factory, "b", "T02")
    before = prepare(insights_factory, period="all")
    saved = store(insights_factory, before)
    seed(insights_factory, "outside-new", "T02", at=NOW)
    asset(insights_factory, ["b"], key="范围外新资产", at=NOW)
    with session_scope(insights_factory) as session:
        apply_action(
            session, "b", Action(request_id="outside-feedback", kind="score", score=2), now=NOW
        )
    after = prepare(insights_factory, period="all", at=NOW + timedelta(hours=1))
    assert before.fingerprint == after.fingerprint
    assert store(insights_factory, after, key="repeat") == saved
    with session_scope(insights_factory) as session:
        assert (
            report_input_status(session, read_report(session, saved), now=NOW + timedelta(hours=1))[
                "changed"
            ]
            is False
        )
        apply_action(
            session, "a", Action(request_id="inside-feedback", kind="score", score=1), now=NOW
        )
    assert (
        prepare(insights_factory, period="all", at=NOW + timedelta(hours=1)).fingerprint
        != before.fingerprint
    )


def test_period_inventory_is_explanatory_and_week_anchor_is_canonical(insights_factory):
    seed(insights_factory)
    before = prepare(insights_factory, tag=None)
    seed(insights_factory, "unrelated-now", "T01", at=NOW)
    after = prepare(insights_factory, tag=None, at=NOW + timedelta(hours=1), anchor="2026-10-24")
    assert after.content.metrics.all_history_total == before.content.metrics.all_history_total + 1
    assert before.fingerprint == after.fingerprint
    assert after.context.normalized_request.period.anchor_date.isoformat() == "2026-10-19"


def test_closed_period_and_source_version_changes_are_effective(insights_factory):
    seed(insights_factory)
    before = prepare(insights_factory, at=AT + timedelta(hours=1), mode="as_recorded")
    closed = prepare(insights_factory, mode="as_recorded")
    assert before.fingerprint != closed.fingerprint
    asset(insights_factory, ["a"], key="相关版本", at=AT + timedelta(hours=2))
    assert prepare(insights_factory, mode="as_recorded").fingerprint != closed.fingerprint


@pytest.mark.parametrize(
    "overrides",
    [
        {"tag": "tv-test::T02"},
        {"timezone": "UTC"},
        {"tag": "different-version::T01"},
        {"mode": "as_recorded"},
        {"period": "month"},
    ],
)
def test_f03_series_identity_rejects_other_ranges(insights_factory, overrides):
    seed(insights_factory)
    seed(insights_factory, "b", "T02")
    first = store(insights_factory, prepare(insights_factory), key="first")
    other = store(insights_factory, prepare(insights_factory, **overrides), key="other")
    with session_scope(insights_factory) as session:
        a, b = read_report(session, first), read_report(session, other)
        assert [item.report_id for item in list_series_reports(session, a)] == [first]
        with pytest.raises(InvalidRequestError):
            ensure_same_series(a, b)


def test_series_database_filters_before_pagination_and_survives_run_deletion(
    insights_factory, client
):
    seed(insights_factory)
    value = prepare(insights_factory)
    first = store(insights_factory, value, key="first")
    # 分页样例保留同一来源与系列，使用独立指纹构造多个历史版本。
    for index in range(55):
        store(
            insights_factory,
            replace(value, fingerprint=f"{index:064x}"),
            key=f"v-{index}",
            at=NOW + timedelta(minutes=index + 1),
        )
    for index in range(51):
        filler = prepare(insights_factory, period="month", anchor="2026-09-22")
        store(
            insights_factory,
            replace(filler, fingerprint=f"f{index:063x}"),
            key=f"f-{index}",
            at=NOW + timedelta(days=1, minutes=index),
        )
    with session_scope(insights_factory) as session:
        session.execute(delete(RunRow))
    with session_scope(insights_factory) as session:
        detail = read_report(session, first)
        assert detail.context.status == "complete" and detail.context.scope.topic.code == "T01"
        versions = list_series_reports(session, detail, limit=50)
        tail = list_series_reports(session, detail, limit=50, offset=50)
        assert len(versions) == 50 and len(tail) == 6 and tail[-1].report_id == first
        assert report_input_status(session, detail, now=NOW)["changed"] is False
    api = client.get(f"/reading-insights/reports/{first}/series?offset=50")
    assert api.status_code == 200 and len(api.json()["items"]) == 6
    assert "下一页" in client.get(f"/ui/reading-insights/reports/{first}").text
    assert "上一页" in client.get(f"/ui/reading-insights/reports/{first}?offset=50").text


def test_compare_api_and_page_refuse_other_series(insights_factory, client):
    seed(insights_factory)
    first = store(insights_factory, prepare(insights_factory), key="first")
    other = store(insights_factory, prepare(insights_factory, timezone="UTC"), key="other")
    assert client.get(f"/reading-insights/reports/{first}/compare/{other}").status_code == 400
    page = client.get(f"/ui/reading-insights/reports/{first}?compare={other}")
    assert page.status_code == 400 and "无法比较" in page.text
    with session_scope(insights_factory, immediate=True) as session:
        row = session.get(ReadingReportContextRow, other)
        from papersteward.reading.insights.contracts import ReportContext

        context = ReportContext.model_validate(row.context_json)
        context = context.model_copy(
            update={"scope": context.scope.model_copy(update={"algorithm_version": "future/3"})}
        )
        row.context_json = context.model_dump(mode="json")
        row.series_key = series_key(context)
    assert client.get(f"/reading-insights/reports/{first}/compare/{other}").status_code == 400


def test_later_asset_sources_do_not_change_historical_activity(insights_factory):
    seed(insights_factory)
    seed(insights_factory, "b", "T02")
    original = asset(insights_factory, ["a"], key="历史版本")
    before = prepare(insights_factory, bodies=True)
    with session_scope(insights_factory) as session:
        append_revision(
            session,
            original.asset_id,
            AssetRevisionInput(
                expected_revision=1,
                title="周期后来源改变",
                body="后来正文",
                status="open",
                sources=[{"paper_id": "b"}],
            ),
            now=NOW,
        )
    after = prepare(insights_factory, bodies=True, at=NOW + timedelta(hours=1))
    assert after.fingerprint == before.fingerprint
    assert after.content.assets[0].title == "历史版本"
    assert after.content.assets[0].revision == 1


def test_reading_declaration_version_and_feedback_in_scope_change_fingerprint(insights_factory):
    from papersteward.reading.insights.contracts import ReadTimeDeclaration
    from papersteward.reading.insights.timeline import record_reading_time

    seed(insights_factory)
    original = prepare(insights_factory)
    with session_scope(insights_factory) as session:
        record_reading_time(
            session,
            "a",
            ReadTimeDeclaration(precision="day", day=AT.date()),
            now=AT + timedelta(hours=1),
            expected_revision=0,
        )
    revised = prepare(insights_factory)
    assert revised.fingerprint != original.fingerprint
    assert (
        revised.context.manifest.papers[0].archive_hash
        == original.context.manifest.papers[0].archive_hash
    )
