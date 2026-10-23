"""P7-S4 自定义排序视图：预设、权重归一化、Top-N 预览与保存。

视图只对当前合格候选集重排，不写全局配置、不生成新 P 快照、不改变候选集；
特殊桶仍由 queue 排除。视图分 `P_view` 是当前排序分数，不是持久化的 P。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.core.entities import ViewConfig, utc_now
from papersteward.core.enums import VIEW_COMPONENT_ORDER
from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.scoring.contracts import QueueItem
from papersteward.scoring.priority import rank_key
from papersteward.scoring.queue import read_backlog, read_queue
from papersteward.storage.repositories import ViewConfigRepository

COMPONENTS: Final = VIEW_COMPONENT_ORDER
PRESET_ORDER: Final = ("balanced", "frontier", "beginner", "reproducible")


@dataclass(frozen=True, slots=True)
class ViewPreset:
    """内置预设；``weights`` 是百分比，``normalized`` 是 0–1 权重。"""

    name: str
    label: str
    description: str
    weights: Mapping[str, float]
    normalized: Mapping[str, float]

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "weights": dict(self.weights),
            "normalized_weights": dict(self.normalized),
            "formula": formula(self.normalized),
        }


def _component_key(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError("组件名必须是字符串")
    key = value.strip().upper()
    if key not in COMPONENTS:
        raise InvalidRequestError(f"未知组件：{value}")
    return key


def _validate_normalized(weights: Mapping[str, float]) -> None:
    if set(weights) != set(COMPONENTS):
        raise InvalidRequestError("权重必须覆盖 R/A/E/F/H/U")
    for key in COMPONENTS:
        value = weights[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not 0.0 <= float(value) <= 1.0
        ):
            raise InvalidRequestError(f"权重 {key} 必须在 0–1 之间")
    if not math.isclose(math.fsum(float(weights[key]) for key in COMPONENTS), 1.0, abs_tol=1e-6):
        raise InvalidRequestError("权重之和必须为 1")


def normalize_view_weights(weights: Mapping[str, Any]) -> dict[str, float]:
    """把 R/A/E/F/H/U 的相对权重归一化为合计 1；0 表示不选。"""
    if not isinstance(weights, Mapping) or not weights:
        raise InvalidRequestError("weights 必须是非空对象")
    values: dict[str, float] = {}
    for raw_key, raw_value in weights.items():
        key = _component_key(raw_key)
        if key in values:
            raise InvalidRequestError(f"组件重复：{key}")
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, int | float)
            or not math.isfinite(raw_value)
            or not 0.0 <= float(raw_value) <= 100.0
        ):
            raise InvalidRequestError(f"权重 {key} 必须是 0–100 的有限数")
        values[key] = float(raw_value)
    total = math.fsum(values.values())
    if total <= 0:
        raise InvalidRequestError("至少需要一个正权重")
    return {key: values.get(key, 0.0) / total for key in COMPONENTS}


def formula(weights: Mapping[str, float]) -> str:
    """生成稳定的视图公式；只列出正权重。"""
    _validate_normalized(weights)
    parts = [f"{float(weights[key]):.2f}*{key}" for key in COMPONENTS if float(weights[key]) > 0]
    if not parts:
        raise InvalidRequestError("至少需要一个正权重")
    return "P_view = " + " + ".join(parts)


def view_score(scores: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """按视图权重计算一位小数分数；scores/weights 必须覆盖六个组件。"""
    if set(scores) != set(COMPONENTS) or set(weights) != set(COMPONENTS):
        raise InvalidRequestError("组件必须为 R/A/E/F/H/U")
    _validate_normalized(weights)
    values: dict[str, float] = {}
    for key in COMPONENTS:
        value = scores[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or not 0.0 <= float(value) <= 100.0
        ):
            raise InvalidRequestError(f"组件 {key} 必须是 0–100 的有限数")
        values[key] = float(value)
    return round(math.fsum(values[key] * float(weights[key]) for key in COMPONENTS), 1)


@dataclass(frozen=True, slots=True)
class ResolvedView:
    name: str
    label: str
    description: str
    weights: Mapping[str, float]
    preset: str | None
    is_preset: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "preset": self.preset,
            "is_preset": self.is_preset,
            "weights": dict(self.weights),
            "formula": formula(self.weights),
        }


@dataclass(frozen=True, slots=True)
class ViewComponent:
    score: float
    weight: float
    contribution: float

    def as_json(self) -> dict[str, float]:
        return {
            "score": self.score,
            "weight": self.weight,
            "contribution": self.contribution,
        }


@dataclass(frozen=True, slots=True)
class ViewRankedItem:
    paper_id: str
    title: str
    primary_tag: str
    summary_100: str
    published_on: date | None
    rank: int
    default_rank: int
    delta_rank: int
    view_score: float
    default_priority: float
    default_rank_score: float
    components: Mapping[str, ViewComponent]

    def as_json(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "primary_tag": self.primary_tag,
            "summary_100": self.summary_100,
            "published_on": (
                self.published_on.isoformat() if self.published_on is not None else None
            ),
            "rank": self.rank,
            "default_rank": self.default_rank,
            "delta_rank": self.delta_rank,
            "view_score": self.view_score,
            "default_priority": self.default_priority,
            "default_rank_score": self.default_rank_score,
            "components": {key: value.as_json() for key, value in self.components.items()},
        }


@dataclass(frozen=True, slots=True)
class ViewPreview:
    score_date: date
    view: ResolvedView
    candidate_total: int
    changed: int
    items: tuple[ViewRankedItem, ...]
    total: int
    limit: int
    offset: int

    def as_json(self) -> dict[str, Any]:
        return {
            "date": self.score_date.isoformat(),
            "view": self.view.as_json(),
            "formula": formula(self.view.weights),
            "candidate_total": self.candidate_total,
            "changed": self.changed,
            "items": [item.as_json() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


def _preset(name: str, label: str, description: str, weights: Mapping[str, float]) -> ViewPreset:
    return ViewPreset(
        name=name,
        label=label,
        description=description,
        weights=dict(weights),
        normalized=normalize_view_weights(weights),
    )


PRESETS: Final[Mapping[str, ViewPreset]] = MappingProxyType(
    {
        "balanced": _preset(
            "balanced",
            "均衡（默认）",
            "日常阅读；接近全局默认展开权重。",
            {"R": 45, "A": 15, "E": 10, "F": 12, "H": 8, "U": 10},
        ),
        "frontier": _preset(
            "frontier",
            "前沿追踪",
            "快速跟新、会议前扫射。",
            {"R": 35, "A": 10, "E": 10, "F": 25, "H": 10, "U": 10},
        ),
        "beginner": _preset(
            "beginner",
            "快速入门",
            "新方向补基础、优先权威。",
            {"R": 45, "A": 25, "E": 10, "F": 10, "H": 5, "U": 5},
        ),
        "reproducible": _preset(
            "reproducible",
            "复现导向",
            "找可运行基线、代码与数据。",
            {"R": 35, "A": 10, "E": 40, "F": 10, "H": 5, "U": 0},
        ),
    }
)


def list_presets() -> tuple[ViewPreset, ...]:
    return tuple(PRESETS[name] for name in PRESET_ORDER)


def resolve_view(
    session: Session,
    *,
    preset: str | None = None,
    weights: Mapping[str, Any] | None = None,
    view_id: str | None = None,
) -> ResolvedView:
    chosen = sum(value is not None for value in (preset, weights, view_id))
    if chosen > 1:
        raise InvalidRequestError("preset/weights/view_id 只能选择一个")
    if preset is not None:
        item = PRESETS.get(preset.strip().casefold())
        if item is None:
            raise NotFoundError("视图预设不存在")
        return ResolvedView(
            name=item.name,
            label=item.label,
            description=item.description,
            weights=item.normalized,
            preset=item.name,
            is_preset=True,
        )
    if weights is not None:
        normalized = normalize_view_weights(weights)
        return ResolvedView(
            name="custom",
            label="自定义视图",
            description="",
            weights=normalized,
            preset=None,
            is_preset=False,
        )
    if view_id is not None:
        config = ViewConfigRepository(session).get(view_id)
        if config is None:
            raise NotFoundError("视图不存在")
        return ResolvedView(
            name=config.name,
            label=config.name,
            description=config.description or "",
            weights=dict(config.weights),
            preset=config.preset,
            is_preset=False,
        )
    balanced = PRESETS["balanced"]
    return ResolvedView(
        name=balanced.name,
        label=balanced.label,
        description=balanced.description,
        weights=balanced.normalized,
        preset=balanced.name,
        is_preset=True,
    )


def preview_view(
    session: Session,
    score_date: date,
    *,
    now: datetime | None = None,
    preset: str | None = None,
    weights: Mapping[str, Any] | None = None,
    view_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
    persistent: bool = False,
) -> ViewPreview:
    if isinstance(score_date, datetime) or not isinstance(score_date, date):
        raise InvalidRequestError("日期必须是 date")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise InvalidRequestError("limit 必须在 1–200 之间")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidRequestError("offset 不能为负")
    view = resolve_view(session, preset=preset, weights=weights, view_id=view_id)
    if persistent:
        queue_items: list[QueueItem] = []
        while True:
            page = read_backlog(session, now=now or utc_now(), limit=200, offset=len(queue_items))
            queue_items.extend(page.items)
            if len(queue_items) >= page.total:
                break
    else:
        queue_items = _all_queue_items(session, score_date, now or utc_now())
    default_rank = {item.paper_id: index + 1 for index, item in enumerate(queue_items)}
    scored: list[tuple[QueueItem, float, dict[str, ViewComponent]]] = []
    for item in queue_items:
        components = _components(item, view.weights)
        score = view_score({key: value.score for key, value in components.items()}, view.weights)
        scored.append((item, score, components))
    scored.sort(
        key=lambda row: rank_key(
            rank_score=row[1],
            relevance=row[2]["R"].score,
            published_on=row[0].published_on,
            authority=row[2]["A"].score,
            title=row[0].title,
            paper_id=row[0].paper_id,
        )
    )
    ranked: list[ViewRankedItem] = []
    for rank, (item, score, components) in enumerate(scored, start=1):
        default = default_rank[item.paper_id]
        ranked.append(
            ViewRankedItem(
                paper_id=item.paper_id,
                title=item.title,
                primary_tag=item.primary_tag,
                summary_100=item.summary_100,
                published_on=item.published_on,
                rank=rank,
                default_rank=default,
                delta_rank=default - rank,
                view_score=score,
                default_priority=item.priority,
                default_rank_score=item.rank_score,
                components=components,
            )
        )
    changed = sum(1 for item in ranked if item.rank != item.default_rank)
    return ViewPreview(
        score_date=score_date,
        view=view,
        candidate_total=len(queue_items),
        changed=changed,
        items=tuple(ranked[offset : offset + limit]),
        total=len(ranked),
        limit=limit,
        offset=offset,
    )


def save_view(
    session: Session,
    name: str,
    *,
    preset: str | None = None,
    weights: Mapping[str, Any] | None = None,
    description: str | None = None,
    now: datetime | None = None,
) -> ViewConfig:
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 128:
        raise InvalidRequestError("视图名称必须为 1–128 个字符")
    if clean_name.casefold() in PRESETS:
        raise ConflictError("视图名称不能与内置预设同名")
    if (preset is None) == (weights is None):
        raise InvalidRequestError("preset 与 weights 必须二选一")
    resolved = resolve_view(session, preset=preset, weights=weights)
    repo = ViewConfigRepository(session)
    if repo.find_by_name(clean_name) is not None:
        raise ConflictError("视图名称已存在")
    description_text = None if description is None else description.strip()
    if description_text == "":
        description_text = None
    if description_text is not None and len(description_text) > 2000:
        raise InvalidRequestError("描述不能超过 2000 个字符")
    view = ViewConfig(
        id=uuid4().hex,
        name=clean_name,
        weights=dict(resolved.weights),
        preset=resolved.preset,
        description=description_text,
        created_at=now or utc_now(),
    )
    try:
        return repo.add(view)
    except InvariantViolation as exc:
        raise ConflictError(exc.message) from exc


def get_view_config(session: Session, target: str) -> ViewConfig:
    repo = ViewConfigRepository(session)
    found = repo.get(target) or repo.find_by_name(target)
    if found is None:
        raise NotFoundError("视图不存在")
    return found


def list_view_configs(session: Session) -> list[ViewConfig]:
    return ViewConfigRepository(session).list()


def delete_view_config(session: Session, target: str) -> ViewConfig:
    found = get_view_config(session, target)
    if not ViewConfigRepository(session).delete(found.id):
        raise NotFoundError("视图不存在")
    return found


def _all_queue_items(session: Session, day: date, now: datetime) -> list[QueueItem]:
    first = read_queue(session, day, now=now, limit=200, offset=0)
    items = list(first.items)
    while len(items) < first.total:
        page = read_queue(session, day, now=now, limit=200, offset=len(items))
        if not page.items:
            break
        items.extend(page.items)
    return items


def _components(item: QueueItem, weights: Mapping[str, float]) -> dict[str, ViewComponent]:
    result: dict[str, ViewComponent] = {}
    for key in COMPONENTS:
        component = item.explanation.components[key]
        result[key] = ViewComponent(
            score=component.score,
            weight=float(weights[key]),
            contribution=round(component.score * float(weights[key]), 4),
        )
    return result


__all__ = [
    "COMPONENTS",
    "PRESET_ORDER",
    "PRESETS",
    "ResolvedView",
    "ViewComponent",
    "ViewPreset",
    "ViewPreview",
    "ViewRankedItem",
    "delete_view_config",
    "formula",
    "get_view_config",
    "list_presets",
    "list_view_configs",
    "normalize_view_weights",
    "preview_view",
    "resolve_view",
    "save_view",
    "view_score",
]
