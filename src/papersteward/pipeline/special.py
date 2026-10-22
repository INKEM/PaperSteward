"""P4 特殊桶提名：review/classic/distant 证据、bridge memo 与 review 标记。

只产生提名，不改变主标签、归属桶或配额；最终归属与展示由 P4-S8/P7 决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from papersteward.core.entities import Paper, Provenance, ResearchProfile
from papersteward.core.enums import DocumentStatus, SpecialBucketKind
from papersteward.core.errors import DomainError
from papersteward.core.invariants import choose_special_filing_kind
from papersteward.llm import LLMGateway
from papersteward.pipeline.relevance import build_profile_block
from papersteward.schemas import (
    BridgeMemoOutput,
    SpecialNominationOutput,
    SpecialNominationsOutput,
)

SPECIAL_PROMPT_VERSION = "special_nomination/v1"
MIN_EVIDENCE_CHARS = 4
DEFAULT_MIN_SPECIAL_CONFIDENCE = 0.6


class SpecialError(DomainError):
    """特殊桶提名输入/配置错误；不包含论文正文或密钥。"""


@dataclass(frozen=True, slots=True)
class SpecialEvidence:
    quote: str
    valid: bool

    def __post_init__(self) -> None:
        if not isinstance(self.quote, str) or not self.quote.strip():
            raise SpecialError("special.evidence.quote", "不能为空")
        if not isinstance(self.valid, bool):
            raise SpecialError("special.evidence.valid", "必须是布尔值")
        object.__setattr__(self, "quote", self.quote.strip())


@dataclass(frozen=True, slots=True)
class SpecialNomination:
    kind: SpecialBucketKind
    confidence: float
    reason: str
    evidence: tuple[SpecialEvidence, ...]
    review_layers: tuple[str, ...]
    classic_basis: tuple[str, ...]
    bridge_memo: BridgeMemoOutput | None
    accepted: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SpecialBucketKind):
            raise SpecialError("special.kind", "必须是 SpecialBucketKind")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise SpecialError("special.confidence", "必须在 0–1 之间")
        object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise SpecialError("special.reason", "不能为空")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, SpecialEvidence) for item in self.evidence
        ):
            raise SpecialError("special.evidence", "必须是 SpecialEvidence 元组")
        for name in ("review_layers", "classic_basis", "reasons"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise SpecialError(f"special.{name}", "必须是非空字符串元组")
        if self.bridge_memo is not None and not isinstance(self.bridge_memo, BridgeMemoOutput):
            raise SpecialError("special.bridge_memo", "必须是 BridgeMemoOutput 或 None")
        if not isinstance(self.accepted, bool):
            raise SpecialError("special.accepted", "必须是布尔值")
        if self.accepted != (not self.reasons):
            raise SpecialError("special.accepted", "必须与 reasons 一致")

    def as_json(self) -> dict[str, Any]:
        return {
            "bucket": self.kind.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "review_layers": list(self.review_layers),
            "classic_basis": list(self.classic_basis),
            "bridge_memo": (
                self.bridge_memo.model_dump(mode="json") if self.bridge_memo is not None else None
            ),
            "evidence": [{"quote": item.quote, "valid": item.valid} for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class SpecialNominationResult:
    paper_id: str
    nominations: tuple[SpecialNomination, ...]
    selected_kind: SpecialBucketKind | None
    needs_review: bool
    reasons: tuple[str, ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.paper_id, str) or not self.paper_id.strip():
            raise SpecialError("special.paper_id", "不能为空")
        if not isinstance(self.nominations, tuple) or any(
            not isinstance(item, SpecialNomination) for item in self.nominations
        ):
            raise SpecialError("special.nominations", "必须是 SpecialNomination 元组")
        expected = choose_special_filing_kind(
            [item.kind for item in self.nominations if item.accepted]
        )
        if self.selected_kind is not expected:
            raise SpecialError("special.selected_kind", "必须按 review > classic > distant 选择")
        if not isinstance(self.needs_review, bool):
            raise SpecialError("special.needs_review", "必须是布尔值")
        if self.needs_review != bool(self.reasons):
            raise SpecialError("special.needs_review", "必须与 reasons 一致")
        if not isinstance(self.provenance, Provenance):
            raise SpecialError("special.provenance", "必须是 Provenance")

    @property
    def accepted(self) -> tuple[SpecialNomination, ...]:
        return tuple(item for item in self.nominations if item.accepted)

    def as_json(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "selected_kind": self.selected_kind.value if self.selected_kind else None,
            "accepted": [item.kind.value for item in self.accepted],
            "needs_review": self.needs_review,
            "reasons": list(self.reasons),
            "nominations": [item.as_json() for item in self.nominations],
            "provenance": {
                "model": self.provenance.model,
                "prompt_version": self.provenance.prompt_version,
                "generated_at": self.provenance.generated_at.isoformat(),
                "input_hash": self.provenance.input_hash,
            },
        }


def _paper_text(paper: Paper) -> str:
    return "\n\n".join(part for part in (paper.title, paper.abstract) if part)


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _nomination_evidence(
    output: SpecialNominationOutput, paper_text: str
) -> tuple[SpecialEvidence, ...]:
    normalized = _normalize_text(paper_text)
    result: list[SpecialEvidence] = []
    for item in output.evidence:
        quote = item.quote.strip()
        valid = len(quote) >= MIN_EVIDENCE_CHARS and _normalize_text(quote) in normalized
        result.append(SpecialEvidence(quote, valid))
    return tuple(result)


def _nomination(
    output: SpecialNominationOutput, paper_text: str, *, min_confidence: float
) -> SpecialNomination:
    evidence = _nomination_evidence(output, paper_text)
    reasons: list[str] = []
    if not evidence or not any(item.valid for item in evidence):
        reasons.append("missing_evidence")
    if any(not item.valid for item in evidence):
        reasons.append("invalid_evidence")
    if output.confidence < min_confidence:
        reasons.append("low_confidence")
    if output.bucket is SpecialBucketKind.DISTANT and output.bridge_memo is None:
        reasons.append("missing_bridge_memo")
    if output.bucket is SpecialBucketKind.REVIEW and len(set(output.review_layers)) < 2:
        reasons.append("insufficient_review_layers")
    if output.bucket is SpecialBucketKind.CLASSIC and not output.classic_basis:
        reasons.append("missing_classic_basis")
    return SpecialNomination(
        kind=output.bucket,
        confidence=output.confidence,
        reason=output.reason.strip(),
        evidence=evidence,
        review_layers=tuple(output.review_layers),
        classic_basis=tuple(output.classic_basis),
        bridge_memo=output.bridge_memo,
        accepted=not reasons,
        reasons=tuple(reasons),
    )


def nominate_special(
    paper: Paper,
    profile: ResearchProfile,
    gateway: LLMGateway,
    *,
    min_confidence: float = DEFAULT_MIN_SPECIAL_CONFIDENCE,
) -> SpecialNominationResult:
    """调用 special_nomination/v1；只提名，不写库、不改变归属。"""
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    if not isinstance(profile, ResearchProfile):
        raise TypeError("profile 必须是 ResearchProfile")
    if profile.status is not DocumentStatus.CONFIRMED:
        raise SpecialError("special.profile", "只允许使用已确认 profile")
    if not isinstance(gateway, LLMGateway):
        raise TypeError("gateway 必须是 LLMGateway")
    if (
        isinstance(min_confidence, bool)
        or not isinstance(min_confidence, int | float)
        or not 0.0 <= min_confidence <= 1.0
    ):
        raise SpecialError("special.min_confidence", "必须在 0–1 之间")
    result = gateway.complete_json(
        "special_nomination",
        {
            "profile": build_profile_block(profile),
            "title": paper.title,
            "abstract": paper.abstract or "（无摘要）",
        },
        prompt_version=SPECIAL_PROMPT_VERSION,
    )
    if not isinstance(result.data, SpecialNominationsOutput):
        raise SpecialError("special.schema", "LLM 返回了非 special_nomination 输出")
    paper_text = _paper_text(paper)
    nominations = tuple(
        _nomination(item, paper_text, min_confidence=min_confidence)
        for item in result.data.nominations
    )
    overall_reasons: list[str] = []
    for item in nominations:
        overall_reasons.extend(f"{reason}:{item.kind.value}" for reason in item.reasons)
    if not paper.abstract:
        overall_reasons.append("missing_abstract")
    deduplicated = tuple(dict.fromkeys(overall_reasons))
    selected = choose_special_filing_kind([item.kind for item in nominations if item.accepted])
    return SpecialNominationResult(
        paper_id=paper.id,
        nominations=nominations,
        selected_kind=selected,
        needs_review=bool(deduplicated),
        reasons=deduplicated,
        provenance=result.provenance,
    )


__all__ = [
    "DEFAULT_MIN_SPECIAL_CONFIDENCE",
    "SPECIAL_PROMPT_VERSION",
    "SpecialError",
    "SpecialEvidence",
    "SpecialNomination",
    "SpecialNominationResult",
    "nominate_special",
]
