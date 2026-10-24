from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from test_priority_queue import DAY, NOW, seed
from test_ui_flows import HEADERS, post, ready, request, wait

from conftest import activate
from papersteward.core.enums import PaperStatus, RunStatus
from papersteward.delivery.contracts import Action
from papersteward.reading.actions import apply_action
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
    # UXR-07：列表卡片明确写「推荐参考分」，与个人 1–5 分区分。
    assert "推荐参考分" in page.text and "综合评分" not in page.text and str(DAY) in page.text
    # 标题即详情入口，不再有重复的「查看论文与推荐依据」链接。
    assert "查看论文与推荐依据" not in page.text
    assert 'class="score-pill"' in page.text and 'class="card-actions"' in page.text
    assert "挖掘新论文" in page.text and "刷新排序" in page.text
    # UXR-04：挖掘表单如实说明检索规划会把研究档案与已读历史发给模型服务。
    assert "内容会发送给你配置的模型服务" in page.text
    for unwanted in ("生成今日日报", 'name="days"', 'value="skip"', 'value="snooze"'):
        assert unwanted not in page.text
    detail = client.get("/ui/papers/one").text
    assert "为什么推荐这篇" in detail and "研究相关性" in detail and "<meter" in detail
    assert "static_input_hash" not in detail and "rounding_adjustment" not in detail
    assert client.get("/ui/queue?min_priority=101").status_code == 422
    # UXR-02：min>max 返回带提示的 HTML 页面（状态码如实 422），不再返回裸 JSON。
    bad_range = client.get("/ui/queue?min_priority=90&max_priority=10")
    assert bad_range.status_code == 422 and "最低分不能高于最高分" in bad_range.text
    assert client.get("/backlog?tag=unknown").json()["total"] == 0
    assert client.get("/queue").json()["items"][0]["rank"] == 1
    assert client.post("/ui/digests", headers=HEADERS, data={}).status_code == 404
    completed = client.post(
        "/ui/actions/one",
        headers={**HEADERS, "HX-Request": "true"},
        data={"kind": "done", "mode": "skim", "request_id": "ui-complete"},
    )
    # X-03：完成阅读原地替换卡片（成功片段），不再整页刷新。
    assert completed.status_code == 200
    assert "HX-Refresh" not in completed.headers
    assert "已归档这篇论文" in completed.text
    # UXR-05：完成动作经 htmx 带外片段同步顶部未读统计（2 → 1）。
    assert 'queue-stats" hx-swap-oob="true"' in completed.text
    assert "未读 <strong>1</strong> 篇" in completed.text
    assert client.get("/queue").json()["total"] == 1


