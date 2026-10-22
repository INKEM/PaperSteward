"""OpenAlex 元数据适配；1000条上限内采用无需密钥的基础分页。"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from papersteward.core.entities import utc_now
from papersteward.sources.contracts import PaperCandidate, QuerySpec, SourceError
from papersteward.sources.http import SourceHTTP

BASE = "https://api.openalex.org"
IDENTITY_PAGE_SIZE = 100


def _literal(value: str) -> str:
    if any(c in value for c in ('"', "\\")) or any(ord(c) < 32 for c in value):
        raise SourceError("openalex", "unsupported_query")
    return f'"{value}"'


def _abstract(index: Any) -> str | None:
    if not index:
        return None
    if not isinstance(index, dict):
        raise ValueError("invalid inverted index")
    words: dict[int, str] = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            raise ValueError("invalid token")
        for pos in positions:
            if type(pos) is not int or not 0 <= pos < 100000 or pos in words:
                raise ValueError("invalid position")
            words[pos] = word
    return " ".join(words[pos] for pos in sorted(words)) or None


def parse_work(work: dict[str, Any], retrieved_at: datetime) -> PaperCandidate:
    try:
        identifier = work.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"https://openalex.org/W\d+", identifier
        ):
            raise ValueError("invalid work id")
        location = work.get("primary_location") or {}
        source = location.get("source") or {}
        oa = (work.get("open_access") or {}).get("is_oa")
        published = work.get("publication_date")
        return PaperCandidate.model_validate(
            {
                "title": work.get("title") or work.get("display_name"),
                "doi": work.get("doi") or None,
                "venue": source.get("display_name") or None,
                "authors": [
                    name
                    for item in work.get("authorships", [])
                    if (name := (item.get("author") or {}).get("display_name"))
                ],
                "published_at": f"{published}T00:00:00Z" if published else None,
                "abstract": _abstract(work.get("abstract_inverted_index")),
                "url": location.get("landing_page_url") or identifier,
                "pdf_url": (work.get("best_oa_location") or {}).get("pdf_url") or None,
                "oa_status": "open" if oa is True else "closed" if oa is False else "unknown",
                "language": work.get("language") or None,
                "document_type": work.get("type") or None,
                "citation_count": work.get("cited_by_count"),
                "provenance": {
                    "source": "openalex",
                    "source_id": identifier.rsplit("/", 1)[1],
                    "url": identifier,
                    "retrieved_at": retrieved_at,
                    "raw_metadata": work,
                },
            }
        )
    except (ValueError, TypeError, AttributeError, ValidationError) as exc:
        raise SourceError("openalex", "invalid_response") from exc


class OpenAlexAdapter:
    source = "openalex"

    def __init__(
        self,
        http: SourceHTTP,
        *,
        now: Callable[[], datetime] = utc_now,
        page_size: int = 100,
        identity_max_pages: int = 3,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在1–100之间")
        if not 1 <= identity_max_pages <= 10:
            raise ValueError("identity_max_pages 必须在1–10之间")
        self.http, self.now, self.page_size = http, now, page_size
        self.identity_max_pages = identity_max_pages

    def _json(self, endpoint: str, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            data = json.loads(self.http.get(BASE + endpoint, params))
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError("invalid response")
            return data
        except (ValueError, UnicodeError) as exc:
            raise SourceError(self.source, "invalid_response") from exc

    def _identity_id(self, endpoint: str, prefix: str, name: str) -> str | None:
        """完整分页核对精确名称；无法证明唯一时保守拒绝而不是猜人/机构。"""
        matches: set[str] = set()
        scanned = 0
        total: int | None = None
        for page in range(1, self.identity_max_pages + 1):
            data = self._json(
                "/" + endpoint,
                {"search": name, "per_page": IDENTITY_PAGE_SIZE, "page": page},
            )
            meta = data.get("meta")
            count = meta.get("count") if isinstance(meta, dict) else None
            if type(count) is not int or count < 0:
                raise SourceError(self.source, "invalid_response")
            total = count
            results = data["results"]
            scanned += len(results)
            for item in results:
                if (
                    isinstance(item, dict)
                    and str(item.get("display_name", "")).casefold() == name.casefold()
                    and re.fullmatch(rf"https://openalex.org/{prefix}\d+", str(item.get("id", "")))
                ):
                    matches.add(str(item["id"]))
            if scanned >= total or len(results) < IDENTITY_PAGE_SIZE:
                break
        if total is not None and scanned >= total:
            if len(matches) == 1:
                return next(iter(matches))
            if not matches:
                return None
        raise SourceError(self.source, "unsupported_query")

    def search(self, query: QuerySpec) -> list[PaperCandidate]:
        operation = " OR " if query.operator == "any" else " AND "
        expression = "(" + operation.join(_literal(term) for term in query.keywords) + ")"
        filters: list[str] = []
        if query.field != "all":
            if len(query.keywords) != 1:
                raise SourceError(self.source, "unsupported_query")
            endpoint, field, prefix = {
                "author": ("authors", "authorships.author.id", "A"),
                "venue": ("sources", "primary_location.source.id", "S"),
                "team": ("institutions", "authorships.institutions.id", "I"),
            }[query.field]
            identifier = self._identity_id(endpoint, prefix, query.keywords[0])
            if identifier is None:
                return []
            filters.append(field + ":" + identifier)
            expression = ""
        for term in query.exclude_keywords:
            expression += " NOT " + _literal(term)
        if query.date_from:
            filters.append("from_publication_date:" + query.date_from.isoformat())
        if query.date_to:
            filters.append("to_publication_date:" + query.date_to.isoformat())
        size = min(self.page_size, query.max_results)
        params: dict[str, str | int] = {
            "per_page": size,
            "page": 1,
            "sort": "publication_date:desc",
        }
        if expression.strip():
            params["search"] = expression.strip()
        if filters:
            params["filter"] = ",".join(filters)
        result: list[PaperCandidate] = []
        seen: set[str] = set()
        while len(result) < query.max_results:
            page = self._json("/works", params)["results"]
            for item in page[: query.max_results - len(result)]:
                candidate = parse_work(item, self.now())
                if candidate.provenance.source_id in seen:
                    raise SourceError(self.source, "invalid_response")
                seen.add(candidate.provenance.source_id)
                result.append(candidate)
            if len(page) < size:
                break
            params["page"] = int(params["page"]) + 1
        return result
