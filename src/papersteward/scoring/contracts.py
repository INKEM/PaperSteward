"""P5冻结的队列/解释传输契约；持久化位于score_snapshot.breakdown_json。"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from papersteward.core.enums import PaperStatus


class Component(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    score: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)


class ScoreExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    paper_id: str
    score_date: date
    config_version: str
    profile_version: str
    taxonomy_version: str
    static_input_hash: str
    primary_tag: str
    components: dict[str, Component]
    layer_weights: dict[str, float]
    scores: dict[str, float]
    dynamic: dict[str, Any]
    quasi: dict[str, Any]
    stability: dict[str, Any]
    evidence: list[dict[str, Any]]
    reasons: list[str]
    binding: dict[str, Any]
    formula: str = "P = wS*S + wD*D + wQ*U; S/D are frozen to one decimal"


class QueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_id: str
    title: str
    primary_tag: str
    summary_100: str
    published_on: date | None
    score_date: date
    priority: float = Field(ge=0, le=100)
    rank_score: float = Field(ge=0, le=100)
    explanation: ScoreExplanation
    excluded_from_priority: bool = False


class QueueResponse(BaseModel):
    items: list[QueueItem]
    total: int
    limit: int
    offset: int


class Exclusion(BaseModel):
    paper_id: str
    reasons: list[str]
    excluded_from_priority: bool = True


class SnapshotResponse(BaseModel):
    score_date: date
    created: int
    existing: int
    excluded: list[Exclusion]
    # 保底填充入队的篇数（仅豁免 below_relevance_gate，快照 reasons 带 quota_fill 标记）。
    quota_filled: int = 0


class BacklogFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    tag: str | None = None
    min_priority: float = Field(default=0, ge=0, le=100)
    max_priority: float = Field(default=100, ge=0, le=100)
    read_status: Literal["unread", "read", "all"] = "unread"

    @model_validator(mode="after")
    def coherent_range(self) -> "BacklogFilters":
        if self.min_priority > self.max_priority:
            raise ValueError("最低分不能高于最高分")
        return self


class BacklogItem(QueueItem):
    rank: int = Field(ge=1)
    status: PaperStatus


class BacklogResponse(BaseModel):
    items: list[BacklogItem]
    total: int
    limit: int
    offset: int


class RefreshResponse(SnapshotResponse):
    queue_total: int
    added_ids: list[str] = Field(default_factory=list)
