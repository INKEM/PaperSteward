"""可解释、离线、保守的候选规范化与去重。"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import unquote

from papersteward.sources.contracts import PaperCandidate, SourceProvenance


def normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", unquote(value.strip()), flags=re.I
    ).casefold()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def normalize_arxiv(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(
        r"^(?:https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:\s*)",
        "",
        value.strip(),
        flags=re.I,
    )
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = re.sub(r"v\d+$", "", value, flags=re.I).casefold()
    return value if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})", value) else None


def normalized_text(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def canonicalize(candidate: PaperCandidate) -> PaperCandidate:
    kind = (candidate.document_type or "").casefold()
    kind = {
        "journalarticle": "article",
        "conference": "article",
        "review": "review",
        "journal-article": "article",
        "conference-paper": "article",
    }.get(kind, kind)
    return PaperCandidate.model_validate(
        {
            **candidate.model_dump(),
            "doi": normalize_doi(candidate.doi),
            "arxiv_id": normalize_arxiv(candidate.arxiv_id),
            "document_type": kind or None,
        }
    )


def duplicate_reason(left: PaperCandidate, right: PaperCandidate) -> str | None:
    return _reason(canonicalize(left), canonicalize(right))


def _conflict(a: PaperCandidate, b: PaperCandidate) -> bool:
    return bool(
        (a.doi and b.doi and a.doi != b.doi)
        or (a.arxiv_id and b.arxiv_id and a.arxiv_id != b.arxiv_id)
    )


def _reason(a: PaperCandidate, b: PaperCandidate) -> str | None:
    if a.doi and a.doi == b.doi:
        return "doi"
    if a.arxiv_id and a.arxiv_id == b.arxiv_id:
        return "arxiv"
    if _conflict(a, b):
        return None
    ta, tb = normalized_text(a.title), normalized_text(b.title)
    aa, ab = (
        {normalized_text(name) for name in a.authors},
        {normalized_text(name) for name in b.authors},
    )
    overlap = bool(aa & ab)
    if ta == tb and (len(ta) >= 20 or overlap) and (not aa or not ab or overlap):
        return "title"
    ratio = SequenceMatcher(None, ta, tb, autojunk=False).ratio()
    if min(len(ta), len(tb)) >= 20 and ratio >= 0.94 and overlap:
        return "title"
    tokens_a, tokens_b = set(ta.split()), set(tb.split())
    if (
        a.published_at
        and b.published_at
        and a.published_at.year == b.published_at.year
        and a.venue
        and b.venue
        and normalized_text(a.venue) == normalized_text(b.venue)
        and aa
        and aa == ab
        and ratio >= 0.65
        and len(tokens_a & tokens_b) / max(len(tokens_a | tokens_b), 1) >= 0.7
    ):
        return "author_year"
    if (not aa or not ab or overlap) and a.abstract and b.abstract:
        wa, wb = normalized_text(a.abstract).split(), normalized_text(b.abstract).split()
        if min(len(wa), len(wb)) >= 30:
            sa = {tuple(wa[i : i + 5]) for i in range(len(wa) - 4)}
            sb = {tuple(wb[i : i + 5]) for i in range(len(wb) - 4)}
            if min(len(sa), len(sb)) >= 20 and len(sa & sb) / len(sa | sb) >= 0.95:
                return "abstract"
    return None


@dataclass(frozen=True)
class DedupeGroup:
    candidate: PaperCandidate
    members: tuple[PaperCandidate, ...]
    reasons: tuple[str, ...]

    @property
    def provenances(self) -> tuple[SourceProvenance, ...]:
        return tuple(member.provenance for member in self.members)

    @property
    def canonical_id(self) -> str:
        item = self.candidate
        key = (
            item.doi
            or item.arxiv_id
            or "|".join(
                (
                    normalized_text(item.title),
                    ",".join(normalized_text(name) for name in item.authors),
                    str(item.published_at.year) if item.published_at else "",
                )
            )
        )
        return "p_" + hashlib.sha256(key.encode()).hexdigest()[:32]


def _order(item: PaperCandidate) -> tuple[int, str, str, str]:
    formal = bool(item.venue and item.doi and item.document_type != "preprint")
    return (
        0 if formal else 1,
        item.provenance.source,
        item.provenance.source_id,
        item.model_dump_json(),
    )


def _merge(members: list[PaperCandidate], reasons: list[str]) -> DedupeGroup:
    members = sorted(members, key=_order)
    values = members[0].model_dump()
    values["authors"] = next((item.authors for item in members if item.authors), ())
    for field in ("doi", "arxiv_id", "venue", "url", "pdf_url", "language", "document_type"):
        values[field] = next(
            (getattr(item, field) for item in members if getattr(item, field)), None
        )
    dates = [item.published_at for item in members if item.published_at]
    values["published_at"] = min(dates) if dates else None
    abstracts = [item for item in members if item.abstract]
    values["abstract"] = (
        min(
            abstracts,
            key=lambda item: item.published_at.isoformat() if item.published_at else "9999",
        ).abstract
        if abstracts
        else None
    )
    counts = [item.citation_count for item in members if item.citation_count is not None]
    values["citation_count"] = max(counts) if counts else None
    values["oa_status"] = (
        "open"
        if any(item.oa_status.value == "open" for item in members)
        else ("closed" if any(item.oa_status.value == "closed" for item in members) else "unknown")
    )
    return DedupeGroup(PaperCandidate.model_validate(values), tuple(members), tuple(reasons))


def deduplicate(candidates: list[PaperCandidate]) -> list[DedupeGroup]:
    groups: list[DedupeGroup] = []
    for item in sorted((canonicalize(item) for item in candidates), key=_order):
        hits: list[tuple[int, str]] = []
        priority = ["doi", "arxiv", "title", "author_year", "abstract"]
        for index, group in enumerate(groups):
            matches = [reason for member in group.members if (reason := _reason(item, member))]
            if not matches:
                continue
            reason = min(matches, key=priority.index)
            if reason not in {"doi", "arxiv"} and any(
                _conflict(item, member) for member in group.members
            ):
                continue
            hits.append((index, reason))
        if not all(reason in {"doi", "arxiv"} for _, reason in hits) and any(
            _conflict(left, right)
            for position, (index, _) in enumerate(hits)
            for other, _ in hits[position + 1 :]
            for left in groups[index].members
            for right in groups[other].members
        ):
            hits = []  # 无强ID的歧义桥接保持独立，避免把两篇已知不同文献串起来。
        members = [item]
        reasons: list[str] = []
        for index, reason in reversed(hits):
            old = groups.pop(index)
            members.extend(old.members)
            reasons.extend((*old.reasons, reason))
        groups.append(_merge(members, reasons))
    return sorted(groups, key=lambda group: group.canonical_id)
