from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from test_priority_queue import DAY, NOW, seed
from test_ui_flows import HEADERS, post, ready, request, wait

from conftest import activate
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.delivery.actions import apply_action
from papersteward.delivery.contracts import Action
from papersteward.scoring.contracts import BacklogFilters
from papersteward.scoring.queue import explain_score, read_backlog, refresh_queue
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import DigestRow, EventRow, PaperRow
from papersteward.storage.repositories import PaperRepository, ScoreSnapshotRepository


def test_backlog_persists_without_daily_snapshot_and_can_be_read(client, migrated_engine):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session, "older")
        report = refresh_queue(session, now=NOW)
        assert report.added_ids == ["older"] and report.queue_total == 1
        later = NOW + timedelta(days=15)
        page = read_backlog(session, now=later)
        assert page.total == 1 and page.items[0].score_date == DAY
        assert page.items[0].rank == 1
        assert explain_score(session, "older", now=later).score_date == DAY
        assert len(ScoreSnapshotRepository(session).history("older")) == 1
        assert session.scalar(select(func.count()).select_from(DigestRow)) == 0
        apply_action(
            session, "older", Action(kind="done", request_id="finish", mode="skim"), now=later
        )
        assert read_backlog(session, now=later).total == 0
        assert (
            read_backlog(session, now=later, filters=BacklogFilters(read_status="read")).total == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(EventRow.event_type == "paper.pushed")
            )
            == 0
        )


def test_refresh_adds_new_papers_and_never_requeues_finished_or_rejected(client, migrated_engine):
    activate(client, paused=True, quiet_start="00:01", quiet_end="23:59", push_time="00:00")
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "first")
        assert refresh_queue(session, now=NOW).queue_total == 1
        apply_action(
            session, "first", Action(kind="reject", note="不关注", request_id="reject"), now=NOW
        )
        seed(session, "second")
        report = refresh_queue(session, now=NOW)
        assert report.added_ids == ["second"]
        assert refresh_queue(session, now=NOW).added_ids == []
        assert PaperRepository(session).get("first").status is PaperStatus.REJECTED
        seed(session, "third")
        assert refresh_queue(session, now=NOW).queue_total == 2
        assert len(ScoreSnapshotRepository(session).history("second")) == 1
        assert refresh_queue(session, now=NOW + timedelta(days=1)).created == 2
        assert read_backlog(session, now=NOW + timedelta(days=1)).items[
            0
        ].score_date == DAY + timedelta(days=1)


def test_filters_ranks_and_score_rendering(client, migrated_engine, monkeypatch):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "one", r=95)
        seed(session, "two", r=80)
        refresh_queue(session, now=NOW)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW + timedelta(days=3))
    page = client.get("/ui/queue")
    assert page.status_code == 200
    assert "第 1 名" in page.text and "第 2 名" in page.text
    assert "综合评分" in page.text and str(DAY) in page.text
    assert "挖掘新论文" in page.text and "刷新排序" in page.text
    for unwanted in ("生成今日日报", 'name="days"', 'value="skip"', 'value="snooze"'):
        assert unwanted not in page.text
    detail = client.get("/ui/papers/one").text
    assert "为什么推荐这篇" in detail and "研究相关性" in detail and "<meter" in detail
    assert "static_input_hash" not in detail and "rounding_adjustment" not in detail
    assert client.get("/ui/queue?min_priority=101").status_code == 422
    # min>max 不再返回裸 JSON，而是带提示的 HTML 页面。
    bad_range = client.get("/ui/queue?min_priority=90&max_priority=10")
    assert bad_range.status_code == 200 and "最低分不能高于最高分" in bad_range.text
    assert client.get("/backlog?tag=unknown").json()["total"] == 0
    assert client.get("/queue").json()["items"][0]["rank"] == 1
    assert client.post("/ui/digests", headers=HEADERS, data={}).status_code == 404
    completed = client.post(
        "/ui/actions/one",
        headers={**HEADERS, "HX-Request": "true"},
        data={"kind": "done", "mode": "skim", "request_id": "ui-complete"},
    )
    assert completed.status_code == 200 and completed.headers["HX-Refresh"] == "true"
    assert client.get("/queue").json()["total"] == 1


def test_three_mining_runs_refresh_queue_and_zero_candidates_succeed(
    client, migrated_engine, monkeypatch
):
    """三次挖掘各自入队一篇；无候选的轮次与保底不达标都如实成功结束。"""
    from test_ui_flows import canned_plan

    manager = ready(client)
    monkeypatch.setattr(manager, "_plan", canned_plan())

    def collect(request, plan):
        with session_scope(manager.factory) as session:
            if PaperRepository(session).get(request.run_id) is None:
                seed(session, request.run_id)

    monkeypatch.setattr(manager, "collect", collect)
    for index in range(3):
        identity = f"mining-{index}"
        assert post(client, "/ui/runs", request(identity)).status_code == 200
        report = wait(manager, identity)
        assert report.status is RunStatus.SUCCEEDED
        assert report.stats_json["queue_total"] == index + 1
        assert report.stats_json["added_ids"] == [identity]
        assert post(client, "/ui/runs", request(identity)).status_code == 200
    with session_scope(manager.factory) as session:
        assert session.scalar(select(func.count()).select_from(DigestRow)) == 0
    # A separate, initially empty workspace is covered by the local sort refresh.
    assert post(client, "/ui/runs", request("local", kind="refresh")).status_code == 200
    assert wait(manager, "local").stats_json["queue_total"] == 3


def test_empty_refresh_is_success_without_model_configuration(client):
    activate(client)
    manager = client.app.state.ui_flows
    assert post(client, "/ui/runs", request("empty", kind="refresh")).status_code == 200
    report = wait(manager, "empty")
    assert report.status is RunStatus.SUCCEEDED
    assert report.stats_json["queue_message"] == "暂无合格候选"


def test_migration_restores_legacy_snoozed_and_preserves_audit(sqlite_engine):
    config = Config("alembic.ini")
    with sqlite_engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0017")
    from conftest import activate_workspace
    from papersteward.core.entities import Event
    from papersteward.core.enums import EventType
    from papersteward.storage.repositories import EventRepository

    activate_workspace(sqlite_engine)
    with session_scope(create_session_factory(sqlite_engine)) as session:
        seed(session, "legacy-snooze")
        refresh_queue(session, now=NOW)
        session.get(PaperRow, "legacy-snooze").status = "snoozed"
        session.flush()
        EventRepository(session).append(
            Event(
                "historical-snooze",
                EventType.PAPER_SNOOZED,
                NOW,
                "legacy-snooze",
                {"until": "2099-01-01"},
            )
        )
    with sqlite_engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    with session_scope(create_session_factory(sqlite_engine)) as session:
        assert session.get(PaperRow, "legacy-snooze").status == "queued"
        assert read_backlog(session, now=NOW).items[0].paper_id == "legacy-snooze"
        assert (
            EventRepository(session).get("historical-snooze").payload_json["until"] == "2099-01-01"
        )


@pytest.mark.parametrize("kind", ["snooze", "skip"])
def test_removed_reading_actions_are_rejected(client, kind):
    response = client.post("/papers/p/actions", json={"kind": kind, "request_id": "old"})
    assert response.status_code == 422
