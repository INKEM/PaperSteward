"""G4日报编排与可恢复的投递边界。发送预约必须先提交，不能包在请求事务中。"""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from papersteward.config import Settings
from papersteward.core.entities import Event
from papersteward.core.enums import EventType, PaperStatus, TagRole
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.channels import send_smtp, smtp_destination, write_file
from papersteward.delivery.compose import DeliveryHistory, compose_digest
from papersteward.delivery.contracts import Digest
from papersteward.delivery.render import render_markdown
from papersteward.onboarding.service import _atomic
from papersteward.scoring.queue import _context, create_snapshots, read_queue
from papersteward.storage import session_scope
from papersteward.storage.delivery import append_digest, content_hash, read_digest
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import DeliveryRow, DigestRow, EventRow
from papersteward.storage.repositories import (
    EnrichmentRepository,
    EventRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ReviewRepository,
)


def audit(session: Session, kind: EventType, now: datetime, payload: dict[str, Any]) -> None:
    EventRepository(session).append(Event(uuid4().hex, kind, now, payload_json=payload))


def delivery_history(session: Session) -> dict[str, DeliveryHistory]:
    result: dict[str, DeliveryHistory] = {}
    sent_at: dict[str, datetime] = {}
    for row in session.scalars(
        select(DeliveryRow).where(DeliveryRow.status == "sent").order_by(DeliveryRow.digest_date)
    ):
        for card in read_digest(session, row.digest_date).items:
            result[card.paper_id] = DeliveryHistory(
                row.digest_date, card.priority, card.explanation.static_input_hash
            )
            sent_at[card.paper_id] = as_utc(row.finished_at or row.created_at)
    for event in session.scalars(
        select(EventRow)
        .where(
            EventRow.event_type.in_(
                [
                    EventType.PAPER_SKIPPED.value,
                    EventType.PAPER_SNOOZED.value,
                ]
            )
        )
        .order_by(EventRow.created_at, EventRow.id)
    ):
        key = event.paper_id
        if (
            key
            and "until" in event.payload_json
            and (key not in sent_at or as_utc(event.created_at) >= sent_at[key])
        ):
            old = result.get(key, DeliveryHistory())
            until = date.fromisoformat(event.payload_json["until"])
            result[key] = replace(
                old,
                skip_until=until if event.event_type == "paper.skipped" else None,
                snooze_until=until if event.event_type == "paper.snoozed" else None,
            )
    return result


def create_digest(
    session: Session, day: date, *, now: datetime, allow_downgrade: bool = False
) -> Digest:
    with _atomic(session):
        ctx = _context(session, now, execution=True)
        if day > now.astimezone(ZoneInfo(ctx["runtime"].value_json["timezone"])).date():
            raise InvariantViolation("digest.date", "不能生成未来日报")
        if session.get(DigestRow, day) is not None:
            return read_digest(session, day)
        history = delivery_history(session)
        # 发送状态不应妨碍下一日更新动态分；筛选/摘要/特殊桶门仍由P5复核。
        for paper in PaperRepository(session).list():
            old = history.get(paper.id)
            due = (
                paper.status is PaperStatus.SNOOZED
                and old
                and old.snooze_until
                and day >= old.snooze_until
            )
            if paper.status is PaperStatus.PUSHED or due:
                PaperRepository(session).transition(
                    paper.id,
                    PaperStatus.QUEUED,
                    event_id=uuid4().hex,
                    occurred_at=now,
                    payload={"stage": "digest_recheck"},
                )
        create_snapshots(session, day, now=now)
        candidates = []
        offset = 0
        while True:
            page = read_queue(session, day, now=now, limit=200, offset=offset)
            candidates.extend(page.items)
            offset += len(page.items)
            if offset >= page.total:
                break
        metadata = {}
        for item in candidates:
            candidate = PaperRepository(session).get(item.paper_id)
            assert candidate is not None
            paper = candidate
            enrichment = EnrichmentRepository(session).get(paper.id)
            filing = [
                b.bucket_id
                for b in PaperBucketRepository(session).list_for_paper(paper.id)
                if b.role.value == "filing"
            ]
            metadata[paper.id] = {
                "authors": list(paper.authors),
                "venue": paper.venue,
                "url": paper.url,
                "pdf_url": paper.pdf_url,
                "code_url": None if enrichment is None else enrichment.signals.code_url,
                "secondary_tags": [
                    t.tag_code
                    for t in PaperTagRepository(session).list_for_paper(paper.id)
                    if t.role is TagRole.SECONDARY
                ],
                "filing_bucket": filing[0] if filing else None,
            }
        digest = compose_digest(
            candidates,
            day=day,
            timezone=ctx["runtime"].value_json["timezone"],
            binding=ctx["binding"],
            config=ctx["config"],
            weekly_capacity=ctx["profile"].parsed_json["reading"]["weekly_capacity"],
            pending_review=ReviewRepository(session).list_open(limit=1)[1],
            history=history,
            metadata=metadata,
            allow_downgrade=allow_downgrade,
        )
        append_digest(session, digest, now)
        audit(
            session, EventType.DIGEST_CREATED, now, {"date": str(day), "count": len(digest.items)}
        )
        return digest


