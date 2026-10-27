"""阅读研究记忆契约；独立于研究档案与推荐契约。"""

from typing import Literal

from pydantic import AwareDatetime, Field

from papersteward.reading.insights.contracts import (
    InsightsModel,
    MaterialExclusion,
    MaterialKind,
    MaterialRef,
)

MEMORY_PURPOSE = "reading_profile_inference"
MemoryStatus = Literal[
    "pending", "confirmed", "deferred", "not_preference", "disabled", "needs_review"
]
MemoryScope = Literal["general", "task_only"]
MemoryKind = Literal["interest", "value_standard"]
STATUS_LABELS = {
    "pending": "待确认",
    "confirmed": "已确认",
    "deferred": "暂不采用",
    "not_preference": "这不代表偏好",
    "disabled": "已停用",
    "needs_review": "需复核",
}


class MemoryManifestInput(InsightsModel):
    kinds: list[MaterialKind] = Field(min_length=1, max_length=8)
    exclusions: list[MaterialExclusion] = Field(default_factory=list, max_length=500)


class MemoryEvidence(InsightsModel):
    ref: MaterialRef
    role: Literal["supporting", "contrary", "context"]
    reading_precision: str
    reading_basis: str
    reading_start: str | None = None
    reading_end: str | None = None
    reading_recorded_at: AwareDatetime | None = None
    feedback_at: AwareDatetime | None = None
    archived_at: AwareDatetime


class MemoryGroup(InsightsModel):
    key: str
    label: str
    kind: MemoryKind
    evidence: list[MemoryEvidence]
    distinct_papers: int
    feedback_months: list[str]
    first_feedback_at: AwareDatetime | None = None
    last_feedback_at: AwareDatetime | None = None
    stable_eligible: bool
    single_task: bool
    concentrated_batch: bool
    context: str
    windows: dict[str, int]
    limitations: list[str]
    fingerprint: str


class MemoryDecisionInput(InsightsModel):
    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    action: Literal[
        "confirm", "revise", "task_only", "not_preference", "defer", "disable", "review"
    ]
    statement: str | None = Field(default=None, min_length=1, max_length=2000)
    explanation: str | None = Field(default=None, max_length=1000)


class MemoryRevisionView(InsightsModel):
    memory_id: str
    revision: int
    kind: MemoryKind
    status: MemoryStatus
    scope: MemoryScope
    statement: str
    explanation: str | None
    origin: str
    recorded_at: AwareDatetime
    group: MemoryGroup
    manifest_id: str
    grant_id: str
    run_id: str
    provenance: dict[str, object]
    source_problems: list[str] = Field(default_factory=list)
    effective_status: MemoryStatus
    usable: bool = False
    recommendation_enabled: Literal[False] = False