def test_stats_scope_distinguishes_unread_total_from_filtered_results(
    client, migrated_engine, monkeypatch
):
    """UXR-05：顶部统计始终是未读总数；列表行如实标注筛选结果数。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "one", r=95)
        seed(session, "two", r=80)
        refresh_queue(session, now=NOW)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW + timedelta(days=3))
    page = client.get("/ui/queue")
    assert "未读 <strong>2</strong> 篇" in page.text
    assert "共 2 篇" in page.text
    filtered = client.get("/ui/queue", params={"search": "one"})
    assert "未读 <strong>2</strong> 篇" in filtered.text
    assert "符合当前筛选 1 篇（未读共 2 篇）" in filtered.text
    # 查看已读/全部时建议仍基于未读总量，不把已读数当待阅读数。
    read_view = client.get("/ui/queue", params={"read_status": "all"})
    assert "未读 <strong>2</strong> 篇" in read_view.text


def test_invalid_filter_keeps_queue_list_container_for_htmx(client):
    """UXR-02 回归：非法筛选在局部刷新下返回稳定的 #queue-list 容器。

    此前片段请求的错误路径会返回整页外壳（或 FastAPI 的 422 JSON 信封），
    把 #queue-list 一起换掉，后续筛选报 htmx:targetError 永久失效。
    """
    activate(client)
    headers = {**HEADERS, "HX-Request": "true"}

    def fragment(**params):
        return client.get("/ui/queue", params={**params, "fragment": "1"}, headers=headers)

    invalid = fragment(min_priority="90", max_priority="10")
    assert invalid.status_code == 422
    assert 'id="queue-list"' in invalid.text
    assert "<html" not in invalid.text and "<h1" not in invalid.text
    assert "最低分不能高于最高分" in invalid.text
    for params in (
        {"min_priority": "101"},
        {"min_priority": "abc"},
        {"max_priority": "-5"},
        {"read_status": "bogus"},
        {"offset": "-1"},
        {"offset": "abc"},
        {"search": "长" * 201},
    ):
        response = fragment(**params)
        assert response.status_code == 422, params
        assert 'id="queue-list"' in response.text, params
        assert "<html" not in response.text, params
    # 错误之后正常筛选仍走同一容器：局部刷新链路没有断。
    recovered = fragment()
    assert recovered.status_code == 200 and 'id="queue-list"' in recovered.text
    # 整页请求（直接访问/刷新）得到完整外壳 + 返回链接。
    page = client.get("/ui/queue?min_priority=90&max_priority=10")
    assert page.status_code == 422 and "<html" in page.text
    assert "最低分不能高于最高分" in page.text and "返回阅读队列" in page.text


def test_queue_fragment_redirects_by_hx_header_before_activation(client):
    """UXR-02：未完成设置时，片段请求经 HX-Redirect 整页跳转，
    不把设置页外壳换进列表容器。"""
    response = client.get(
        "/ui/queue", params={"fragment": "1"}, headers={**HEADERS, "HX-Request": "true"}
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/ui/setup"
    full = client.get("/ui/queue", follow_redirects=False)
    assert full.status_code == 303 and full.headers["location"] == "/ui/setup"


def test_filtered_empty_offers_one_click_clear(client, migrated_engine, monkeypatch):
    """UXR-19：筛选后空结果给「清除筛选」直接行动，链接为无参数干净地址。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "one", r=95)
        refresh_queue(session, now=NOW)
    monkeypatch.setattr("papersteward.api.pages.utc_now", lambda: NOW + timedelta(days=3))
    empty = client.get("/ui/queue", params={"search": "不存在的词"})
    assert "没有论文符合当前筛选" in empty.text
    assert '<a class="button secondary" href="/ui/queue">清除筛选，显示全部</a>' in empty.text
    fragment = client.get(
        "/ui/queue",
        params={"search": "不存在的词", "fragment": "1"},
        headers={**HEADERS, "HX-Request": "true"},
    )
    assert "清除筛选，显示全部" in fragment.text and 'id="queue-list"' in fragment.text
    # 有结果时结果行也提供清除入口。
    listed = client.get("/ui/queue", params={"search": "one"})
    assert 'href="/ui/queue">清除筛选</a>' in listed.text


def test_truly_empty_queue_hides_filter_form(client):
    """UXR-19：真空队列不渲染成排筛选控件，保留挖掘引导。"""
    activate(client)
    page = client.get("/ui/queue")
    assert "留一点时间，读重要的论文" in page.text
    assert 'class="queue-filters"' not in page.text
    assert "挖掘新论文" in page.text


def test_detail_presents_summary_slots_and_source_material(client, migrated_engine):
    """UXR-08：详情页先帮助理解论文——四槽概括、模型标注、证据折叠、推荐依据后置。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "one", r=95)
        refresh_queue(session, now=NOW)
    detail = client.get("/ui/papers/one").text
    assert "模型概括，请以原文为准" in detail
    assert "内容概括" in detail
    for label, value in (
        ("研究问题", "what"),
        ("研究方法", "how"),
        ("主要发现", "finding"),
        ("局限", "limitation"),
    ):
        assert f"<dt>{label}</dt><dd>{value}</dd>" in detail
    assert "可核查证据（论文原文片段）" in detail
    assert "<blockquote>evidence</blockquote>" in detail
    # 推荐依据仍在页面（默认折叠），阅读状态徽章可见
    assert "推荐依据（六维评分与相关性依据）" in detail and "为什么推荐这篇" in detail
    assert "队列中" in detail


def test_detail_archived_branch_also_shows_summary_slots(client, migrated_engine):
    """UXR-08：归档态详情同样呈现四槽内容概括。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "one", r=95)
        refresh_queue(session, now=NOW)
        apply_action(session, "one", Action(kind="done", request_id="fin", mode="skim"), now=NOW)
    detail = client.get("/ui/papers/one").text
    assert "已完成归档" in detail and "内容概括" in detail
    assert "<dt>研究问题</dt><dd>what</dd>" in detail
    assert "我的阅读反馈" in detail


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