def _preflight(session: Session, digest: Digest, channel: str, now: datetime) -> None:
    ctx = _context(session, now, execution=True)
    if digest.binding != ctx["binding"]:
        raise InvariantViolation("digest.binding", "日报依据已变化")
    if channel not in {"local_markdown", "smtp"}:
        raise InvariantViolation("delivery.channel", "不支持的投递渠道")
    if channel == "smtp" and ctx["runtime"].value_json["channel"] != "smtp":
        raise InvariantViolation("delivery.channel", "G4未确认SMTP；本地文件仍可用")
    if digest.digest_date != now.astimezone(ZoneInfo(digest.timezone)).date():
        raise InvariantViolation("delivery.date", "只允许发送本地今日日报，历史日报可审阅导出")
    if (
        now.astimezone(ZoneInfo(digest.timezone)).strftime("%H:%M")
        < ctx["runtime"].value_json["push_time"]
    ):
        raise InvariantViolation("delivery.time", "尚未到已确认的推送时间")


def deliver(
    factory: sessionmaker[Session],
    day: date,
    *,
    now: datetime,
    settings: Settings,
    channel: str = "local_markdown",
    out: Path | None = None,
    recover_file: bool = False,
) -> dict[str, Any]:
    if channel == "local_markdown" and out is None:
        raise InvariantViolation("delivery.path", "本地文件投递必须提供输出路径")
    destination = (
        str(out.absolute()) if channel == "local_markdown" and out else smtp_destination(settings)
    )
    destination_hash = content_hash(destination)
    # UNIQUE主键是跨进程预约锁；事务在任何外部调用前提交。
    try:
        with session_scope(factory) as session:
            digest = read_digest(session, day)
            _preflight(session, digest, channel, now)
            row = session.get(DeliveryRow, (day, channel))
            if row is not None:
                if row.destination_hash != destination_hash:
                    raise InvariantViolation("delivery.destination", "同日报渠道不能更换投递目标")
                if row.status == "sent":
                    return {
                        "status": "sent",
                        "existing": True,
                        "date": str(day),
                        "channel": channel,
                    }
                if not (recover_file and channel == "local_markdown"):
                    raise InvariantViolation(
                        "delivery.status", "已有投递预约或送达不确定；禁止自动重发"
                    )
            else:
                session.add(
                    DeliveryRow(
                        digest_date=day,
                        channel=channel,
                        status="reserved",
                        destination_hash=destination_hash,
                        created_at=now,
                    )
                )
                session.flush()
                audit(
                    session,
                    EventType.DELIVERY_RESERVED,
                    now,
                    {"date": str(day), "channel": channel},
                )
    except IntegrityError as exc:
        raise InvariantViolation("delivery.status", "投递已被其他进程预约") from exc
    try:
        with session_scope(factory) as session:
            _preflight(session, digest, channel, now)
        if channel == "local_markdown":
            assert out is not None
            write_file(out, render_markdown(digest))
        else:
            send_smtp(digest, settings)
    except Exception as exc:
        with session_scope(factory) as session:
            row = session.get(DeliveryRow, (day, channel))
            assert row is not None
            row.status = "uncertain" if channel == "smtp" else "failed"
            row.error = (
                "邮件送达结果未知，请人工核对；未自动重试"
                if channel == "smtp"
                else "文件输出失败；检查路径后显式恢复"
            )
            audit(
                session,
                EventType.DELIVERY_FAILED,
                now,
                {"date": str(day), "channel": channel, "status": row.status},
            )
        raise InvariantViolation("delivery.failed", row.error) from exc
    # 若此事务失败，预约仍在；SMTP不会因客户端重试而再次发送，文件可显式恢复。
    with session_scope(factory) as session:
        row = session.get(DeliveryRow, (day, channel))
        assert row is not None
        row.status, row.finished_at, row.error = "sent", now, None
        for card in digest.items:
            paper = PaperRepository(session).get(card.paper_id)
            if paper is not None and paper.status is PaperStatus.QUEUED:
                PaperRepository(session).transition(
                    paper.id,
                    PaperStatus.PUSHED,
                    event_id=uuid4().hex,
                    occurred_at=now,
                    payload={"digest_date": str(day), "channel": channel},
                )
        audit(session, EventType.DELIVERY_SENT, now, {"date": str(day), "channel": channel})
    return {"status": "sent", "existing": False, "date": str(day), "channel": channel}
