"""Semantic Scholar bulk 检索；TLDR 为来源数据，不充当生成摘要。"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from papersteward.core.entities import utc_now
from papersteward.sources.contracts import PaperCandidate, QuerySpec, SourceError
from papersteward.sources.http import SourceHTTP

ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
FIELDS = (
    "title,abstract,authors,venue,publicationDate,externalIds,citationCount,"
    "isOpenAccess,openAccessPdf,publicationTypes,tldr,url"
)


def compile_query(query: QuerySpec) -> str:
    if query.field != "all":
        raise SourceError("semantic_scholar", "unsupported_query")

    def literal(term: str) -> str:
        if any(c in term for c in ('"', "\\")) or any(ord(c) < 32 for c in term):
            raise SourceError("semantic_scholar", "unsupported_query")
        return f'"{term}"'

    operation = " | " if query.operator == "any" else " + "
    result = "(" + operation.join(literal(term) for term in query.keywords) + ")"
    return result + "".join(" -" + literal(term) for term in query.exclude_keywords)


def parse_paper(data: dict[str, Any], retrieved_at: datetime) -> PaperCandidate:
    try:
        identifier = data.get("paperId", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", identifier):
            raise ValueError("invalid paper id")
        external = data.get("externalIds") or {}
        date = data.get("publicationDate")
        url = data.get("url") or f"https://www.semanticscholar.org/paper/{identifier}"
        oa = data.get("isOpenAccess")
        types = data.get("publicationTypes") or []
        return PaperCandidate.model_validate(
            {
                "title": data.get("title"),
                "authors": [item["name"] for item in data.get("authors", []) if item.get("name")],
                "abstract": data.get("abstract") or None,
                "doi": external.get("DOI") or None,
                "arxiv_id": external.get("ArXiv") or None,
                "venue": data.get("venue") or None,
                "published_at": f"{date}T00:00:00Z" if date else None,
                "url": url,
                "pdf_url": (data.get("openAccessPdf") or {}).get("url") or None,
                "oa_status": "open" if oa is True else "closed" if oa is False else "unknown",
                "citation_count": data.get("citationCount"),
                "document_type": types[0] if types else None,
                "provenance": {
                    "source": "semantic_scholar",
                    "source_id": identifier,
                    "url": url,
                    "retrieved_at": retrieved_at,
                    "raw_metadata": data,
                },
            }
        )
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
        raise SourceError("semantic_scholar", "invalid_response") from exc


class SemanticScholarAdapter:
    source = "semantic_scholar"

    def __init__(self, http: SourceHTTP, *, now: Callable[[], datetime] = utc_now) -> None:
        self.http, self.now = http, now

    def search(self, query: QuerySpec) -> list[PaperCandidate]:
        params: dict[str, str | int] = {
            "query": compile_query(query),
            "fields": FIELDS,
            "sort": "publicationDate:desc",
        }
        if query.date_from or query.date_to:
            params["publicationDateOrYear"] = ":".join(
                value.isoformat() if value else "" for value in (query.date_from, query.date_to)
            )
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        result: list[PaperCandidate] = []
        while len(result) < query.max_results:
            try:
                response = json.loads(self.http.get(ENDPOINT, params))
                if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                    raise ValueError("invalid response")
                for item in response["data"][: query.max_results - len(result)]:
                    candidate = parse_paper(item, self.now())
                    if candidate.provenance.source_id in seen_ids:
                        raise ValueError("repeated paper")
                    seen_ids.add(candidate.provenance.source_id)
                    result.append(candidate)
                token = response.get("token")
                if not response["data"] or token is None or len(result) >= query.max_results:
                    break
                if not isinstance(token, str) or not token or token in seen_tokens:
                    raise ValueError("invalid continuation token")
                seen_tokens.add(token)
                params["token"] = token
            except (ValueError, UnicodeError) as exc:
                raise SourceError(self.source, "invalid_response") from exc
        return result
