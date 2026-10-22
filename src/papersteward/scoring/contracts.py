"""P5冻结的队列/解释传输契约；持久化位于score_snapshot.breakdown_json。"""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
