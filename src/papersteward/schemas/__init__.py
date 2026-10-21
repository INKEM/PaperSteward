"""Agent 输出 JSON schema 注册表。"""

from __future__ import annotations

from papersteward.schemas.models import (
    SCHEMA_MODELS,
    ClassificationOutput,
    DynamicScoreOutput,
    EventEnvelope,
    EvidenceItem,
    PriorityScoreOutput,
    QuasiScoreOutput,
    SchemaError,
    SchemaName,
    ScoreSnapshotOutput,
    SecondaryTagOutput,
    SpecialCandidateOutput,
    StaticScoreOutput,
    SummaryOutput,
    schema_for,
    validate_json_text,
    validate_mapping,
)

__all__ = [
    "SCHEMA_MODELS",
    "ClassificationOutput",
    "DynamicScoreOutput",
    "EventEnvelope",
    "EvidenceItem",
    "PriorityScoreOutput",
    "QuasiScoreOutput",
    "SchemaError",
    "SchemaName",
    "ScoreSnapshotOutput",
    "SecondaryTagOutput",
    "SpecialCandidateOutput",
    "StaticScoreOutput",
    "SummaryOutput",
    "schema_for",
    "validate_json_text",
    "validate_mapping",
]
