from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_digest_delivery import prepared
from test_priority_queue import DAY, NOW, seed

from conftest import activate
from papersteward.cli.main import main
from papersteward.config import Settings, get_settings
from papersteward.core.enums import EventType, PaperStatus
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import Action
from papersteward.delivery.service import create_digest, deliver
from papersteward.reading.actions import apply_action
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import EventRow, RatingRow
from papersteward.storage.repositories import EventRepository, PaperRepository, RatingRepository


def test_actions_api_gate_done_idempotency_and_revisions(client, migrated_engine, monkeypatch):
    body = {
        "kind": "done",
        "mode": "deep",
        "score": 4,
        "note": "备注不外发",
        "request_id": "done-1",
    }
    assert client.post("/papers/p/actions", json=body).status_code == 409
    factory = prepared(client, migrated_engine)
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW)
    first = client.post("/papers/p/actions", json=body)
    assert first.status_code == 200 and first.json()["status"] == "archived"
    assert client.post("/papers/p/actions", json=body).json() == first.json()
    assert client.post("/papers/p/actions", json={**body, "score": 5}).status_code == 409
    assert client.post("/papers/p/actions", json={**body, "score": True}).status_code == 422
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW + timedelta(seconds=1))
    assert (
        client.post(
            "/papers/p/actions", json={"kind": "score", "score": 5, "request_id": "rate-1"}
        ).status_code
        == 200
    )
    monkeypatch.setattr("papersteward.api.actions.utc_now", lambda: NOW + timedelta(seconds=2))
    assert (
        client.post(
            "/papers/p/actions", json={"kind": "note", "note": "新备注", "request_id": "note-1"}
        ).status_code
        == 200
    )
    with session_scope(factory) as session:
        rows = RatingRepository(session).list_for_paper("p")
        assert [(r.score, r.note) for r in rows] == [
            (4, "备注不外发"),
            (5, "备注不外发"),
            (5, "新备注"),
        ]
        assert rows[0].tags_at_read == ("T01",)
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(EventRow.event_type == "paper.done")
            )
            == 1
        )


def test_queue_can_wait_without_date_or_hidden_cooldown(client, migrated_engine):
    from papersteward.scoring.queue import read_backlog, refresh_queue

    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        later = NOW + timedelta(days=14)
        assert read_backlog(session, now=later).total == 1
        refresh_queue(session, now=later)
        apply_action(
            session, "p", Action(request_id="reject", kind="reject", note="无关原因"), now=later
        )
        assert PaperRepository(session).get("p").status is PaperStatus.REJECTED
        assert read_backlog(session, now=later).total == 0
        assert session.scalar(select(func.count()).select_from(RatingRow)) == 0


def test_actions_atomic_failure_and_outer_rollback(client, migrated_engine, monkeypatch):
    factory = prepared(client, migrated_engine)
    original = EventRepository.append

    def broken(self, event):
        if event.event_type is EventType.ACTION_RECORDED:
            raise RuntimeError("receipt failure")
        return original(self, event)

    request = Action(request_id="done", kind="done", mode="skim")
    with session_scope(factory) as session:
        with monkeypatch.context() as m:
            m.setattr(EventRepository, "append", broken)
            with pytest.raises(RuntimeError):
                apply_action(session, "p", request, now=NOW)
        assert PaperRepository(session).get("p").status is PaperStatus.QUEUED
        assert not RatingRepository(session).list_for_paper("p")
    with factory() as session:
        apply_action(session, "p", request, now=NOW)
        session.rollback()
    with session_scope(factory) as session:
        assert PaperRepository(session).get("p").status is PaperStatus.QUEUED


def test_rejected_legacy_digest_cannot_be_delivered(client, migrated_engine, tmp_path):
    factory = prepared(client, migrated_engine)
    with session_scope(factory) as session:
        apply_action(
            session, "p", Action(request_id="reject", kind="reject", note="不关注"), now=NOW
        )
    with pytest.raises(InvariantViolation, match="拒绝"):
        deliver(factory, DAY, now=NOW, settings=Settings(_env_file=None), out=tmp_path / "no.md")
    assert not (tmp_path / "no.md").exists()


def test_delivered_receipt_is_idempotent_after_done(client, migrated_engine, tmp_path):
    factory = prepared(client, migrated_engine)
    settings = Settings(_env_file=None)
    path = tmp_path / "sent.md"
    deliver(factory, DAY, now=NOW, settings=settings, out=path)
    with session_scope(factory) as session:
        apply_action(session, "p", Action(request_id="finish", kind="done", mode="skim"), now=NOW)
    assert deliver(factory, DAY, now=NOW, settings=settings, out=path)["existing"]


def test_prequeue_cannot_be_done_and_cli_can_read(client, migrated_engine, monkeypatch, capsys):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session)
        with pytest.raises(InvariantViolation):
            apply_action(
                session, "p", Action(request_id="early", kind="done", mode="skim"), now=NOW
            )
        create_digest(session, DAY, now=NOW)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    monkeypatch.setattr("papersteward.cli.reading.utc_now", lambda: NOW)
    get_settings.cache_clear()
    try:
        assert (
            main(
                [
                    "done",
                    "p",
                    "--mode",
                    "deep",
                    "--score",
                    "4",
                    "--note",
                    "CLI",
                    "--request-id",
                    "cli",
                ]
            )
            == 0
        )
        assert '"status": "archived"' in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_reject_can_be_restored_from_ui(client, migrated_engine):
    """不感兴趣支持移回待处理：队列卡片就地给出入口，详情页同样提供。"""
    factory = prepared(client, migrated_engine)
    headers = {"origin": "http://testserver"}
    rejected = client.post(
        "/ui/actions/p",
        data={"kind": "reject", "note": "不关注", "request_id": "rj-1"},
        headers=headers,
    )
    assert rejected.status_code == 200, rejected.text
    # UXR-10：按钮如实命名为「移回待处理」（实际迁移 REJECTED→SCREENED，不直接回队列）。
    assert "移回待处理" in rejected.text
    detail = client.get("/ui/papers/p")
    assert "已被标记为不感兴趣" in detail.text and "移回待处理" in detail.text
    # UXR-10：详情上下文「移回待处理」完成 REJECTED→SCREENED 迁移，
    # 返回完整正文与如实消息；SCREENED 详情页说明去向与下一步（UXR-09 内容保留）。
    ui_restored = client.post(
        "/ui/actions/p",
        data={"kind": "restore", "context": "detail", "request_id": "rs-1"},
        headers=headers,
    )
    assert ui_restored.status_code == 200
    assert "移回待处理状态" in ui_restored.text and "重新评估" in ui_restored.text
    assert "内容概括" in ui_restored.text
    with session_scope(factory) as session:
        assert PaperRepository(session).get("p").status is PaperStatus.SCREENED
    detail_after = client.get("/ui/papers/p")
    assert "移回待处理状态" in detail_after.text and "重新评估是否入队" in detail_after.text
    assert (
        client.post("/papers/p/actions", json={"kind": "restore", "request_id": "rs-2"}).status_code
        == 409
    )
