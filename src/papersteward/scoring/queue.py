"""绑定已确认运行参数的可解释主题队列；日快照追加、同日幂等，读取不写库。"""

from collections import Counter
from collections.abc import Set
from dataclasses import asdict
from datetime import date, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from papersteward.core.entities import Paper, ScoreSnapshot, StaticScoreSnapshot
from papersteward.core.enums import (
    BucketRole,
    BucketType,
    EventType,
    PaperStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import InvariantViolation
from papersteward.errors import NotFoundError
from papersteward.onboarding.service import _atomic
from papersteward.pipeline.context import bound_context
from papersteward.scoring.config import ScoringConfig
from papersteward.scoring.contracts import (
    BacklogFilters,
    BacklogItem,
    BacklogResponse,
    Component,
    Exclusion,
    QueueItem,
    QueueResponse,
    RefreshResponse,
    ScoreExplanation,
    SnapshotResponse,
)
from papersteward.scoring.dynamic import Completion, compute_dynamic, natural_day, target_shares
from papersteward.scoring.preference import Feedback, compute_preference, weekly_cutoff
from papersteward.scoring.priority import compute_priority, rank_key
from papersteward.scoring.service import read_scoring_config
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ArchiveSnapshotRow, EventRow, RatingRow
from papersteward.storage.repositories import (
    BucketRepository,
    ConfigRepository,
    PaperAnalysisRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ScoreSnapshotRepository,
    StaticScoreSnapshotRepository,
    SummaryRepository,
    TaxonomyRepository,
)

ELIGIBLE = frozenset({PaperStatus.CLASSIFIED, PaperStatus.SCORED, PaperStatus.QUEUED})
# 周任务只读候选：已投递未读同样需要当日U；不重新入队，避免改变投递历史。
WEEKLY_ELIGIBLE = ELIGIBLE | {PaperStatus.PUSHED}
BACKLOG_STATUSES = frozenset({PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.READING})
READ_STATUSES = frozenset({PaperStatus.DONE, PaperStatus.ARCHIVED})


def _context(
    session: Session,
    now: datetime,
    *,
    execution: bool = False,
    day: date | None = None,
) -> dict[str, Any]:
    profile, binding = bound_context(
        session, now, action="queue", enforce_schedule=execution, day=day
    )
    config = read_scoring_config(session, version=binding["scoring_version"])
    runtime = ConfigRepository(session).get("onboarding_runtime", binding["runtime_version"])
    taxonomy = TaxonomyRepository(session).get(binding["taxonomy_id"], binding["taxonomy_version"])
    assert config is not None and runtime is not None and taxonomy is not None
    tags = [t.code for t in taxonomy.tags if t.active and t.level is TagLevel.PRIMARY]
    if set(config.dynamic.by_primary_tag) - set(tags):
        raise InvariantViolation("dynamic.by_primary_tag", "包含未知主标签")
    return {
        "profile": profile,
        "binding": binding,
        "config": config,
        "taxonomy": taxonomy,
        "runtime": runtime,
        "shares": target_shares(tags, runtime.value_json["target_shares"]),
    }


def _eligibility(
    session: Session,
    paper: Paper,
    ctx: dict[str, Any],
    day: date,
    *,
    eligible: Set[PaperStatus] = ELIGIBLE,
) -> tuple[list[str], str | None, StaticScoreSnapshot | None]:
    reasons: list[str] = []
    buckets = PaperBucketRepository(session).list_for_paper(paper.id)
    filing = [b for b in buckets if b.role is BucketRole.FILING]
    bucket = BucketRepository(session).get(filing[0].bucket_id) if len(filing) == 1 else None
    if bucket is not None and bucket.bucket_type is not BucketType.TOPIC:
        return ["special_bucket"], None, None
    if paper.status not in eligible:
        reasons.append("ineligible_status:" + paper.status.value)
    assigned = PaperTagRepository(session).list_for_paper(paper.id)
    primary = [t.tag_code for t in assigned if t.role is TagRole.PRIMARY]
    tag = primary[0] if len(primary) == 1 else None
    if tag not in ctx["shares"] or bucket is None or not bucket.active or bucket.tag_code != tag:
        reasons.append("invalid_classification_or_filing")
    if any(t.taxonomy_version != ctx["binding"]["taxonomy_version"] for t in assigned):
        reasons.append("classification_version_mismatch")
    summary = SummaryRepository(session).get(paper.id)
    if summary is None or not summary.summary_100.strip():
        reasons.append("summary_unavailable")
    static = next(
        (
            s
            for s in reversed(StaticScoreSnapshotRepository(session).history(paper.id))
            if s.score_date <= day
        ),
        None,
    )
    if static is None:
        reasons.append("missing_static_snapshot")
    else:
        binding = ctx["binding"]
        if (static.config_version, static.profile_version, static.taxonomy_version) != (
            binding["scoring_version"],
            binding["profile_version"],
            binding["taxonomy_version"],
        ):
            reasons.append("static_version_mismatch")
        if static.relevance < max(55, ctx["config"].gates.min_relevance_for_push):
            reasons.append("below_relevance_gate")
    return reasons, tag, static


def _history(
    session: Session, ctx: dict[str, Any], day: date
) -> tuple[list[Completion], list[Feedback]]:
    timezone = ZoneInfo(ctx["runtime"].value_json["timezone"])
    cutoff = weekly_cutoff(day, ctx["config"].quasi_dynamic.update_weekday)
    done: dict[str, EventRow] = {}
    for event in session.scalars(
        select(EventRow)
        .options(load_only(EventRow.id, EventRow.paper_id, EventRow.created_at))
        .where(EventRow.event_type == EventType.PAPER_DONE.value)
        .order_by(EventRow.created_at, EventRow.id)
    ):
        if event.paper_id and as_utc(event.created_at).astimezone(timezone).date() <= day:
            done[event.paper_id] = event
    completions = []
    feedback = []
    for paper_id, event in done.items():
        paper = PaperRepository(session).get(paper_id)
        if paper is None or paper.status not in {PaperStatus.DONE, PaperStatus.ARCHIVED}:
            continue
        completed_on = as_utc(event.created_at).astimezone(timezone).date()
        rows = list(
            session.scalars(
                select(RatingRow)
                .options(
                    load_only(
                        RatingRow.paper_id,
                        RatingRow.created_at,
                        RatingRow.score,
                        RatingRow.tags_at_read_json,
                    )
                )
                .where(RatingRow.paper_id == paper_id)
                .order_by(RatingRow.created_at)
            )
        )
        # 截止前最后一条修订；未来修订不改变已经冻结的一周。
        rating = next(
            (
                r
                for r in reversed(rows)
                if as_utc(r.created_at).astimezone(timezone).date() < cutoff
            ),
            None,
        )
        tags = PaperTagRepository(session).list_for_paper(paper_id)
        primary = next((t.tag_code for t in tags if t.role is TagRole.PRIMARY), None)
        # 只投影阅读时元数据与标签，绝不加载归档正文中的备注或Rating.note。
        archived = session.execute(
            select(
                ArchiveSnapshotRow.primary_tag,
                ArchiveSnapshotRow.secondary_tags_json,
                ArchiveSnapshotRow.content_json["metadata"].label("metadata_at_read"),
            ).where(ArchiveSnapshotRow.paper_id == paper_id)
        ).one_or_none()
        metadata = {} if archived is None else (archived.metadata_at_read or {})
        if archived is not None:
            primary = archived.primary_tag
        saved_tags = tuple(rating.tags_at_read_json) if rating is not None else ()
        if not saved_tags and archived is not None:
            saved_tags = (archived.primary_tag, *(archived.secondary_tags_json or []))
        primary = next((t for t in saved_tags if t in ctx["shares"]), primary)
        if primary is not None and primary in ctx["shares"]:
            completions.append(Completion(primary, completed_on))
        feedback.append(
            Feedback(
                paper_id,
                completed_on,
                None if rating is None else rating.score,
                saved_tags or tuple(t.tag_code for t in tags),
                tuple(metadata.get("authors", paper.authors)),
                metadata.get("venue", paper.venue),
            )
        )
    return completions, feedback


def _build(
    session: Session,
    paper: Paper,
    tag: str,
    static: StaticScoreSnapshot,
    ctx: dict[str, Any],
    day: date,
    completions: list[Completion],
    feedback: list[Feedback],
    backlog: int,
) -> ScoreSnapshot:
    config: ScoringConfig = ctx["config"]
    dynamic = compute_dynamic(
        primary_tag=tag,
        published_on=None if paper.published_at is None else paper.published_at.date(),
        as_of=day,
        shares=ctx["shares"],
        completions=completions,
        config=config,
        backlog=backlog,
        weekly_capacity=ctx["profile"].parsed_json["reading"]["weekly_capacity"],
    )
    tags = PaperTagRepository(session).list_for_paper(paper.id)
    secondaries = sorted(
        (t for t in tags if t.role is TagRole.SECONDARY), key=lambda t: (-t.confidence, t.tag_code)
    )
    quasi = compute_preference(
        paper_id=paper.id,
        primary_tag=tag,
        secondary_tags=[t.tag_code for t in secondaries],
        authors=paper.authors,
        venue=paper.venue,
        feedback=feedback,
        as_of=day,
        config=config,
        parents={t.code: t.parent_code for t in ctx["taxonomy"].tags if t.parent_code},
    )
    previous = ScoreSnapshotRepository(session).latest(paper.id)
    if previous is not None and previous.score_date >= day:
        raise InvariantViolation("score_date", "不能倒序补算日快照")
    old = {} if previous is None else previous.breakdown_json
    reset = old.get("static_input_hash") != static.input_hash
    prior = old.get("scores", {}).get("rank_score")
    priority = compute_priority(
        static.static_score,
        dynamic.dynamic_score,
        quasi.preference,
        config,
        previous_rank=prior,
        reset=reset,
    )
    values = {
        "R": static.relevance,
        "A": static.authority,
        "E": static.extra_value,
        "F": dynamic.freshness,
        "H": dynamic.hunger,
        "U": quasi.preference,
    }
    weights = {
        "R": config.static.weight * config.static.relevance,
        "A": config.static.weight * config.static.authority,
        "E": config.static.weight * config.static.extra_value,
        "F": config.dynamic.weight * config.dynamic.freshness,
        "H": config.dynamic.weight * config.dynamic.hunger,
        "U": config.quasi_dynamic.weight,
    }
    confidence = {
        "R": 0.0,
        "A": static.confidence,
        "E": static.confidence,
        "F": 0.0 if dynamic.age_days is None else 1.0,
        "H": min(1, dynamic.total_completed / max(1, config.dynamic.prior_strength)),
        "U": quasi.confidence,
    }
    summary = SummaryRepository(session).get(paper.id)
    evidence: list[dict[str, Any]] = [{"stage": "static", "details": dict(static.breakdown_json)}]
    if summary is not None:
        evidence.append({"stage": "summary", "quotes": list(summary.evidence)})
    analysis = PaperAnalysisRepository(session).get(paper.id)
    if analysis is not None:
        evidence.extend(
            {"stage": name, "details": analysis.payload_json.get(name)}
            for name in ("relevance", "classification")
        )
        confidence["R"] = (
            analysis.payload_json.get("relevance", {}).get("rubric", {}).get("confidence", 0.0)
        )
    reasons = list(dynamic.reasons + quasi.reasons)
    if analysis is None:
        reasons.append("relevance_evidence_unavailable")
    if priority.delta_limited:
        reasons.append("large_daily_change_limited")
    explanation = ScoreExplanation(
        paper_id=paper.id,
        score_date=day,
        config_version=static.config_version,
        profile_version=static.profile_version,
        taxonomy_version=static.taxonomy_version,
        static_input_hash=static.input_hash,
        primary_tag=tag,
        components={
            k: Component(
                score=v,
                weight=weights[k],
                contribution=round(v * weights[k], 4),
                confidence=confidence[k],
            )
            for k, v in values.items()
        },
        layer_weights={
            "S": config.static.weight,
            "D": config.dynamic.weight,
            "U": config.quasi_dynamic.weight,
        },
        scores={
            "rounding_adjustment": round(
                priority.priority - sum(values[k] * weights[k] for k in values), 4
            ),
            "S": static.static_score,
            "D": dynamic.dynamic_score,
            "U": quasi.preference,
            "P": priority.priority,
            "rank_score": priority.rank_score,
        },
        dynamic=asdict(dynamic),
        quasi=asdict(quasi),
        stability=asdict(priority),
        evidence=evidence,
        reasons=reasons,
        binding=ctx["binding"],
    )
    return ScoreSnapshot(
        paper_id=paper.id,
        score_date=day,
        relevance=static.relevance,
        authority=static.authority,
        extra_value=static.extra_value,
        static_score=static.static_score,
        freshness=dynamic.freshness,
        hunger=dynamic.hunger,
        dynamic_score=dynamic.dynamic_score,
        preference=quasi.preference,
        priority=priority.priority,
        config_version=static.config_version,
        profile_version=static.profile_version,
        taxonomy_version=static.taxonomy_version,
        breakdown_json=explanation.model_dump(mode="json"),
    )


def create_snapshots(
    session: Session,
    day: date,
    *,
    now: datetime,
    eligible: Set[PaperStatus] = ELIGIBLE,
) -> SnapshotResponse:
    """追加当日快照；周任务可传入 ``WEEKLY_ELIGIBLE`` 以覆盖已投递未读候选。"""
    natural_day(day)
    with _atomic(session):
        ctx = _context(session, now, execution=True, day=day)
        if day > now.astimezone(ZoneInfo(ctx["runtime"].value_json["timezone"])).date():
            raise InvariantViolation("score_date", "不能生成未来日快照")
        candidates = []
        excluded = []
        for paper in PaperRepository(session).list():
            reasons, tag, static = _eligibility(session, paper, ctx, day, eligible=eligible)
            if reasons:
                excluded.append(Exclusion(paper_id=paper.id, reasons=reasons))
            else:
                assert tag is not None and static is not None
                candidates.append((paper, tag, static))
        backlog = Counter(tag for _, tag, _ in candidates)
        completions, feedback = _history(session, ctx, day)
        created = existing = 0
        for paper, tag, static in candidates:
            repo = ScoreSnapshotRepository(session)
            same = next((s for s in repo.history(paper.id) if s.score_date == day), None)
            if same is not None:
                if (
                    same.breakdown_json.get("static_input_hash") != static.input_hash
                    or same.breakdown_json.get("primary_tag") != tag
                ):
                    raise InvariantViolation(
                        "score_date", "同日依据已变化；保留旧快照，请使用新日期"
                    )
                existing += 1
            else:
                repo.append(
                    _build(
                        session, paper, tag, static, ctx, day, completions, feedback, backlog[tag]
                    )
                )
                created += 1
            for target in (
                (PaperStatus.SCORED, PaperStatus.QUEUED)
                if paper.status is PaperStatus.CLASSIFIED
                else (PaperStatus.QUEUED,)
                if paper.status is PaperStatus.SCORED
                else ()
            ):
                PaperRepository(session).transition(
                    paper.id,
                    target,
                    event_id=uuid4().hex,
                    occurred_at=now,
                    payload={"stage": "priority", "score_date": day.isoformat()},
                )
        return SnapshotResponse(
            score_date=day, created=created, existing=existing, excluded=excluded
        )


def _item(session: Session, paper: Paper, snapshot: ScoreSnapshot) -> QueueItem:
    explanation = ScoreExplanation.model_validate(snapshot.breakdown_json)
    summary = SummaryRepository(session).get(paper.id)
    assert summary is not None and snapshot.priority is not None
    return QueueItem(
        paper_id=paper.id,
        title=paper.title,
        primary_tag=explanation.primary_tag,
        summary_100=summary.summary_100,
        published_on=None if paper.published_at is None else paper.published_at.date(),
        score_date=snapshot.score_date,
        priority=snapshot.priority,
        rank_score=explanation.scores["rank_score"],
        explanation=explanation,
    )


def read_queue(
    session: Session, day: date, *, now: datetime, limit: int = 50, offset: int = 0
) -> QueueResponse:
    natural_day(day)
    if not 1 <= limit <= 200 or offset < 0:
        raise InvariantViolation("pagination", "limit须为1–200，offset非负")
    ctx = _context(session, now, day=day)
    items = []
    for paper in PaperRepository(session).list():
        reasons, tag, static = _eligibility(session, paper, ctx, day)
        if reasons:
            continue
        snapshot = next(
            (s for s in ScoreSnapshotRepository(session).history(paper.id) if s.score_date == day),
            None,
        )
        if (
            snapshot is not None
            and snapshot.priority is not None
            and "scores" in snapshot.breakdown_json
            and static is not None
            and snapshot.breakdown_json.get("static_input_hash") == static.input_hash
            and snapshot.breakdown_json.get("primary_tag") == tag
        ):
            items.append(_item(session, paper, snapshot))
    items.sort(
        key=lambda item: rank_key(
            rank_score=item.rank_score,
            relevance=item.explanation.components["R"].score,
            published_on=item.published_on,
            authority=item.explanation.components["A"].score,
            title=item.title,
            paper_id=item.paper_id,
        )
    )
    return QueueResponse(
        items=items[offset : offset + limit], total=len(items), limit=limit, offset=offset
    )


def explain_score(
    session: Session, paper_id: str, *, now: datetime, day: date | None = None
) -> ScoreExplanation | Exclusion:
    ctx = _context(session, now, day=day)
    target_day = day or now.astimezone(ZoneInfo(ctx["runtime"].value_json["timezone"])).date()
    paper = PaperRepository(session).get(paper_id)
    if paper is None:
        raise NotFoundError("文献不存在")
    reasons, _, _ = _eligibility(session, paper, ctx, target_day)
    if "special_bucket" in reasons:
        return Exclusion(paper_id=paper_id, reasons=["special_bucket"])
    history = ScoreSnapshotRepository(session).history(paper_id)
    snapshot = next(
        (
            s
            for s in reversed(history)
            if (s.score_date <= target_day if day is None else s.score_date == day)
            and "scores" in s.breakdown_json
        ),
        None,
    )
    if snapshot is None:
        raise NotFoundError("没有该日优先级快照；请先执行 scores snapshot")
    return ScoreExplanation.model_validate(snapshot.breakdown_json)


def read_backlog(
    session: Session,
    *,
    now: datetime,
    limit: int = 50,
    offset: int = 0,
    filters: BacklogFilters | None = None,
) -> BacklogResponse:
    """长期阅读队列：最新可用快照，不受当天日报、容量或历史冷却限制。"""
    if not 1 <= limit <= 200 or offset < 0:
        raise InvariantViolation("pagination", "limit须为1–200，offset非负")
    filters = filters or BacklogFilters()
    ctx = _context(session, now)
    today = now.astimezone(ZoneInfo(ctx["runtime"].value_json["timezone"])).date()
    allowed = (
        BACKLOG_STATUSES
        if filters.read_status == "unread"
        else READ_STATUSES
        if filters.read_status == "read"
        else BACKLOG_STATUSES | READ_STATUSES
    )
    candidates: list[tuple[QueueItem, PaperStatus]] = []
    contexts = {today: ctx}
    for paper in PaperRepository(session).list():
        if paper.status not in allowed:
            continue
        for snapshot in reversed(ScoreSnapshotRepository(session).history(paper.id)):
            if (
                snapshot.score_date > today
                or snapshot.priority is None
                or "scores" not in snapshot.breakdown_json
            ):
                continue
            if snapshot.score_date not in contexts:
                contexts[snapshot.score_date] = _context(session, now, day=snapshot.score_date)
            reasons, tag, static = _eligibility(
                session, paper, contexts[snapshot.score_date], snapshot.score_date, eligible=allowed
            )
            if reasons or static is None:
                continue
            if (
                snapshot.breakdown_json.get("static_input_hash") != static.input_hash
                or snapshot.breakdown_json.get("primary_tag") != tag
            ):
                continue
            item = _item(session, paper, snapshot)
            if (
                filters.tag is None or item.primary_tag == filters.tag
            ) and filters.min_priority <= item.priority <= filters.max_priority:
                candidates.append((item, paper.status))
            break
    candidates.sort(
        key=lambda pair: rank_key(
            rank_score=pair[0].rank_score,
            relevance=pair[0].explanation.components["R"].score,
            published_on=pair[0].published_on,
            authority=pair[0].explanation.components["A"].score,
            title=pair[0].title,
            paper_id=pair[0].paper_id,
        )
    )
    items = [
        BacklogItem(**item.model_dump(), rank=index, status=status)
        for index, (item, status) in enumerate(candidates, 1)
    ]
    return BacklogResponse(
        items=items[offset : offset + limit], total=len(items), limit=limit, offset=offset
    )


def refresh_queue(session: Session, *, now: datetime) -> RefreshResponse:
    """本地刷新排序并将新合格论文入队；日内幂等，不创建日报。"""
    with _atomic(session):
        ctx = _context(session, now)
        day = now.astimezone(ZoneInfo(ctx["runtime"].value_json["timezone"])).date()
        before = {
            paper.id
            for paper in PaperRepository(session).list()
            if paper.status in BACKLOG_STATUSES
        }
        report = create_snapshots(session, day, now=now, eligible=ELIGIBLE | BACKLOG_STATUSES)
        after: set[str] = set()
        offset = 0
        while True:
            page = read_backlog(session, now=now, limit=200, offset=offset)
            after.update(item.paper_id for item in page.items)
            offset += len(page.items)
            if offset >= page.total:
                break
        return RefreshResponse(
            **report.model_dump(), queue_total=len(after), added_ids=sorted(after - before)
        )
