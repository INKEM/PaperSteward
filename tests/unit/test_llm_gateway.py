from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.llm import (
    FakeLLM,
    LLMGateway,
    LLMProviderError,
    LLMTimeoutError,
    LLMValidationError,
    PromptRenderError,
    timeout_failure,
)
from papersteward.schemas import ClassificationOutput, SummaryOutput

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
VALID = (FIXTURES / "classification.valid.json").read_text(encoding="utf-8")
INVALID = (FIXTURES / "classification.invalid.json").read_text(encoding="utf-8")
SUMMARY = (FIXTURES / "summary.valid.json").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)

VARIABLES = {
    "taxonomy": "T01 检索",
    "title": "A paper title",
    "abstract": "A paper abstract.",
}


def _gateway(provider: FakeLLM, **kwargs: object) -> LLMGateway:
    return LLMGateway(provider, clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


def test_valid_completion_records_provenance_and_usage() -> None:
    provider = FakeLLM(
        [VALID],
        model_id="fake-llm",
        latency_ms=12.0,
        cost_per_1k_tokens=2.0,
    )
    result = _gateway(provider).complete_json("classification", VARIABLES)

    assert isinstance(result.data, ClassificationOutput)
    assert result.raw_text == VALID
    assert result.repair_used is False
    assert result.attempts == 1
    assert result.provenance.model == "fake-llm"
    assert result.provenance.prompt_version == "classification/v2"
    assert result.provenance.generated_at == NOW
    assert len(result.provenance.input_hash) == 64
    assert result.usage.calls == 1
    assert result.usage.retries == 0
    assert result.usage.repairs == 0
    assert result.usage.prompt_tokens > 0
    assert result.usage.cost_usd > 0
    assert result.usage.latency_ms == 12.0
    assert "不可信数据" in provider.calls[0]
    assert "A paper title" in provider.calls[0]


def test_schema_invalid_output_is_repaired_once() -> None:
    provider = FakeLLM([INVALID, VALID], model_id="fake-llm")
    result = _gateway(provider).complete_json("classification", VARIABLES)

    assert isinstance(result.data, ClassificationOutput)
    assert result.repair_used is True
    assert result.provenance.prompt_version == "classification/v2+repair:repair/v2"
    assert result.usage.calls == 2
    assert result.usage.repairs == 1
    assert result.attempts == 2
    assert provider.call_count == 2
    assert "schema" in provider.calls[1]
    assert INVALID in provider.calls[1]


def test_repair_failure_raises_without_data() -> None:
    provider = FakeLLM([INVALID, INVALID])
    with pytest.raises(LLMValidationError) as exc_info:
        _gateway(provider).complete_json("classification", VARIABLES)

    error = exc_info.value
    assert error.field == "schema.classification.primary_confidence"
    assert error.raw_text == INVALID
    assert error.repair_text == INVALID
    assert error.usage is not None
    assert error.usage.calls == 2
    assert error.usage.repairs == 1
    assert error.attempts == 2


def test_repair_can_be_disabled() -> None:
    provider = FakeLLM([INVALID])
    with pytest.raises(LLMValidationError) as exc_info:
        _gateway(provider, repair_attempts=0).complete_json("classification", VARIABLES)

    assert exc_info.value.usage.repairs == 0
    assert provider.call_count == 1


def test_provider_timeout_retries_then_succeeds() -> None:
    provider = FakeLLM(
        [VALID],
        failures=[timeout_failure(), None],
    )
    result = _gateway(provider, max_attempts=3).complete_json("classification", VARIABLES)

    assert isinstance(result.data, ClassificationOutput)
    assert provider.call_count == 2
    assert result.usage.calls == 2
    assert result.usage.retries == 1
    assert result.attempts == 2


def test_all_provider_attempts_fail() -> None:
    provider = FakeLLM(
        [VALID],
        failures=[timeout_failure(), timeout_failure(), timeout_failure()],
    )
    with pytest.raises(LLMTimeoutError):
        _gateway(provider, max_attempts=3).complete_json("classification", VARIABLES)

    assert provider.call_count == 3


def test_summary_prompt_uses_summary_schema() -> None:
    provider = FakeLLM([SUMMARY])
    result = _gateway(provider).complete_json(
        "summary",
        {"title": "A paper title", "abstract": "A paper abstract."},
    )

    assert isinstance(result.data, SummaryOutput)
    assert result.provenance.prompt_version == "summary/v2"


def test_ping_and_input_hash_are_deterministic() -> None:
    provider = FakeLLM(["pong"])
    gateway = _gateway(provider)

    first = gateway.ping()
    assert first.text == "pong"
    assert first.usage.calls == 1

    result_a = _gateway(FakeLLM([VALID])).complete_json("classification", VARIABLES)
    result_b = _gateway(FakeLLM([VALID])).complete_json("classification", VARIABLES)
    assert result_a.provenance.input_hash == result_b.provenance.input_hash


def test_missing_prompt_variable_and_invalid_config_are_rejected() -> None:
    gateway = _gateway(FakeLLM([VALID]))
    with pytest.raises(PromptRenderError, match="缺少变量"):
        gateway.complete_json("classification", {"taxonomy": "T01"})

    with pytest.raises(ValueError):
        LLMGateway(FakeLLM([VALID]), max_attempts=0)
    with pytest.raises(ValueError):
        LLMGateway(FakeLLM([VALID]), repair_attempts=2)
    with pytest.raises(ValueError):
        LLMGateway(FakeLLM([VALID]), timeout_s=0)


def test_fake_llm_repeats_last_response_and_validates_constructor() -> None:
    provider = FakeLLM(["one", "two"])
    assert provider.complete("a", timeout_s=1).text == "one"
    assert provider.complete("b", timeout_s=1).text == "two"
    assert provider.complete("c", timeout_s=1).text == "two"

    with pytest.raises(ValueError):
        FakeLLM([])
    with pytest.raises(ValueError):
        FakeLLM(["x"], cost_per_1k_tokens=-1)


def test_non_retryable_provider_error_is_not_retried() -> None:
    class UnauthorizedProvider:
        model_id = "unauthorized"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str, *, timeout_s: float) -> object:
            self.calls += 1
            raise LLMProviderError("LLM 认证失败", retryable=False)

    provider = UnauthorizedProvider()
    with pytest.raises(LLMProviderError, match="认证失败") as caught:
        LLMGateway(provider, max_attempts=3).ping()  # type: ignore[arg-type]

    assert provider.calls == 1
    assert caught.value.usage.calls == 1


