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


def _relevance() -> dict[str, object]:
    return {
        "problem": 5,
        "method": 4,
        "domain": 5,
        "actionability": 4,
        "evidence": [
            {"dimension": "problem", "quote": "long-document retrieval"},
            {"dimension": "method", "quote": "cross-encoder reranking"},
            {"dimension": "domain", "quote": "long-document QA benchmarks"},
            {"dimension": "actionability", "quote": "improve retrieval"},
        ],
        "confidence": 0.86,
    }


def _special_nomination() -> dict[str, object]:
    return {
        "nominations": [
            {
                "bucket": "review",
                "confidence": 0.82,
                "reason": "比较检索与生成评测两个层级。",
                "evidence": [{"quote": "retrieval and generation evaluation"}],
                "review_layers": ["method", "evaluation"],
            }
        ]
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


def _special_scoring() -> dict[str, object]:
    payload = _scoring()
    del payload["priority"]
    payload["tier"] = "high"
    return payload


def _event() -> dict[str, object]:
    return {
        "type": "paper.done",
        "paper_id": "p001",
        "payload": {"read_mode": "deep", "score": 4},
        "created_at": "2026-10-21T21:15:00+08:00",
    }


def test_schema_registry_and_generated_json_schema() -> None:
    assert set(SCHEMA_MODELS) == {
        "classification",
        "relevance",
        "summary",
        "scoring",
        "special_scoring",
        "special_nomination",
        "event",
        "profile_extraction",
    }

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
        ("relevance", _relevance()),
        ("summary", _summary()),
        ("scoring", _scoring()),
        ("special_scoring", _special_scoring()),
        ("special_nomination", _special_nomination()),
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


def test_special_scoring_uses_tier_without_priority() -> None:
    payload = _special_scoring()
    assert validate_mapping("special_scoring", payload)

    payload["priority"] = {"P": 75.1, "rank_score": 74.6}
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("special_scoring", payload)
    assert exc_info.value.field == "schema.special_scoring.priority"

    payload = _special_scoring()
    del payload["tier"]
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("special_scoring", payload)
    assert exc_info.value.field == "schema.special_scoring.tier"


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


def test_relevance_rejects_out_of_range_and_missing_evidence() -> None:
    payload = _relevance()
    payload["problem"] = 6
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("relevance", payload)
    assert exc_info.value.field == "schema.relevance.problem"

    payload = _relevance()
    payload["evidence"] = []
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("relevance", payload)
    assert exc_info.value.field == "schema.relevance.evidence"


def test_classification_runner_up_and_evidence_contract() -> None:
    payload = _classification()
    payload.update(
        runner_up_tag="T01",
        runner_up_confidence=0.62,
        evidence=[{"quote": "retrieval-augmented generation"}],
    )
    model = validate_mapping("classification", payload)
    assert model.model_dump(mode="json")["runner_up_tag"] == "T01"

    payload = _classification()
    payload["runner_up_tag"] = "T01"
    with pytest.raises(SchemaError) as exc_info:
        validate_mapping("classification", payload)
    assert "runner_up_confidence" in str(exc_info.value)

    payload = _classification()
    payload.update(runner_up_tag="T02", runner_up_confidence=0.5)
    with pytest.raises(SchemaError, match="runner_up_tag"):
        validate_mapping("classification", payload)


def test_special_nomination_bucket_specific_requirements() -> None:
    payload = _special_nomination()
    payload["nominations"][0]["review_layers"] = ["method"]
    with pytest.raises(SchemaError, match="两个层级"):
        validate_mapping("special_nomination", payload)

    payload = _special_nomination()
    payload["nominations"][0]["bucket"] = "distant"
    with pytest.raises(SchemaError, match="bridge_memo"):
        validate_mapping("special_nomination", payload)

    payload = _special_nomination()
    payload["nominations"][0]["bucket"] = "classic"
    payload["nominations"][0]["review_layers"] = []
    with pytest.raises(SchemaError, match="经典依据"):
        validate_mapping("special_nomination", payload)

    payload = _special_nomination()
    payload["nominations"].append(dict(payload["nominations"][0]))
    with pytest.raises(SchemaError, match="只能提名一次"):
        validate_mapping("special_nomination", payload)


def test_profile_extraction_fixture_validates() -> None:
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "fixtures/llm/profile_extraction.valid.json"
    model = validate_json_text("profile_extraction", fixture.read_text(encoding="utf-8"))
    data = model.model_dump(mode="json")
    assert len(data["tags"]) == 5
    assert abs(sum(item["target_share"] for item in data["profile"]["subdirections"]) - 1) < 1e-9
