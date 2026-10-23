"""只在P5合格候选内编排；不写库，不创造语义证据。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from papersteward.core.enums import ReadMode
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import Digest, DigestCard
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.scoring.contracts import QueueItem
from papersteward.scoring.priority import rank_key


def item_key(item: QueueItem) -> tuple[float, float, int, float, str, str]:
    return rank_key(
        rank_score=item.rank_score,
        relevance=item.explanation.components["R"].score,
        published_on=item.published_on,
        authority=item.explanation.components["A"].score,
        title=item.title,
        paper_id=item.paper_id,
    )


@dataclass(frozen=True)
class DeliveryHistory:
    pushed_on: date | None = None
    priority: float | None = None
    static_hash: str | None = None
    skip_until: date | None = None
    snooze_until: date | None = None


def _deep(item: QueueItem) -> bool:
    r = item.explanation.components["R"].score
    e = item.explanation.components["E"].score
    # 仅使用已保留的可信rubric；历史记录没有证据时默认粗读。
    direct = False
    for evidence in item.explanation.evidence:
        if evidence.get("stage") == "relevance":
            details = evidence.get("details") or {}
            rubric = (details.get("rubric") or {}).get("effective_scores") or {}
            direct = (
                not details.get("needs_review", True)
                and rubric.get("problem", 0) >= 4
                and rubric.get("method", 0) >= 4
            )
    return (r >= 80 and (e >= 70 or direct)) or (65 <= r < 80 and direct)


def compose_digest(
    candidates: Sequence[QueueItem],
    *,
    day: date,
    timezone: str,
    binding: dict[str, Any],
    config: ScoringConfig,
    weekly_capacity: int,
    pending_review: int = 0,
    history: Mapping[str, DeliveryHistory] | None = None,
    metadata: Mapping[str, dict[str, Any]] | None = None,
    allow_downgrade: bool = False,
    queued_on: Mapping[str, date] | None = None,
) -> Digest:
    validate_scoring_config(config)
    if len({i.paper_id for i in candidates}) != len(candidates):
        raise InvariantViolation("digest.items", "候选不能重复")
    if any(i.score_date != day or i.excluded_from_priority for i in candidates):
        raise InvariantViolation("digest.items", "须为当日合格主题候选")
    gates = config.gates
    total_cap = min(7, gates.max_daily_push)
    deep_cap, skim_cap = min(2, gates.deep_cap), min(5, gates.skim_cap)
    per_tag = min(2, gates.max_per_primary_tag)
    deferred: dict[str, str] = {}
    eligible = []
    for item in sorted(candidates, key=item_key):
        old = (history or {}).get(item.paper_id, DeliveryHistory())
        if old.snooze_until is not None:
            if day < old.snooze_until:
                deferred[item.paper_id] = "snoozed"
                continue
            # 主动延期到期不受一般已发送去重限制。
        elif old.skip_until and day < old.skip_until:
            deferred[item.paper_id] = "skip_cooldown"
            continue
        elif old.skip_until is None and old.pushed_on is not None:
            changed = old.static_hash != item.explanation.static_input_hash or (
                old.priority is not None and abs(item.priority - old.priority) > 10
            )
            if not changed or day <= old.pushed_on:
                deferred[item.paper_id] = "already_pushed"
                continue
        eligible.append(item)

    # 先预留H最高的饥饿标签和另一个标签，再按原队列补满；输出仍按原排序。
    hungry = sorted(
        (i for i in eligible if i.explanation.components["H"].score > 50),
        key=lambda i: (-i.explanation.components["H"].score, item_key(i)),
    )
    inventory = len(candidates) > 2 * weekly_capacity
    order = (
        sorted(
            eligible,
            key=lambda i: ((queued_on or {}).get(i.paper_id, i.published_on) == day, item_key(i)),
        )
        if inventory
        else list(eligible)
    )
    if hungry:
        order.remove(hungry[0])
        order.insert(0, hungry[0])
    if order:
        other = next((i for i in order if i.primary_tag != order[0].primary_tag), None)
        if other is not None:
            order.remove(other)
            order.insert(1, other)
    counts: Counter[str] = Counter()
    modes: Counter[ReadMode] = Counter()
    chosen: list[DigestCard] = []
    for item in order:
        if len(chosen) >= total_cap or counts[item.primary_tag] >= per_tag:
            deferred[item.paper_id] = "daily_or_tag_cap"
            continue
        deep = _deep(item)
        mode = ReadMode.DEEP if deep else ReadMode.SKIM
        reason = (
            f"{item.primary_tag}：R={item.explanation.components['R'].score:g}，"
            f"E={item.explanation.components['E'].score:g}；"
            + ("高相关且额外价值或直接参考证据充分，建议精读" if deep else "建议先粗读核对可用性")
        )
        if mode is ReadMode.DEEP and modes[mode] >= deep_cap:
            if not allow_downgrade:
                deferred[item.paper_id] = "deep_cap_deferred"
                continue
            mode = ReadMode.SKIM
            reason += "；精读额度已满，按显式请求降为粗读"
        if modes[mode] >= (deep_cap if mode is ReadMode.DEEP else skim_cap):
            deferred[item.paper_id] = "read_mode_cap"
            continue
        chosen.append(
            DigestCard(
                **item.model_dump(),
                **(metadata or {}).get(item.paper_id, {}),
                read_mode=mode,
                reason=reason,
            )
        )
        modes[mode] += 1
        counts[item.primary_tag] += 1
    return Digest(
        digest_date=day,
        timezone=timezone,
        binding=binding,
        items=sorted(chosen, key=item_key),
        queue_total=len(candidates),
        pending_review=0,
        deferred=deferred,
        inventory_mode=inventory,
        special_slots=min(2, gates.max_special_push),
    )
