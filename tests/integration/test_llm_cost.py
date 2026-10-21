from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from papersteward.core.entities import Paper, Run
from papersteward.llm import (
    FakeLLM,
    LLMGateway,
    LLMValidationError,
    record_llm_usage,
)
from papersteward.storage import (
    PaperRepository,
    RunRepository,
    SummaryRepository,
    create_session_factory,
    session_scope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
VALID = (FIXTURES / "classification.valid.json").read_text(encoding="utf-8")
INVALID = (FIXTURES / "classification.invalid.json").read_text(encoding="utf-8")
VARIABLES = {"taxonomy": "T01", "title": "T", "abstract": "A"}


def _gateway(*responses: str) -> LLMGateway:
    return LLMGateway(
        FakeLLM(list(responses), latency_ms=10.0, cost_per_1k_tokens=2.0),
        clock=lambda: datetime(2026, 10, 21, 8, 0, tzinfo=UTC),
    )


def test_record_llm_usage_accumulates_cost(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    result1 = _gateway(VALID).complete_json("classification", VARIABLES)
    result2 = _gateway(VALID).complete_json("classification", VARIABLES)

    with session_scope(factory) as session:
        RunRepository(session).add(Run(id="run-1", run_type="ingest"))

    with session_scope(factory) as session:
        updated = record_llm_usage(session, "run-1", result1.usage)
        assert updated is not None
        assert updated.cost_json["llm_call_count"] == 1

    with session_scope(factory) as session:
        updated = record_llm_usage(session, "run-1", result2.usage)
        assert updated is not None
        cost = updated.cost_json
        assert cost["llm_call_count"] == 2
        assert cost["llm_prompt_tokens"] > 0
        assert cost["llm_completion_tokens"] > 0
        assert cost["llm_cost_usd"] > 0
        assert cost["llm_latency_ms"] == pytest.approx(20.0)
        serialized = json.dumps(cost)
        assert VARIABLES["title"] not in serialized
        assert VARIABLES["abstract"] not in serialized
        assert record_llm_usage(session, "missing", result1.usage) is None


def test_invalid_llm_output_does_not_write_business_tables(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        PaperRepository(session).add(Paper(id="p001", title="RAG"))

    with session_scope(factory) as session:
        with pytest.raises(LLMValidationError):
            _gateway(INVALID, INVALID).complete_json("classification", VARIABLES)
        assert SummaryRepository(session).get("p001") is None
