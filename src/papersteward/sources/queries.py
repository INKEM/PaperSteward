"""从已确认的档案生成确定性查询；不推断关键词，不调用模型。"""

from sqlalchemy.orm import Session

from papersteward.core.entities import ResearchProfile
from papersteward.core.enums import DocumentStatus
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.documents.profile import parse_profile
from papersteward.sources.contracts import QuerySpec
from papersteward.storage.repositories import ProfileRepository


def selected_profile(session: Session) -> ResearchProfile:
    profile = ProfileRepository(session).latest_confirmed()
    if profile is None:
        raise InvariantViolation("onboarding.profile", "缺少已确认的研究档案，请先完成初始化确认")
    _validate_profile(profile)
    return profile


def _validate_profile(profile: ResearchProfile) -> None:
    parsed = parse_profile(profile.content_md, document_id=profile.id)
    if (
        profile.status is not DocumentStatus.CONFIRMED
        or parsed.version != profile.version
        or dict(parsed.parsed_json) != dict(profile.parsed_json)
    ):
        raise InvariantViolation("onboarding.profile", "档案未确认或内容已漂移，请重新确认")


def _terms(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DomainValidationError(field, "必须是字符串列表")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def generate_queries(
    profile: ResearchProfile,
    *,
    max_queries: int = 20,
    max_results: int = 100,
) -> list[QuerySpec]:
    _validate_profile(profile)
    if (
        isinstance(max_queries, bool)
        or not isinstance(max_queries, int)
        or not 1 <= max_queries <= 100
    ):
        raise DomainValidationError("queries.max_queries", "必须是 1–100 的整数")
    data = dict(profile.parsed_json)
    excluded = _terms(data.get("exclude"), "profile.exclude")
    queries: list[QuerySpec] = []

    def add(value: object, field: str = "all") -> None:
        terms = _terms(value, f"profile.search.{field}")
        if terms:
            query = QuerySpec.model_validate(
                {
                    "keywords": terms,
                    "exclude_keywords": excluded,
                    "field": field,
                    "max_results": max_results,
                }
            )
            if query not in queries:
                queries.append(query)

    priority = {"high": 0, "medium": 1, "low": 2}
    for item in sorted(data["subdirections"], key=lambda item: priority[item["priority"]]):
        add(item["keywords"])
    for field in ("methods", "data"):
        add(data.get(field))
    authority = data.get("authority") or {}
    if not isinstance(authority, dict):
        raise DomainValidationError("profile.authority", "必须是对象")
    for key, field in (("authors", "author"), ("venues", "venue"), ("teams", "team")):
        for term in _terms(authority.get(key), f"profile.authority.{key}"):
            add([term], field)
    if not queries:
        raise DomainValidationError("profile.search", "没有可用关键词、方法、数据或关注列表")
    return queries[:max_queries]
