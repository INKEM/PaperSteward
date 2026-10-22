"""P4 摘要：100 字 + 四槽 + 逐字段证据/数字校验；失败进入 needs_review。

``LLMGateway`` 已负责一次 schema 修复重试；本模块只做业务校验与持久化，不访问
网络、不改变 PaperStatus。摘要文本仍来自不可信论文内容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from papersteward.core.entities import Paper, Provenance, Summary
from papersteward.core.enums import SummaryValidationStatus
from papersteward.core.errors import DomainError
from papersteward.llm import LLMGateway
from papersteward.schemas import SummaryOutput
from papersteward.storage.repositories import SummaryRepository

EvidenceField = Literal["what", "how", "finding", "limitation"]
EVIDENCE_FIELDS: tuple[EvidenceField, ...] = ("what", "how", "finding", "limitation")
SUMMARY_PROMPT_VERSION = "summary/v2"
MAX_SUMMARY_CHARS = 100
MIN_EVIDENCE_CHARS = 4
VAGUE_TERMS: tuple[str, ...] = ("很好", "重要价值", "新颖", "非常全面", "效果很好")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")


class SummaryError(DomainError):
    """摘要生成/校验/持久化错误；不包含论文正文、prompt 或密钥。"""


@dataclass(frozen=True, slots=True)
class SummaryEvidence:
    field: EvidenceField
    quote: str
    valid: bool

    def __post_init__(self) -> None:
        if self.field not in EVIDENCE_FIELDS:
            raise SummaryError("summary.evidence.field", "未知槽位")
        if not isinstance(self.quote, str) or not self.quote.strip():
            raise SummaryError("summary.evidence.quote", "不能为空")
        if not isinstance(self.valid, bool):
            raise SummaryError("summary.evidence.valid", "必须是布尔值")
        object.__setattr__(self, "quote", self.quote.strip())


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """一次摘要生成结果；缺失/伪造证据已转为 review，不静默丢弃。"""

    summary: Summary
    evidence: tuple[SummaryEvidence, ...]
    missing_fields: tuple[EvidenceField, ...]
    invalid_quotes: tuple[str, ...]
    unsupported_numbers: tuple[str, ...]
    vague_terms: tuple[str, ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.summary, Summary):
            raise SummaryError("summary.result", "必须是 Summary")
        if not isinstance(self.provenance, Provenance):
            raise SummaryError("summary.provenance", "必须是 Provenance")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, SummaryEvidence) for item in self.evidence
        ):
            raise SummaryError("summary.evidence", "必须是 SummaryEvidence 元组")
        for field in self.missing_fields:
            if field not in EVIDENCE_FIELDS:
                raise SummaryError("summary.missing_fields", "未知槽位")
        if len(set(self.missing_fields)) != len(self.missing_fields):
            raise SummaryError("summary.missing_fields", "不能重复")
        for values, name in (
            (self.invalid_quotes, "invalid_quotes"),
            (self.unsupported_numbers, "unsupported_numbers"),
            (self.vague_terms, "vague_terms"),
        ):
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise SummaryError(f"summary.{name}", "必须是非空字符串元组")
        expected = (
            SummaryValidationStatus.NEEDS_REVIEW
            if self.needs_review
            else SummaryValidationStatus.PASSED
        )
        if self.summary.validation_status is not expected:
            raise SummaryError("summary.validation_status", "与校验结果不一致")
        if self.summary.provenance != self.provenance:
            raise SummaryError("summary.provenance", "与结果 provenance 不一致")

    @property
    def paper_id(self) -> str:
        return self.summary.paper_id

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = [f"missing_evidence:{field}" for field in self.missing_fields]
        if self.invalid_quotes:
            reasons.append("invalid_evidence")
        reasons.extend(f"unsupported_number:{number}" for number in self.unsupported_numbers)
        reasons.extend(f"vague_language:{term}" for term in self.vague_terms)
        return tuple(dict.fromkeys(reasons))

    @property
    def needs_review(self) -> bool:
        return bool(
            self.missing_fields
            or self.invalid_quotes
            or self.unsupported_numbers
            or self.vague_terms
        )

    def as_json(self) -> dict[str, Any]:
        summary = self.summary
        return {
            "paper_id": summary.paper_id,
            "summary_100": summary.summary_100,
            "what": summary.what,
            "how": summary.how,
            "finding": summary.finding,
            "limitation": summary.limitation,
            "validation_status": summary.validation_status.value,
            "evidence": [
                {"field": item.field, "quote": item.quote, "valid": item.valid}
                for item in self.evidence
            ],
            "missing_fields": list(self.missing_fields),
            "invalid_quotes": list(self.invalid_quotes),
            "unsupported_numbers": list(self.unsupported_numbers),
            "vague_terms": list(self.vague_terms),
            "needs_review": self.needs_review,
            "reasons": list(self.reasons),
            "provenance": {
                "model": self.provenance.model,
                "prompt_version": self.provenance.prompt_version,
                "generated_at": self.provenance.generated_at.isoformat(),
                "input_hash": self.provenance.input_hash,
            },
        }


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _paper_text(paper: Paper) -> str:
    return "\n\n".join(part for part in (paper.title, paper.abstract) if part)


def _valid_evidence(output: SummaryOutput, paper_text: str) -> tuple[SummaryEvidence, ...]:
    normalized = _normalize_text(paper_text)
    result: list[SummaryEvidence] = []
    for item in output.evidence:
        quote = item.quote.strip()
        valid = len(quote) >= MIN_EVIDENCE_CHARS and _normalize_text(quote) in normalized
        result.append(SummaryEvidence(item.field, quote, valid))
    return tuple(result)


def _unsupported_numbers(text: str, evidence: tuple[SummaryEvidence, ...]) -> tuple[str, ...]:
    evidence_numbers = {
        number for item in evidence if item.valid for number in _NUMBER_RE.findall(item.quote)
    }
    summary_numbers = _NUMBER_RE.findall(text)
    return tuple(
        dict.fromkeys(number for number in summary_numbers if number not in evidence_numbers)
    )


def generate_summary(paper: Paper, gateway: LLMGateway) -> SummaryResult:
    """调用 summary/v2 并校验槽位、证据与数字；LLM schema 失败向上抛出。"""
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if not paper.abstract or not paper.abstract.strip():
        raise SummaryError("summary.abstract", "缺少摘要，不能生成概括")
    if not isinstance(gateway, LLMGateway):
        raise TypeError("gateway 必须是 LLMGateway")

    result = gateway.complete_json(
        "summary",
        {"title": paper.title, "abstract": paper.abstract},
        prompt_version=SUMMARY_PROMPT_VERSION,
    )
    if not isinstance(result.data, SummaryOutput):
        raise SummaryError("summary.schema", "LLM 返回了非 summary 输出")
    output = result.data
    evidence = _valid_evidence(output, _paper_text(paper))
    valid_fields = {item.field for item in evidence if item.valid}
    missing_fields = tuple(field for field in EVIDENCE_FIELDS if field not in valid_fields)
    invalid_quotes = tuple(item.quote for item in evidence if not item.valid)
    combined_text = " ".join(
        (
            output.summary_100,
            output.what,
            output.how,
            output.finding,
            output.limitation,
        )
    )
    unsupported_numbers = _unsupported_numbers(combined_text, evidence)
    vague_terms = tuple(term for term in VAGUE_TERMS if term in combined_text)
    needs_review = bool(missing_fields or invalid_quotes or unsupported_numbers or vague_terms)
    summary = Summary(
        paper_id=paper.id,
        summary_100=output.summary_100,
        what=output.what,
        how=output.how,
        finding=output.finding,
        limitation=output.limitation,
        evidence=tuple(item.quote for item in evidence if item.valid),
        validation_status=(
            SummaryValidationStatus.NEEDS_REVIEW if needs_review else SummaryValidationStatus.PASSED
        ),
        provenance=result.provenance,
    )
    return SummaryResult(
        summary=summary,
        evidence=evidence,
        missing_fields=missing_fields,
        invalid_quotes=invalid_quotes,
        unsupported_numbers=unsupported_numbers,
        vague_terms=vague_terms,
        provenance=result.provenance,
    )


def persist_summary(session: Session, result: SummaryResult) -> Summary:
    """保存/覆盖摘要；是否进入 review 由 validation_status 表达，不提交事务。"""
    if not isinstance(session, Session):
        raise TypeError("session 必须是 Session")
    if not isinstance(result, SummaryResult):
        raise TypeError("result 必须是 SummaryResult")
    return SummaryRepository(session).save(result.summary)


__all__ = [
    "EVIDENCE_FIELDS",
    "MAX_SUMMARY_CHARS",
    "SUMMARY_PROMPT_VERSION",
    "SummaryError",
    "SummaryEvidence",
    "SummaryResult",
    "VAGUE_TERMS",
    "generate_summary",
    "persist_summary",
]