def test_overlong_secondary_tag_name_is_trimmed_not_failed() -> None:
    """模型把副标签名写成超长短语：截断到 64 字符直接通过，不触发修复/失败。"""
    import json

    payload = json.loads(VALID)
    long_name = "a very descriptive label that the model wrote as a full sentence " + "x" * 40
    assert len(long_name) > 64
    payload["secondary_tags"] = [
        {"facet": "method", "name": long_name, "confidence": 0.8},
        {"facet": "task", "name": "正常标签", "confidence": 0.7},
    ]
    provider = FakeLLM([json.dumps(payload, ensure_ascii=False)], model_id="fake-llm")
    result = _gateway(provider).complete_json("classification", VARIABLES)
    assert provider.call_count == 1  # 未走修复
    assert result.usage.repairs == 0
    names = [tag.name for tag in ClassificationOutput.model_validate(result.data).secondary_tags]
    assert names[0] == long_name[:64].rstrip() and len(names[0]) == 64
    assert names[1] == "正常标签"


def test_trim_deduplicates_colliding_secondary_tags() -> None:
    """截断后 facet+name 撞重的副标签保留首个（schema 要求唯一）。"""
    import json

    payload = json.loads(VALID)
    stem = "shared prefix " + "y" * 60
    payload["secondary_tags"] = [
        {"facet": "method", "name": stem + "AAA", "confidence": 0.8},
        {"facet": "method", "name": stem + "BBB", "confidence": 0.7},
    ]
    provider = FakeLLM([json.dumps(payload, ensure_ascii=False)], model_id="fake-llm")
    result = _gateway(provider).complete_json("classification", VARIABLES)
    tags = ClassificationOutput.model_validate(result.data).secondary_tags
    assert len(tags) == 1


def test_other_schema_violations_still_strict_after_trim() -> None:
    """白名单之外的违规（如非法 facet）仍走修复-失败路径，不被截断掩盖。"""
    import json

    payload = json.loads(VALID)
    payload["secondary_tags"] = [{"facet": "not-a-facet", "name": "x", "confidence": 0.8}]
    provider = FakeLLM(
        [json.dumps(payload, ensure_ascii=False), json.dumps(payload, ensure_ascii=False)],
        model_id="fake-llm",
    )
    with pytest.raises(LLMValidationError):
        _gateway(provider).complete_json("classification", VARIABLES)
