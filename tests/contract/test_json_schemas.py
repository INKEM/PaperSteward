from __future__ import annotations

import json

import pytest

from papersteward.schemas import (
    SCHEMA_MODELS,
    SchemaError,
    schema_for,
    validate_json_text,
    validate_mapping,
)

pytestmark = pytest.mark.contract


def _classification() -> dict[str, object]:
    return {
        "primary_tag": "T02",
        "primary_confidence": 0.86,
        "secondary_tags": [
            {"facet": "method", "name": "sparse retrieval", "confidence": 0.92},
            {"facet": "task", "name": "long-document QA", "confidence": 0.88},
        ],
        "special_candidates": [
            {
                "bucket": "review",
                "confidence": 0.91,
                "reason": "系统梳理检索、重排与生成三层方法，并给出跨层比较。",
            }
        ],
        "boundary_note": "贡献主战场在生成侧。",
    }


def _summary() -> dict[str, object]:
    return {
        "summary_100": (
            "针对长文档问答提出层次化检索与跨段重排；"
            "三个基准上 EM 提升 2–4 点；离线聚类带来较高延迟。"
        ),
        "what": "长文档问答的检索与重排",
        "how": "段落聚类召回 + 跨层注意力重排",
        "finding": "三个基准 EM 提升 2–4 点",
        "limitation": "离线聚类导致较高延迟",
        "evidence": [
            {"field": "finding", "quote": "improves EM by 2–4 points"},
            {"field": "limitation", "quote": "offline clustering adds latency"},
        ],
    }


def _scoring() -> dict[str, object]:
    return {
        "paper_id": "p001",
        "date": "2026-10-21",
        "profile_version": "1.0",
        "taxonomy_version": "1.0",
        "config_version": "1.0.0",
        "static": {"R": 84, "A": 72, "E": 65, "S": 78.8},
        "dynamic": {"F": 80, "H": 55, "D": 70.0},
        "quasi": {"U": 60, "confidence": 0.4},
        "priority": {"P": 75.1, "rank_score": 74.6},
        "explanation": ["问题契合度 5/5", "方法可复用", "新鲜度较高"],
    }


def _event() -> dict[str, object]:
    return {
        "type": "paper.done",
        "paper_id": "p001",
        "payload": {"read_mode": "deep", "score": 4},
        "created_at": "2026-10-21T21:15:00+08:00",
    }


def test_schema_registry_and_generated_json_schema() -> None:
    assert set(SCHEMA_MODELS) == {"classification", "summary", "scoring", "event"}

    classification = schema_for("classification")
    assert classification["additionalProperties"] is False
    assert {"primary_tag", "primary_confidence"} <= set(classification["required"])
    assert classification["properties"]["primary_confidence"]["maximum"] == 1.0
    json.dumps(classification)

    with pytest.raises(SchemaError, match="未知 schema"):
        schema_for("unknown")


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("classification", _classification()),
        ("summary", _summary()),
        ("scoring", _scoring()),
        ("event", _event()),
    ],
)
def test_valid_payloads(name: str, payload: dict[str, object]) -> None:
    model = validate_mapping(name, payload)
    assert model.model_dump(mode="json")


def test_classification_rejects_bad_values_duplicates_and_extra_fields() -> None:
    payload = _classification()
    payload["primary_confidence"] = 1.2
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("classification", payload)
    assert exc_info.value.field == "schema.classification.primary_confidence"

    payload = _classification()
    payload["secondary_tags"] = [
        {"facet": "method", "name": "same", "confidence": 0.9},
        {"facet": "method", "name": "Same", "confidence": 0.8},
    ]
    with pytest.raises(SchemaError, match="不能重复"):
        validate_mapping("classification", payload)

    payload = _classification()
    payload["secondary_tags"] = [
        {"facet": "method", "name": f"tag {index}", "confidence": 0.9} for index in range(6)
    ]
    with pytest.raises(SchemaError):
        validate_mapping("classification", payload)

    payload = _classification()
    payload["unexpected"] = True
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("classification", payload)
    assert exc_info.value.field == "schema.classification.unexpected"


def test_summary_rejects_too_long_and_missing_evidence_slots() -> None:
    payload = _summary()
    payload["summary_100"] = "字" * 101
    with pytest.raises(SchemaError, match="100"):
        validate_mapping("summary", payload)

    payload = _summary()
    payload["evidence"] = []
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("summary", payload)
    assert exc_info.value.field == "schema.summary.evidence"

    payload = _summary()
    payload["evidence"] = [{"field": "unknown", "quote": "x"}]
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("summary", payload)
    assert exc_info.value.field == "schema.summary.evidence.0.field"


def test_scoring_rejects_out_of_range_and_missing_explanation() -> None:
    payload = _scoring()
    payload["static"] = {"R": 101, "A": 72, "E": 65, "S": 78.8}
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("scoring", payload)
    assert exc_info.value.field == "schema.scoring.static.R"

    payload = _scoring()
    payload["explanation"] = []
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("scoring", payload)
    assert exc_info.value.field == "schema.scoring.explanation"


def test_event_requires_paper_id_for_paper_events_and_aware_time() -> None:
    payload = _event()
    del payload["paper_id"]
    with pytest.raises(SchemaError, match="paper_id"):
        validate_mapping("event", payload)

    payload = _event()
    payload["created_at"] = "2026-10-21T21:15:00"
    with pytest.raises(SchemaError, match="时区"):
        validate_mapping("event", payload)

    document_event = {
        "type": "profile.confirmed",
        "payload": {"document_id": "demo-profile", "version": "1.0"},
        "created_at": "2026-10-21T21:15:00+08:00",
    }
    assert validate_mapping("event", document_event).model_dump()["type"] == "profile.confirmed"


def test_validate_json_text_handles_syntax_and_type_errors() -> None:
    assert validate_json_text("classification", json.dumps(_classification()))
    with pytest.raises(SchemaError, match="JSON 解析失败"):
        validate_json_text("classification", "{not-json", source="bad.json")
    with pytest.raises(SchemaError, match="根节点"):
        validate_json_text("classification", "[]", source="bad.json")
