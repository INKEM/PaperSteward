"""手动/调度器可调用的周度U任务；复用P5算术，报告幂等，不读取备注。"""

from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import Event, Run
from papersteward.core.enums import EventType, RunStatus
from papersteward.core.errors import InvariantViolation
from papersteward.onboarding.service import _atomic
from papersteward.scoring.preference import weekly_cutoff
from papersteward.scoring.queue import ELIGIBLE, _context, _history, create_snapshots
from papersteward.storage.delivery import content_hash
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import EventRow, PaperRow, RatingRow, ScoreSnapshotRow
from papersteward.storage.repositories import EventRepository, RunRepository


def _check_data(session: Session, *, cutoff: str, timezone: ZoneInfo) -> dict[str, Any]:
    done = {
        row.paper_id: as_utc(row.created_at)
        for row in session.execute(
            select(EventRow.paper_id, EventRow.created_at)
            .where(EventRow.event_type == EventType.PAPER_DONE.value)
            .order_by(EventRow.created_at, EventRow.id)
        )
        if row.paper_id
    }
    invalid: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    orphaned = set()
    revisions = 0
    # 显式列投影，备注既不查询也不放进报告。
    for row in session.execute(
        select(
            RatingRow.paper_id,
            RatingRow.created_at,
            RatingRow.score,
            RatingRow.tags_at_read_json,
            RatingRow.read_mode,
        )
    ):
        created = as_utc(row.created_at)
        if created.astimezone(timezone).date().isoformat() >= cutoff:
            continue
        revisions += 1
        reasons = []
        if row.score is not None and (type(row.score) is not int or not 1 <= row.score <= 5):
            reasons.append("invalid_score")
        tags = row.tags_at_read_json
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
            reasons.append("invalid_reading_tags")
        elif len(set(tags)) != len(tags):
            reasons.append("duplicate_reading_tags")
        if isinstance(tags, list) and (not tags or len(tags) > 6):
            warnings.append({"paper_id": row.paper_id, "reason": "legacy_reading_tags"})
        if row.read_mode is None:
            warnings.append({"paper_id": row.paper_id, "reason": "legacy_missing_read_mode"})
        elif row.read_mode not in {"deep", "skim"}:
            reasons.append("invalid_read_mode")
        if row.paper_id not in done:
            orphaned.add(row.paper_id)
        elif created < done[row.paper_id]:
            reasons.append("rating_before_completion")
        invalid.extend({"paper_id": row.paper_id, "reason": reason} for reason in reasons)
    return {
        "revisions_before_cutoff": revisions,
        "invalid": invalid,
        "warnings": warnings,
        "orphan_rating_papers": sorted(orphaned),
    }


def refresh_weekly(session: Session, *, now: datetime, run_id: str | None = None) -> Run:
    with _atomic(session):
        ctx = _context(session, now, execution=True)
        timezone = ZoneInfo(ctx["runtime"].value_json["timezone"])
        day = now.astimezone(timezone).date()
        cutoff = weekly_cutoff(day, ctx["config"].quasi_dynamic.update_weekday)
        key = run_id or ("weekly-u-" + str(cutoff) + "-" + content_hash(ctx["binding"])[:16])
        existing = RunRepository(session).get(key)
        if existing is not None:
            if (
                existing.run_type != "weekly_preference"
                or existing.stats_json.get("cutoff") != str(cutoff)
                or existing.stats_json.get("binding") != ctx["binding"]
            ):
                raise InvariantViolation("weekly.run_id", "运行ID已用于其他周或配置")
            return existing
        checks = _check_data(session, cutoff=str(cutoff), timezone=timezone)
        stats: dict[str, Any] = {
            "cutoff": str(cutoff),
            "score_date": str(day),
            "binding": ctx["binding"],
            "data_checks": checks,
        }
        if checks["invalid"]:
            report = Run(
                key,
                "weekly_preference",
                RunStatus.FAILED,
                now,
                now,
                stats,
                {
                    "code": "invalid_rating_data",
                    "message": "评分数据不完整；请核对报告，不会改写评分或候选快照",
                },
            )
        else:
            _, feedback = _history(session, ctx, day)
            history = [f for f in feedback if f.completed_on < cutoff]
            rated = sum(f.rating is not None for f in history)
            coverage = rated / len(history) if history else 0.0
            snapshot = create_snapshots(session, day, now=now)
            scores = [
                {
                    "paper_id": row.paper_id,
                    "U": row.preference,
                    "confidence": row.breakdown_json.get("quasi", {}).get("confidence", 0.0),
                }
                for row in session.scalars(
                    select(ScoreSnapshotRow)
                    .join(PaperRow, PaperRow.id == ScoreSnapshotRow.paper_id)
                    .where(
                        ScoreSnapshotRow.score_date == day,
                        PaperRow.status.in_([s.value for s in ELIGIBLE]),
                    )
                )
            ]
            stats.update(
                completed_count=len(history),
                rated_count=rated,
                coverage=round(coverage, 4),
                weak_signal=coverage < 0.3,
                created=snapshot.created,
                existing=snapshot.existing,
                excluded=len(snapshot.excluded),
                scores=scores,
            )
            report = Run(key, "weekly_preference", RunStatus.SUCCEEDED, now, now, stats)
        RunRepository(session).add(report)
        EventRepository(session).append(
            Event(
                uuid4().hex,
                EventType.PREFERENCE_UPDATED,
                now,
                payload_json={"run_id": key, "cutoff": str(cutoff), "status": report.status.value},
            )
        )
        return report
