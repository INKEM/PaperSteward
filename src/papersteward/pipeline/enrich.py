"""P4 富化输入契约：中性信号、provider 协议与确定性降级。

P4-S1 不访问网络、不写数据库；后续阶段负责持久化与真实富化。缺失字段必须保持
中性值，不能因为“没查到”而惩罚文献。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from papersteward.core.entities import Paper
from papersteward.core.enums import OaStatus


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _text_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        raise ValueError(f"{field} 必须是字符串序列")
    items = tuple(_text(item, field) for item in value)
    if len(set(items)) != len(items):
        raise ValueError(f"{field} 不能包含重复项")
    return items


def _optional_url(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = _text(value, field)
    parsed = urlparse(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"{field} 必须是 http(s) URL")
    return text


@dataclass(frozen=True, slots=True)
class EnrichmentSignals:
    """富化结果；未知值使用中性值，``confidence`` 不做过度声明。"""

    teams: tuple[str, ...] = ()
    venue_level: str | None = None
    citation_count: int | None = None
    citation_velocity: float | None = None
    code_url: str | None = None
    data_url: str | None = None
    oa_status: OaStatus = OaStatus.UNKNOWN
    experiment_quality: tuple[str, ...] = ()
    confidence: float = 0.0
    source: str = "neutral"

    def __post_init__(self) -> None:
        object.__setattr__(self, "teams", _text_tuple(self.teams, "enrichment.teams"))
        object.__setattr__(
            self,
            "experiment_quality",
            _text_tuple(self.experiment_quality, "enrichment.experiment_quality"),
        )
        object.__setattr__(
            self,
            "venue_level",
            _text(self.venue_level, "enrichment.venue_level")
            if self.venue_level is not None
            else None,
        )
        if self.citation_count is not None and (
            type(self.citation_count) is not int or self.citation_count < 0
        ):
            raise ValueError("enrichment.citation_count 必须是非负整数或 None")
        if self.citation_velocity is not None:
            velocity = self.citation_velocity
            if (
                isinstance(velocity, bool)
                or not isinstance(velocity, int | float)
                or not math.isfinite(velocity)
                or velocity < 0
            ):
                raise ValueError("enrichment.citation_velocity 必须是非负有限数或 None")
            object.__setattr__(self, "citation_velocity", float(velocity))
        object.__setattr__(self, "code_url", _optional_url(self.code_url, "enrichment.code_url"))
        object.__setattr__(self, "data_url", _optional_url(self.data_url, "enrichment.data_url"))
        if not isinstance(self.oa_status, OaStatus):
            raise ValueError("enrichment.oa_status 必须是 OaStatus")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(self.confidence)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("enrichment.confidence 必须在 0–1 之间")
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "source", _text(self.source, "enrichment.source"))


@runtime_checkable
class EnrichmentProvider(Protocol):
    """富化 provider：只接收已筛选文献，返回带置信度/来源的中性信号。"""

    @property
    def source(self) -> str: ...

    def enrich(self, paper: Paper) -> EnrichmentSignals:
        """返回信号；无外部证据时返回中性值，失败应抛出可审计错误。"""
        ...


class NeutralEnricher:
    """确定性降级：只复用已在 Paper 中的 OA 状态，不推断任何新信号。"""

    source = "neutral"

    def enrich(self, paper: Paper) -> EnrichmentSignals:
        if not isinstance(paper, Paper):
            raise TypeError("paper 必须是 Paper")
        return EnrichmentSignals(oa_status=paper.oa_status, source=self.source)


__all__ = ["EnrichmentProvider", "EnrichmentSignals", "NeutralEnricher"]
