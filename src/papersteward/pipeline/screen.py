"""确定性硬过滤；未知字段不会被伪装为不合格或推断为经典。"""

from datetime import date
from typing import Self

from pydantic import model_validator

from papersteward.sources.contracts import PaperCandidate, SourceModel, Text


class ScreenConfig(SourceModel):
    languages: tuple[Text, ...] = ()
    document_types: tuple[Text, ...] = ("article", "preprint", "review")
    date_from: date | None = None
    date_to: date | None = None
    exclude_terms: tuple[Text, ...] = ()
    blocked_authors: tuple[Text, ...] = ()
    blocked_venues: tuple[Text, ...] = ()
    predatory_venues: tuple[Text, ...] = ()
    classic_ids: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to")
        return self


def screen(candidate: PaperCandidate, config: ScreenConfig) -> list[str]:
    reasons: list[str] = []
    if (
        config.languages
        and candidate.language
        and candidate.language.casefold() not in {v.casefold() for v in config.languages}
    ):
        reasons.append("language")
    if (
        config.document_types
        and candidate.document_type
        and candidate.document_type.casefold() not in {v.casefold() for v in config.document_types}
    ):
        reasons.append("document_type")
    if candidate.doi not in config.classic_ids and candidate.arxiv_id not in config.classic_ids:
        published = candidate.published_at.date() if candidate.published_at else None
        if published and config.date_from and published < config.date_from:
            reasons.append("date_from")
        if published and config.date_to and published > config.date_to:
            reasons.append("date_to")
    text = (candidate.title + " " + (candidate.abstract or "")).casefold()
    reasons += ["exclude_term:" + term for term in config.exclude_terms if term.casefold() in text]
    names = {name.casefold() for name in candidate.authors}
    reasons += [
        "blocked_author:" + name for name in config.blocked_authors if name.casefold() in names
    ]
    venue = (candidate.venue or "").casefold()
    reasons += [
        "blocked_venue:" + value for value in config.blocked_venues if value.casefold() == venue
    ]
    reasons += [
        "predatory_venue:" + value for value in config.predatory_venues if value.casefold() == venue
    ]
    return reasons
