"""P7-S5 全局权重治理：影响预览、确认、版本激活与静态分重算。

预览先落一条 ``previewed`` 变更记录（不改评分配置/快照），确认时重新计算并核对
preview hash，再原子写入新配置版本、下一生效日的静态快照和 ``config.activated``
事件。只有 confirmed 且 effective_on≤当日的变更会改变后续快照使用的配置版本。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session

from papersteward.core.entities import (
    Event,
    ScoringChange,
    StaticScoreSnapshot,
    utc_now,
)
from papersteward.core.enums import (
    VIEW_COMPONENT_ORDER,
    AssignedBy,
    EventType,
    PaperStatus,
    ScoringChangeStatus,
)
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.scoring.config import (
    ConfigError,
    ScoringConfig,
    config_value_json,
    validate_scoring_config,
)
from papersteward.scoring.priority import compute_priority, rank_key
from papersteward.scoring.queue import read_queue
from papersteward.scoring.service import read_scoring_config, save_scoring_config
from papersteward.storage.repositories import (
    ConfigRepository,
    EventRepository,
    PaperRepository,
    ScoringChangeRepository,
    StaticScoreSnapshotRepository,
)

COMPONENTS = VIEW_COMPONENT_ORDER
WEIGHT_PATHS = frozenset(
    {
        "static.weight",
        "static.relevance",
        "static.authority",
        "static.extra_value",
        "dynamic.weight",
        "dynamic.freshness",
        "dynamic.hunger",
        "quasi_dynamic.weight",
    }
)


@dataclass(frozen=True, slots=True)
class WeightImpact:
    base_version: str
    config_version: str
    preview_day: date
    effective_on: date
    old_weights: Mapping[str, float]
    new_weights: Mapping[str, float]
    items: tuple[dict[str, Any], ...]
    changed: int
    preview_hash: str


def component_weights(config: ScoringConfig) -> dict[str, float]:
    """把分层配置展开为 R/A/E/F/H/U 的最终组件权重。"""
    validate_scoring_config(config)
    weights = {
        "R": config.static.weight * config.static.relevance,
        "A": config.static.weight * config.static.authority,
        "E": config.static.weight * config.static.extra_value,
        "F": config.dynamic.weight * config.dynamic.freshness,
        "H": config.dynamic.weight * config.dynamic.hunger,
        "U": config.quasi_dynamic.weight,
    }
    if not math.isclose(math.fsum(weights.values()), 1.0, abs_tol=1e-6):
        raise ConfigError("展开后的组件权重之和必须为 1", source=f"config:{config.version}")
    return {key: float(weights[key]) for key in COMPONENTS}


def next_config_version(version: str) -> str:
    """语义版本最后一段加一；非数字后缀追加 ``.1``。"""
    text = version.strip()
    if not text:
        raise InvalidRequestError("当前配置版本为空，无法自动提升")
    parts = text.split(".")
    if parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        candidate = ".".join(parts)
    else:
        candidate = f"{text}.1"
    if len(candidate) > 64:
        raise InvalidRequestError("新版本号超过 64 个字符")
    return candidate


def apply_overrides(
    config: ScoringConfig,
    overrides: Mapping[str, Any],
    *,
    version: str | None = None,
) -> ScoringConfig:
    """按点路径覆盖字段并重新做完整校验；不写库。"""
    if not isinstance(overrides, Mapping) or not overrides:
        raise InvalidRequestError("至少需要一个配置覆盖项")
    data = config_value_json(config)
    for path, value in overrides.items():
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequestError("覆盖路径不能为空")
        parts = [part for part in path.strip().split(".") if part]
        if len(parts) < 2:
            raise InvalidRequestError(f"覆盖路径必须包含顶层字段：{path}")
        if path.strip() not in WEIGHT_PATHS:
            raise InvalidRequestError(
                "P7 权重治理只允许修改线性权重字段：" + ", ".join(sorted(WEIGHT_PATHS))
            )
        cursor: dict[str, Any] = data
        for part in parts[:-1]:
            nested = cursor.get(part)
            if not isinstance(nested, dict):
                raise InvalidRequestError(f"未知配置路径：{path}")
            cursor = nested
        if parts[-1] not in cursor:
            raise InvalidRequestError(f"未知配置字段：{path}")
        cursor[parts[-1]] = value
    next_version = version.strip() if version is not None else next_config_version(config.version)
    if not next_version:
        raise InvalidRequestError("新版本号不能为空")
    if next_version == config.version:
        raise InvalidRequestError("新版本号必须与当前版本不同")
    data["version"] = next_version
    try:
        proposed = ScoringConfig.model_validate(data)
    except ValidationError as exc:
        raise InvalidRequestError("配置字段不合法，请检查类型与取值范围") from exc
    validate_scoring_config(proposed, source=f"config-preview:{next_version}")
    return proposed


def preview_from_overrides(
    session: Session,
    overrides: Mapping[str, Any],
    *,
    version: str | None = None,
    preview_day: date,
    effective_on: date,
    now: datetime | None = None,
    limit: int = 20,
) -> ScoringChange:
    base, _ = _base_config(session, preview_day, now)
    return preview_weight_change(
        session,
        apply_overrides(base, overrides, version=version),
        preview_day=preview_day,
        effective_on=effective_on,
        now=now,
        limit=limit,
    )


def preview_weight_change(
    session: Session,
    proposed: ScoringConfig,
    *,
    preview_day: date,
    effective_on: date,
    now: datetime | None = None,
    limit: int = 20,
) -> ScoringChange:
    """计算影响并追加 previewed 记录；不保存配置、不改快照/激活。"""
    timestamp = now or utc_now()
    _validate_days(preview_day, effective_on)
    _validate_limit(limit)
    validate_scoring_config(proposed, source=f"config-preview:{proposed.version}")
    if len(proposed.version) > 64:
        raise InvalidRequestError("配置版本号不能超过 64 个字符")
    base, _ = _base_config(session, preview_day, timestamp)
    _assert_weight_only(proposed, base)
    if proposed.version == base.version:
        raise InvalidRequestError("新版本号必须与当前激活版本不同")
    if ConfigRepository(session).get("scoring", proposed.version) is not None:
        raise ConflictError("配置版本已存在；请提升版本号")
    conflicts = _static_conflicts(session, effective_on)
    if conflicts:
        raise ConflictError(
            "生效日必须晚于已有静态快照日期",
            details={"papers": conflicts[:20]},
        )
    impact = _build_impact(session, proposed, base, preview_day, effective_on, timestamp)
    preview_json = {
        "day": preview_day.isoformat(),
        "effective_on": effective_on.isoformat(),
        "base_version": base.version,
        "config_version": proposed.version,
        "old_component_weights": dict(impact.old_weights),
        "new_component_weights": dict(impact.new_weights),
        "changed": impact.changed,
        "total": len(impact.items),
        "items": list(impact.items[:limit]),
    }
    change = ScoringChange(
        id=uuid4().hex,
        status=ScoringChangeStatus.PREVIEWED,
        base_version=base.version,
        config_version=proposed.version,
        preview_day=preview_day,
        effective_on=effective_on,
        proposed_json=config_value_json(proposed),
        preview_json=preview_json,
        preview_hash=impact.preview_hash,
        created_at=timestamp,
    )
    return ScoringChangeRepository(session).add(change)


def preview_from_config(
    session: Session,
    proposed: ScoringConfig,
    *,
    preview_day: date,
    effective_on: date,
    now: datetime | None = None,
    limit: int = 20,
) -> ScoringChange:
    return preview_weight_change(
        session,
        proposed,
        preview_day=preview_day,
        effective_on=effective_on,
        now=now,
        limit=limit,
    )


def confirm_weight_change(
    session: Session,
    change_id: str,
    *,
    confirmed_by: str,
    note: str | None = None,
    now: datetime | None = None,
) -> tuple[ScoringChange, int]:
    """确认预览：写配置版本、重算生效日静态快照、记录激活事件。"""
    if not confirmed_by.strip():
        raise InvalidRequestError("必须记录确认人")
    timestamp = now or utc_now()
    repository = ScoringChangeRepository(session)
    change = repository.get(change_id)
    if change is None:
        raise NotFoundError("权重变更不存在")
    if change.status is not ScoringChangeStatus.PREVIEWED:
        raise ConflictError("权重变更已确认")
    base, binding = _base_config(session, change.preview_day, timestamp)
    if change.effective_on < runtime_local_day(session, timestamp, binding):
        raise ConflictError("生效日已过；请重新预览并使用未来生效日")
    if base.version != change.base_version:
        raise ConflictError("预览基线已变化；请重新预览")
    try:
        proposed = ScoringConfig.model_validate(change.proposed_json)
    except ValidationError as exc:
        raise ConflictError("预览中的配置已不可解析；请重新预览") from exc
    validate_scoring_config(proposed, source=f"config-confirm:{change.config_version}")
    _assert_weight_only(proposed, base)
    if proposed.version != change.config_version:
        raise ConflictError("预览配置版本不一致")
    if ConfigRepository(session).get("scoring", proposed.version) is not None:
        raise ConflictError("配置版本已存在；请重新预览")
    conflicts = _static_conflicts(session, change.effective_on)
    if conflicts:
        raise ConflictError(
            "生效日必须晚于已有静态快照日期",
            details={"papers": conflicts[:20]},
        )
    impact = _build_impact(
        session, proposed, base, change.preview_day, change.effective_on, timestamp
    )
    if impact.preview_hash != change.preview_hash:
        raise ConflictError("预览已过期（队列或配置已变化）；请重新预览")
    save_scoring_config(
        session,
        proposed,
        updated_by=AssignedBy.USER,
        updated_at=timestamp,
    )
    recomputed = _recompute_static_snapshots(session, proposed, change.effective_on, timestamp)
    confirmed = repository.confirm(
        change.id,
        confirmed_by=confirmed_by.strip(),
        note=None if note is None else note.strip() or None,
        now=timestamp,
    )
    EventRepository(session).append(
        Event(
            id=f"evt-config-activated-{change.id}",
            event_type=EventType.CONFIG_ACTIVATED,
            created_at=timestamp,
            payload_json={
                "change_id": change.id,
                "base_version": change.base_version,
                "config_version": proposed.version,
                "effective_on": change.effective_on.isoformat(),
                "confirmed_by": confirmed_by.strip(),
                "static_recomputed": recomputed,
            },
        )
    )
    return confirmed, recomputed


def list_changes(session: Session, *, limit: int = 50, offset: int = 0) -> list[ScoringChange]:
    return ScoringChangeRepository(session).list(limit=limit, offset=offset)


def count_changes(session: Session) -> int:
    return ScoringChangeRepository(session).count()


def get_change(session: Session, change_id: str) -> ScoringChange:
    change = ScoringChangeRepository(session).get(change_id)
    if change is None:
        raise NotFoundError("权重变更不存在")
    return change


def _assert_weight_only(proposed: ScoringConfig, base: ScoringConfig) -> None:
    """P7 只治理线性权重；门槛、EMA、half-life、U 子权重等不得借预览激活。"""
    proposed_data = config_value_json(proposed)
    base_data = config_value_json(base)
    for data in (proposed_data, base_data):
        data.pop("version", None)
        for path in WEIGHT_PATHS:
            head, _, leaf = path.partition(".")
            nested = data.get(head)
            if isinstance(nested, dict):
                nested.pop(leaf, None)
    if proposed_data != base_data:
        raise InvalidRequestError(
            "只允许修改线性权重字段；gates/stability/dynamic 内部参数/"
            "quasi_dynamic 子权重必须保持不变"
        )


def _base_config(
    session: Session, day: date, now: datetime | None
) -> tuple[ScoringConfig, dict[str, Any]]:
    timestamp = now or utc_now()
    _, binding = bound_context(
        session,
        timestamp,
        action="config.preview",
        enforce_schedule=False,
        day=day,
    )
    base = read_scoring_config(session, version=binding["scoring_version"])
    if base is None:
        raise NotFoundError("当前激活的评分配置不存在")
    return base, binding


def _build_impact(
    session: Session,
    proposed: ScoringConfig,
    base: ScoringConfig,
    preview_day: date,
    effective_on: date,
    now: datetime,
) -> WeightImpact:
    old_weights = component_weights(base)
    new_weights = component_weights(proposed)
    queue_items = _all_queue_items(session, preview_day, now)
    old_rank = {item.paper_id: index + 1 for index, item in enumerate(queue_items)}
    rows: list[tuple[Any, float, float, dict[str, Any]]] = []
    for item in queue_items:
        scores = {key: item.explanation.components[key].score for key in COMPONENTS}
        static_new = round(
            scores["R"] * proposed.static.relevance
            + scores["A"] * proposed.static.authority
            + scores["E"] * proposed.static.extra_value,
            1,
        )
        dynamic_new = round(
            scores["F"] * proposed.dynamic.freshness + scores["H"] * proposed.dynamic.hunger,
            1,
        )
        priority = compute_priority(
            static_new,
            dynamic_new,
            scores["U"],
            proposed,
            previous_rank=item.rank_score,
            reset=True,
        )
        rows.append((item, priority.priority, priority.rank_score, scores))
    rows.sort(
        key=lambda row: rank_key(
            rank_score=row[2],
            relevance=row[3]["R"],
            published_on=row[0].published_on,
            authority=row[3]["A"],
            title=row[0].title,
            paper_id=row[0].paper_id,
        )
    )
    items: list[dict[str, Any]] = []
    ranking: list[list[Any]] = []
    changed = 0
    for new_rank, (item, new_priority, new_rank_score, scores) in enumerate(rows, start=1):
        old = old_rank[item.paper_id]
        if old != new_rank:
            changed += 1
        ranking.append(
            [
                item.paper_id,
                old,
                new_rank,
                item.priority,
                new_priority,
                item.rank_score,
                new_rank_score,
            ]
        )
        items.append(
            {
                "paper_id": item.paper_id,
                "title": item.title,
                "primary_tag": item.primary_tag,
                "old_rank": old,
                "new_rank": new_rank,
                "delta_rank": old - new_rank,
                "old_priority": item.priority,
                "new_priority": new_priority,
                "old_rank_score": item.rank_score,
                "new_rank_score": new_rank_score,
                "components": {
                    key: {
                        "score": scores[key],
                        "old_weight": old_weights[key],
                        "new_weight": new_weights[key],
                        "old_contribution": round(scores[key] * old_weights[key], 4),
                        "new_contribution": round(scores[key] * new_weights[key], 4),
                    }
                    for key in COMPONENTS
                },
            }
        )
    payload = {
        "preview_day": preview_day.isoformat(),
        "effective_on": effective_on.isoformat(),
        "base_version": base.version,
        "config_version": proposed.version,
        "proposed": config_value_json(proposed),
        "ranking": ranking,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return WeightImpact(
        base_version=base.version,
        config_version=proposed.version,
        preview_day=preview_day,
        effective_on=effective_on,
        old_weights=old_weights,
        new_weights=new_weights,
        items=tuple(items),
        changed=changed,
        preview_hash=digest,
    )


def _all_queue_items(session: Session, day: date, now: datetime) -> list[Any]:
    first = read_queue(session, day, now=now, limit=200, offset=0)
    items = list(first.items)
    while len(items) < first.total:
        page = read_queue(session, day, now=now, limit=200, offset=len(items))
        if not page.items:
            break
        items.extend(page.items)
    return items


def _static_conflicts(session: Session, effective_on: date) -> list[str]:
    conflicts: list[str] = []
    for paper in PaperRepository(session).list():
        if paper.status is PaperStatus.ARCHIVED:
            continue
        latest = StaticScoreSnapshotRepository(session).latest(paper.id)
        if latest is not None and latest.score_date >= effective_on:
            conflicts.append(paper.id)
    return conflicts


def _recompute_static_snapshots(
    session: Session, proposed: ScoringConfig, effective_on: date, now: datetime
) -> int:
    repository = StaticScoreSnapshotRepository(session)
    count = 0
    for paper in PaperRepository(session).list():
        if paper.status is PaperStatus.ARCHIVED:
            continue
        latest = repository.latest(paper.id)
        if latest is None:
            continue
        static_score = round(
            latest.relevance * proposed.static.relevance
            + latest.authority * proposed.static.authority
            + latest.extra_value * proposed.static.extra_value,
            1,
        )
        input_hash = hashlib.sha256(
            f"{latest.input_hash}:{proposed.version}:{effective_on.isoformat()}".encode()
        ).hexdigest()
        breakdown = dict(latest.breakdown_json)
        breakdown["recomputed"] = {
            "trigger": "config_changed",
            "from_input_hash": latest.input_hash,
            "config_version": proposed.version,
        }
        repository.append(
            StaticScoreSnapshot(
                paper_id=paper.id,
                score_date=effective_on,
                relevance=latest.relevance,
                authority=latest.authority,
                extra_value=latest.extra_value,
                static_score=static_score,
                confidence=latest.confidence,
                config_version=proposed.version,
                profile_version=latest.profile_version,
                taxonomy_version=latest.taxonomy_version,
                input_hash=input_hash,
                breakdown_json=breakdown,
                created_at=now,
            )
        )
        count += 1
    return count


def _validate_days(preview_day: date, effective_on: date) -> None:
    for label, value in (("preview_day", preview_day), ("effective_on", effective_on)):
        if isinstance(value, datetime) or not isinstance(value, date):
            raise InvalidRequestError(f"{label} 必须是 date")
    if effective_on <= preview_day:
        raise InvalidRequestError("effective_on 必须晚于预览日")


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise InvalidRequestError("limit 必须在 1–200 之间")


__all__ = [
    "COMPONENTS",
    "WeightImpact",
    "WEIGHT_PATHS",
    "apply_overrides",
    "component_weights",
    "confirm_weight_change",
    "count_changes",
    "get_change",
    "list_changes",
    "next_config_version",
    "preview_from_config",
    "preview_from_overrides",
    "preview_weight_change",
]
