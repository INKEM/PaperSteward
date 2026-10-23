"""Agent 检索规划：综合研究档案、已读品味与队列覆盖生成多条互补检索式。

LLM 失败或输出不可用时退回 :func:`generate_queries` 的确定性展开；每次退回都记录
原因并进入运行报告。规划器只读库，不写任何表。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papersteward.core.entities import ResearchProfile, utc_now
from papersteward.core.enums import PaperStatus, TagRole
from papersteward.llm.errors import LLMError
from papersteward.llm.gateway import LLMGateway
from papersteward.pipeline.relevance import build_profile_block
from papersteward.schemas.models import QueryPlanningOutput
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.queries import generate_queries, selected_profile
from papersteward.storage.models import PaperRow, PaperTagRow, RatingRow

logger = logging.getLogger(__name__)

DEFAULT_QUERY_BUDGET = 6
FALLBACK_MAX_RESULTS = 30
HISTORY_WEEKS = 8
HISTORY_LIMIT = 40
QUEUE_STATUSES = frozenset({PaperStatus.QUEUED, PaperStatus.PUSHED, PaperStatus.READING})


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """一条可执行的检索式及其来源说明。"""

    spec: QuerySpec
    rationale: str


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """一轮挖掘的检索计划；origin 说明计划来自模型还是确定性兜底。"""

    queries: tuple[PlannedQuery, ...]
    strategy_note: str
    origin: Literal["agent", "profile"]
    fallback_reason: str | None = None
    round_index: int = 1

    def as_json(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "strategy_note": self.strategy_note,
            "fallback_reason": self.fallback_reason,
            "round": self.round_index,
            "queries": [
                {
                    "keywords": list(query.spec.keywords),
                    "operator": query.spec.operator,
                    "exclude_keywords": list(query.spec.exclude_keywords),
                    "field": query.spec.field,
                    "date_from": (
                        query.spec.date_from.isoformat() if query.spec.date_from else None
                    ),
                    "date_to": query.spec.date_to.isoformat() if query.spec.date_to else None,
                    "max_results": query.spec.max_results,
                    "rationale": query.rationale,
                }
                for query in self.queries
            ],
        }

    def keyword_digest(self) -> str:
        """已用关键词摘要，供下一轮规划避免重复。"""
        return "、".join(keyword for query in self.queries for keyword in query.spec.keywords)


def _profile_exclude_terms(profile: ResearchProfile) -> tuple[str, ...]:
    terms: list[str] = []
    terms.extend(
        value.strip()
        for value in profile.parsed_json.get("exclude") or ()
        if isinstance(value, str) and value.strip()
    )
    return tuple(dict.fromkeys(terms))


def _history_block(session: Session, *, today: date, limit: int = HISTORY_LIMIT) -> str:
    """渲染近期已读评分；没有任何评分历史时给出明确空态。"""
    cutoff = datetime.combine(
        today - timedelta(weeks=HISTORY_WEEKS), datetime.min.time(), tzinfo=UTC
    )
    rows = session.execute(
        select(RatingRow.score, RatingRow.tags_at_read_json, PaperRow.title, PaperRow.venue)
        .join(PaperRow, PaperRow.id == RatingRow.paper_id)
        .where(RatingRow.created_at >= cutoff)
        .order_by(RatingRow.created_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return "（近 8 周没有已读评分记录）"
    lines: list[str] = []
    for score, tags_at_read, title, venue in rows:
        primary_tag = next(iter(tags_at_read or ()), None)
        parts = [f"《{title}》"]
        if primary_tag:
            parts.append(f"主标签 {primary_tag}")
        parts.append(f"评分 {score if score is not None else '未评'}/5")
        if venue:
            parts.append(str(venue))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _coverage_block(session: Session) -> str:
    """渲染当前队列各主标签的篇数，供模型优先补足欠覆盖方向。"""
    counts: dict[str, int] = {}
    for tag_code, total in session.execute(
        select(PaperTagRow.tag_code, func.count())
        .select_from(PaperRow)
        .join(PaperTagRow, PaperTagRow.paper_id == PaperRow.id)
        .where(PaperRow.status.in_([status.value for status in QUEUE_STATUSES]))
        .where(PaperTagRow.role == TagRole.PRIMARY.value)
        .group_by(PaperTagRow.tag_code)
    ).all():
        counts[str(tag_code)] = int(total)
    if not counts:
        return "（队列为空）"
    return "\n".join(f"{tag_code}: {total} 篇" for tag_code, total in sorted(counts.items()))


def _round_note(round_index: int, *, used_keywords: str, round_feedback: str) -> str:
    if round_index <= 1:
        return "这是本轮挖掘的第 1 轮规划。"
    used = used_keywords or "（无）"
    extra = round_feedback or "请换角度、放宽范围或覆盖此前未用的子方向"
    return (
        f"这是第 {round_index} 轮规划：此前候选不足。已用检索式关键词：{used}；"
        f"{extra}，不要与已用检索式同义重复。"
    )


def _clamp_queries(
    output: Any, *, profile: ResearchProfile, today: date, max_queries: int
) -> tuple[PlannedQuery, ...]:
    """把模型输出钳成合法 QuerySpec：剔除空关键词/越界值/重复项，合并档案排除词。"""
    profile_excludes = _profile_exclude_terms(profile)
    default_from = today - timedelta(days=365)
    planned: list[PlannedQuery] = []
    seen: set[tuple[Any, ...]] = set()
    for item in output.queries:
        keywords = tuple(
            dict.fromkeys(
                keyword.strip()
                for keyword in item.keywords
                if isinstance(keyword, str) and keyword.strip()
            )
        )
        if not keywords:
            continue
        excludes = tuple(
            dict.fromkeys(
                [term.strip() for term in item.exclude_keywords if term.strip()]
                + list(profile_excludes)
            )
        )
        try:
            spec = QuerySpec.model_validate(
                {
                    "keywords": keywords,
                    "operator": item.operator,
                    "exclude_keywords": excludes,
                    "field": item.field,
                    "date_from": item.date_from or default_from,
                    "date_to": item.date_to or today,
                    "max_results": max(5, min(100, int(item.max_results))),
                }
            )
        except Exception:
            # 单条规划解析失败不拖垮整轮检索；留痕计数便于发现模型输出退化。
            logger.warning("planned query dropped (invalid item)", exc_info=True)
            continue
        key = (
            spec.field,
            spec.operator,
            tuple(k.casefold() for k in spec.keywords),
            tuple(e.casefold() for e in spec.exclude_keywords),
            spec.date_from,
            spec.date_to,
        )
        if key in seen:
            continue
        seen.add(key)
        planned.append(PlannedQuery(spec=spec, rationale=item.rationale.strip()))
        if len(planned) >= max_queries:
            break
    return tuple(planned)


def _fallback_plan(
    profile: ResearchProfile,
    *,
    reason: str,
    round_index: int,
    max_queries: int,
) -> QueryPlan:
    """确定性兜底：沿用档案展开的既有逻辑，不调用模型。"""
    specs = generate_queries(profile, max_queries=max_queries, max_results=FALLBACK_MAX_RESULTS)
    return QueryPlan(
        queries=tuple(
            PlannedQuery(spec=spec, rationale="研究档案确定性展开（模型规划不可用，已兜底）")
            for spec in specs
        ),
        strategy_note="模型检索规划不可用，使用档案确定性展开的检索式。",
        origin="profile",
        fallback_reason=reason,
        round_index=round_index,
    )


def plan_queries(
    session: Session,
    gateway: LLMGateway,
    *,
    focus_hint: str = "",
    target: int = 5,
    round_index: int = 1,
    used_keywords: str = "",
    round_feedback: str = "",
    max_queries: int = DEFAULT_QUERY_BUDGET,
    today: date | None = None,
) -> QueryPlan:
    """生成一轮挖掘的检索计划；模型失败时退回档案确定性展开。"""
    profile = selected_profile(session)
    current = today or utc_now().date()
    try:
        result = gateway.complete_json(
            "query_planning",
            {
                "profile": build_profile_block(profile),
                "history": _history_block(session, today=current),
                "coverage": _coverage_block(session),
                "focus": focus_hint.strip() or "（无，按档案与品味自行决定）",
                "round_note": _round_note(
                    round_index, used_keywords=used_keywords, round_feedback=round_feedback
                ),
                "today": current.isoformat(),
                "date_from": (current - timedelta(days=365)).isoformat(),
                "date_to": current.isoformat(),
            },
        )
    except LLMError as exc:
        return _fallback_plan(
            profile,
            reason=f"模型调用失败：{exc}",
            round_index=round_index,
            max_queries=max_queries,
        )
    if not isinstance(result.data, QueryPlanningOutput):
        return _fallback_plan(
            profile,
            reason="模型输出不是有效的检索计划",
            round_index=round_index,
            max_queries=max_queries,
        )
    planned = _clamp_queries(result.data, profile=profile, today=current, max_queries=max_queries)
    if not planned:
        return _fallback_plan(
            profile,
            reason="模型规划的所有检索式都无效",
            round_index=round_index,
            max_queries=max_queries,
        )
    return QueryPlan(
        queries=planned,
        strategy_note=result.data.strategy_note.strip(),
        origin="agent",
        round_index=round_index,
    )


def plan_profile_queries(
    session: Session,
    *,
    reason: str,
    round_index: int = 1,
    max_queries: int = DEFAULT_QUERY_BUDGET,
) -> QueryPlan:
    """模型未配置或调用方明确要求兜底时的入口；不触碰 LLM。"""
    profile = selected_profile(session)
    return _fallback_plan(profile, reason=reason, round_index=round_index, max_queries=max_queries)


__all__ = [
    "DEFAULT_QUERY_BUDGET",
    "FALLBACK_MAX_RESULTS",
    "PlannedQuery",
    "QueryPlan",
    "plan_profile_queries",
    "plan_queries",
]
