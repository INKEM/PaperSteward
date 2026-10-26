"""手工资产的创建、修订、状态、筛选、删除与报告接入验证（R1-05）。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_reading_insight_projection import (
    import_content,
    seed_archive,
    seed_paper,
    system_content,
)
from test_ui_flows import HEADERS, wait

from conftest import activate
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.delivery.contracts import Action
from papersteward.errors import ConflictError, InvalidRequestError
from papersteward.jobs.ui_flow import FlowRequest, ReviewParams, UIFlowManager
from papersteward.reading.actions import apply_action
from papersteward.reading.insights.assets import (
    append_revision,
    assets_for_paper,
    change_status,
    create_asset,
    delete_asset,
    delete_preview,
    get_asset_view,
    list_assets,
)
from papersteward.reading.insights.contracts import (
    AssetCreateInput,
    AssetRevisionInput,
)
from papersteward.reading.insights.reports import list_reports, read_report
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.models import (
    ArchiveSnapshotRow,
    RatingRow,
    ReadingAssetRevisionRow,
    ReadingAssetRow,
    ReadingAssetSourceRow,
)
from papersteward.storage.workspace import clear_workspace

NOW5 = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
ANCHOR = "2026-10-22"


def seed_two_archives(client) -> dict[str, datetime]:
    factory = create_session_factory(client.app.state.engine)
    done_a = datetime(2026, 10, 20, 2, tzinfo=UTC)
    done_b = datetime(2026, 10, 21, 2, tzinfo=UTC)
    with session_scope(factory) as session:
        seed_paper(session, "as-p1", first_seen_at=done_a)
        seed_archive(
            session,
            "as-p1",
            archived_at=done_a,
            content=system_content(title="系统阅读A", completed_at=done_a),
            rating_col=4,
            note_col="初始备注",
            ratings=[(done_a, 4, "初始备注")],
        )
        seed_paper(session, "as-p2", first_seen_at=done_b)
        seed_archive(
            session,
            "as-p2",
            archived_at=done_b,
            content=import_content(title="导入B"),
            rating_col=3,
            ratings=[(done_b, 3, None)],
        )
    return {"p1": done_a, "p2": done_b}


def create_input(**overrides: object) -> AssetCreateInput:
    payload: dict[str, object] = {
        "kind": "problem",
        "title": "一个方法问题",
        "body": "这篇论文的对照设置存疑。",
        "sources": [{"paper_id": "as-p1"}],
        "idempotency_key": "asset-key-1",
    }
    payload.update(overrides)
    return AssetCreateInput.model_validate(payload)


def revision_input(expected: int, **overrides: object) -> AssetRevisionInput:
    payload: dict[str, object] = {
        "expected_revision": expected,
        "title": "一个方法问题（修订）",
        "body": "补充了对照设置的细节。",
        "status": "open",
        "sources": [{"paper_id": "as-p1"}, {"paper_id": "as-p2"}],
    }
    payload.update(overrides)
    return AssetRevisionInput.model_validate(payload)


def test_create_requires_archived_source_and_replays_idempotency(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session, pytest.raises(DomainValidationError):
        create_asset(
            session,
            create_input(sources=[{"paper_id": "missing-paper"}]),
            now=NOW5,
        )
    with session_scope(factory) as session:
        view = create_asset(session, create_input(), now=NOW5)
        assert view.revision.revision == 1
        assert view.revision.status == "open"  # 问题默认待解决
        assert view.revision.sources[0].paper_id == "as-p1"
        assert view.revision.sources[0].archive_hash != ""
        replay = create_asset(session, create_input(), now=NOW5)
        assert replay.asset_id == view.asset_id
        with pytest.raises(InvariantViolation):
            create_asset(
                session,
                create_input(idempotency_key="asset-key-1", kind="harvest"),
                now=NOW5,
            )
        count = len(session.scalars(select(ReadingAssetRow)).all())
        assert count == 1


def test_revision_conflict_keeps_old_versions_and_sources(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(session, create_input(), now=NOW5)
        asset_id = view.asset_id
        append_revision(
            session, asset_id, revision_input(expected=1), now=datetime(2026, 10, 27, 3, tzinfo=UTC)
        )
    with session_scope(factory) as session, pytest.raises(ConflictError) as excinfo:
        append_revision(
            session, asset_id, revision_input(expected=1), now=datetime(2026, 10, 27, 3, tzinfo=UTC)
        )
    assert excinfo.value.details == {"current_revision": 2}
    with session_scope(factory) as session:
        view = get_asset_view(session, asset_id)
        assert view.revision.revision == 2
        assert len(view.revision.sources) == 2
        assert [item.revision for item in view.history] == [1, 2]
    with session_scope(factory) as session:
        from papersteward.storage.insights import ReadingAssetRepository

        repository = ReadingAssetRepository(session)
        old_sources = repository.sources(asset_id, 1)
        new_sources = repository.sources(asset_id, 2)
        assert [source.paper_id for source in old_sources] == ["as-p1"]
        assert sorted(source.paper_id for source in new_sources) == ["as-p1", "as-p2"]


def test_status_transitions_per_kind(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(session, create_input(), now=NOW5)
        view = change_status(session, view.asset_id, "resolved", now=NOW5, expected_revision=1)
        assert view.revision.status == "resolved"
        view = change_status(session, view.asset_id, "open", now=NOW5, expected_revision=2)
        assert view.revision.status == "open"
        with pytest.raises(DomainValidationError):
            change_status(session, view.asset_id, "active", now=NOW5, expected_revision=3)
    with session_scope(factory) as session:
        harvest = create_asset(
            session,
            create_input(kind="harvest", title="一条收获", idempotency_key="asset-h-1"),
            now=NOW5,
        )
        assert harvest.revision.status == "active"
        with pytest.raises(DomainValidationError):
            change_status(session, harvest.asset_id, "open", now=NOW5, expected_revision=1)
        view = change_status(session, harvest.asset_id, "archived", now=NOW5, expected_revision=1)
        assert view.revision.status == "archived"


def test_rating_reference_is_pinned(client, migrated_engine):
    activate(client)
    done = seed_two_archives(client)["p1"]
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(
            session,
            create_input(sources=[{"paper_id": "as-p1", "rating_at": done}]),
            now=NOW5,
        )
        assert view.revision.sources[0].rating_at == done
    with session_scope(factory) as session:
        apply_action(
            session,
            "as-p1",
            Action(request_id="as-rev-1", kind="score", score=2),
            now=datetime(2026, 10, 27, 3, tzinfo=UTC),
        )
    with session_scope(factory) as session:
        view = get_asset_view(session, view.asset_id)
        # 引用的反馈版本不被后续评分修订影响。
        assert view.revision.sources[0].rating_at == done


def test_multi_paper_asset_never_changes_reading_counts(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(
            session,
            create_input(sources=[{"paper_id": "as-p1"}, {"paper_id": "as-p2"}]),
            now=NOW5,
        )
        archives_before = len(session.scalars(select(ArchiveSnapshotRow)).all())
        ratings_before = session.scalars(select(RatingRow)).all()
        append_revision(
            session,
            view.asset_id,
            revision_input(expected=1),
            now=datetime(2026, 10, 27, 3, tzinfo=UTC),
        )
        assert len(session.scalars(select(ArchiveSnapshotRow)).all()) == archives_before
        assert session.scalars(select(RatingRow)).all() == ratings_before


def test_filters_and_assets_for_paper(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        problem = create_asset(session, create_input(), now=NOW5)
        create_asset(
            session,
            create_input(
                kind="harvest",
                title="另一个关键词收获",
                body="浅显但有用",
                idempotency_key="asset-key-2",
                sources=[{"paper_id": "as-p2"}],
            ),
            now=NOW5,
        )
        listed = list_assets(session, limit=10, offset=0)
        assert listed["total"] == 2
        assert list_assets(session, kind="harvest", limit=10, offset=0)["total"] == 1
        assert list_assets(session, status="open", limit=10, offset=0)["total"] == 1
        assert list_assets(session, paper_id="as-p2", limit=10, offset=0)["total"] == 1
        assert list_assets(session, keyword="对照", limit=10, offset=0)["total"] == 1
        linked = assets_for_paper(session, "as-p2")
        assert [view.asset_id for view in linked] != []
        assert linked[0].asset_id != problem.asset_id


def test_delete_preview_reports_and_body_opt_in(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    in_week = datetime(2026, 10, 22, 3, tzinfo=UTC)
    with session_scope(factory) as session:
        view = create_asset(session, create_input(), now=in_week)
    asset_id = view.asset_id
    manager: UIFlowManager = client.app.state.ui_flows
    manager.submit(
        FlowRequest(
            run_id="as-rep-plain",
            kind="review",
            review=ReviewParams.model_validate({"period": "week", "anchor_date": ANCHOR}),
        )
    )
    wait(manager, "as-rep-plain")
    manager.submit(
        FlowRequest(
            run_id="as-rep-body",
            kind="review",
            review=ReviewParams.model_validate(
                {"period": "week", "anchor_date": ANCHOR, "asset_bodies": True}
            ),
        )
    )
    wait(manager, "as-rep-body")
    with session_scope(factory) as session:
        listing = list_reports(session, limit=10, offset=0)
    ids = {item.run_id: item.report_id for item in listing["items"]}
    with session_scope(factory) as session:
        plain = read_report(session, ids["as-rep-plain"])
    assert plain.content.metrics.asset_events == 1
    assert plain.content.metrics.asset_assets == 1
    assert [ref.body for ref in plain.content.assets] == [None]
    assert plain.content.assets[0].title == "一个方法问题"

    with session_scope(factory) as session:
        with_body = read_report(session, ids["as-rep-body"])
    assert with_body.content.assets[0].body == "这篇论文的对照设置存疑。"

    with session_scope(factory) as session:
        preview = delete_preview(session, asset_id)
        assert preview["revision_count"] == 1
        assert preview["reports_with_body"] == [ids["as-rep-body"]]
        assert set(preview["referenced_reports"]) == {ids["as-rep-plain"]}
        with pytest.raises(InvalidRequestError):
            delete_asset(
                session,
                asset_id,
                also_report_ids=[ids["as-rep-plain"]],
            )
        removed = delete_asset(session, asset_id, also_report_ids=[ids["as-rep-body"]])
        assert removed == [ids["as-rep-body"]]
        archives = len(session.scalars(select(ArchiveSnapshotRow)).all())
        assert archives == 2  # 阅读量与归档不变
    assert client.get(f"/reading-insights/reports/{ids['as-rep-body']}").status_code == 404
    # 未勾选的报告仅显示「来源已移除」。
    kept = client.get(f"/ui/reading-insights/reports/{ids['as-rep-plain']}")
    assert kept.status_code == 200
    assert "来源已移除" in kept.text
    assert "一个方法问题" not in kept.text


def test_asset_body_html_is_escaped_on_page(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(
            session,
            create_input(
                title="带 <b>标签</b> 的标题",
                body="<script>alert(1)</script> 正文",
                idempotency_key="asset-html-1",
            ),
            now=NOW5,
        )
    page = client.get(f"/ui/reading-insights/assets/{view.asset_id}")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


def test_workspace_reset_clears_asset_tables(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        create_asset(session, create_input(), now=NOW5)
    with session_scope(factory) as session:
        clear_workspace(session, keep_model=False)
        assert len(session.scalars(select(ReadingAssetRow)).all()) == 0
        assert len(session.scalars(select(ReadingAssetRevisionRow)).all()) == 0
        assert len(session.scalars(select(ReadingAssetSourceRow)).all()) == 0


def test_paper_detail_lists_assets_and_keeps_context(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    response = client.post(
        "/ui/reading-insights/assets",
        data={
            "source_paper": "as-p1",
            "idempotency_key": "asset-page-1",
            "kind": "problem",
            "title": "页面创建的问题",
            "body": "页面创建的正文",
            "sources": "",
            "applicability": "",
        },
        headers=HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/papers/as-p1")
    detail = client.get("/ui/papers/as-p1?asset_created=1")
    assert "已保存这条资产" in detail.text
    assert "相关资产（1）" in detail.text
    assert "页面创建的问题" in detail.text

    listing = client.get("/ui/reading-insights/assets?kind=problem")
    assert "页面创建的问题" in listing.text


def test_revision_conflict_on_page_preserves_draft(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(session, create_input(), now=NOW5)
        append_revision(
            session,
            view.asset_id,
            revision_input(expected=1),
            now=datetime(2026, 10, 27, 3, tzinfo=UTC),
        )
    stale = client.post(
        f"/ui/reading-insights/assets/{view.asset_id}/revisions",
        data={
            "expected_revision": "1",
            "title": "另一个窗口的修改",
            "body": "另一个窗口的正文",
            "status": "open",
            "sources": "as-p1",
        },
        headers=HEADERS,
    )
    assert stale.status_code == 409
    assert "已被其他页面更新" in stale.text
    assert ">另一个窗口的正文</textarea>" in stale.text  # 草稿完整保留且不重复


def test_report_and_overview_show_asset_activity(client, migrated_engine):
    activate(client)
    seed_two_archives(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        view = create_asset(session, create_input(), now=datetime(2026, 10, 22, 3, tzinfo=UTC))
        append_revision(
            session,
            view.asset_id,
            revision_input(expected=1),
            now=datetime(2026, 10, 27, 3, tzinfo=UTC),
        )
    overview = client.get(f"/ui/reading-insights?kind=week&anchor_date={ANCHOR}")
    assert "资产整理活动" in overview.text
    assert '<a href="/ui/reading-insights/assets">手工资产</a>' in overview.text

    manager = client.app.state.ui_flows
    # 第二次修订发生在周期截止之后：当时记录不把它计入。
    manager.submit(
        FlowRequest(
            run_id="as-ov-1",
            kind="review",
            review=ReviewParams.model_validate({"period": "week", "anchor_date": ANCHOR}),
        )
    )
    wait(manager, "as-ov-1")
    with session_scope(factory) as session:
        listing = list_reports(session, limit=5, offset=0)
    with session_scope(factory) as session:
        report = read_report(session, listing["items"][0].report_id)
    assert report.content.metrics.asset_events == 1  # 仅创建在周期内
    assert report.content.metrics.asset_assets == 1
