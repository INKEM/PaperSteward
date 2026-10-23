"""显式阅读反馈：客户端幂等、状态/评分/审计同事务，备注不进入模型。"""

import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from papersteward.core.entities import Event, Rating
from papersteward.core.enums import EventType, PaperStatus, TagRole
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import Action
from papersteward.errors import NotFoundError
from papersteward.reading.archive import archive_done
from papersteward.scoring.queue import eligibility, explain_score, queue_context
from papersteward.storage.delivery import content_hash
from papersteward.storage.repositories import (
    EventRepository,
    PaperRepository,
    PaperTagRepository,
    RatingRepository,
)
from papersteward.storage.tx import atomic


def apply_action(
    session: Session, paper_id: str, action: Action, *, now: datetime
) -> dict[str, Any]:
    action = Action.model_validate(action.model_dump(exclude_unset=True))
    request_hash = content_hash(
        {"paper_id": paper_id, **action.model_dump(mode="json", exclude_unset=True)}
    )
    receipt_id = hashlib.sha256(("reading:" + action.request_id).encode()).hexdigest()
    try:
        with atomic(session):
            ctx = queue_context(session, now)
            from papersteward.pipeline.context import runtime_local_day

            day = runtime_local_day(session, now, ctx["binding"])
            receipts = EventRepository(session)
            existing = receipts.get(receipt_id)
            if existing is not None:
                if existing.payload_json.get("request_hash") != request_hash:
                    raise InvariantViolation("action.request_id", "请求ID已用于不同操作")
                return dict(existing.payload_json["result"])
            repo = PaperRepository(session)
            paper = repo.get(paper_id)
            if paper is None:
                raise NotFoundError("文献不存在")
            payload: dict[str, Any] = {"request_id": action.request_id, "kind": action.kind}

            def transition(target: PaperStatus) -> None:
                repo.transition(
                    paper_id, target, event_id=uuid4().hex, occurred_at=now, payload=payload
                )

            def event(kind: EventType, values: dict[str, Any]) -> None:
                receipts.append(Event(uuid4().hex, kind, now, paper_id, {**payload, **values}))

            ratings = RatingRepository(session).list_for_paper(paper_id)
            last = ratings[-1] if ratings else None
            if action.kind == "done":
                if paper.status not in {
                    PaperStatus.QUEUED,
                    PaperStatus.PUSHED,
                    PaperStatus.READING,
                }:
                    raise InvariantViolation("action.done", "只能完成已入队、已推送或阅读中的文献")
                reasons, _, _ = eligibility(session, paper, ctx, day)
                if [r for r in reasons if not r.startswith("ineligible_status:")]:
                    raise InvariantViolation("action.done", "摘要/分类/评分依据未就绪")
                explain_score(session, paper_id, now=now)
                tags = PaperTagRepository(session).list_for_paper(paper_id)
                primary = [t.tag_code for t in tags if t.role is TagRole.PRIMARY]
                secondaries = sorted(
                    (t for t in tags if t.role is TagRole.SECONDARY),
                    key=lambda t: (-t.confidence, t.tag_code),
                )
                if paper.status is not PaperStatus.READING:
                    transition(PaperStatus.READING)
                transition(PaperStatus.DONE)
                RatingRepository(session).add(
                    Rating(
                        paper_id,
                        action.score,
                        action.note,
                        tuple(primary + [t.tag_code for t in secondaries]),
                        action.mode,
                        now,
                    )
                )
                archive_done(session, paper_id, now=now)
            elif action.kind in {"score", "note"}:
                if paper.status not in {PaperStatus.DONE, PaperStatus.ARCHIVED} or last is None:
                    raise InvariantViolation("action.feedback", "只能修订已完成文献的反馈")
                if now <= last.created_at:
                    raise InvariantViolation("action.time", "修订时间须晚于上条反馈，请稍后重试")
                score = action.score if action.kind == "score" else last.score
                note = action.note if action.kind == "note" else last.note
                RatingRepository(session).add(
                    Rating(paper_id, score, note, last.tags_at_read, last.read_mode, now)
                )
                event(
                    EventType.PAPER_RATED if action.kind == "score" else EventType.PAPER_NOTE_ADDED,
                    {
                        "old": last.score if action.kind == "score" else last.note,
                        "new": score if action.kind == "score" else note,
                    },
                )
            elif action.kind == "restore":
                if paper.status is not PaperStatus.REJECTED:
                    raise InvariantViolation(
                        "action.restore", "只有被标记为不感兴趣的文献才能恢复推荐"
                    )
                transition(PaperStatus.SCREENED)
            else:
                if paper.status not in {
                    PaperStatus.QUEUED,
                    PaperStatus.PUSHED,
                    PaperStatus.READING,
                }:
                    raise InvariantViolation("action.status", "只能将阅读队列中的文献标为不感兴趣")
                payload["reason"] = action.note
                transition(PaperStatus.REJECTED)
            current = repo.get(paper_id)
            assert current is not None
            result = {
                "paper_id": paper_id,
                "status": current.status.value,
                "kind": action.kind,
                "request_id": action.request_id,
            }
            receipts.append(
                Event(
                    receipt_id,
                    EventType.ACTION_RECORDED,
                    now,
                    paper_id,
                    {"request_hash": request_hash, "result": result},
                )
            )
            return result
    except IntegrityError as exc:
        raise InvariantViolation(
            "action.concurrent", "操作已被其他请求写入；使用同request_id重试"
        ) from exc
