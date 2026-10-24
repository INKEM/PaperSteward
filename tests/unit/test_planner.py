"""检索规划器的钳制、兜底与 prompt 渲染契约。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from papersteward.core.entities import ResearchProfile
from papersteward.core.enums import DocumentStatus
from papersteward.documents.profile import parse_profile
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.llm.prompts import QUERY_PLANNING_V1, default_registry
from papersteward.schemas.models import PlannedQueryOutput, QueryPlanningOutput
from papersteward.sources.contracts import QuerySpec
from papersteward.sources.planner import PlannedQuery, QueryPlan, _clamp_queries, _fallback_plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)
TODAY = date(2026, 10, 23)
PLANNING = json.loads((FIXTURES / "llm/query_planning.valid.json").read_text())


def _profile() -> ResearchProfile:
    text = (FIXTURES / "profiles/basic.md").read_text(encoding="utf-8")
    profile = parse_profile(text, source="basic.md", document_id="demo-profile")
    return replace(profile, status=DocumentStatus.CONFIRMED, confirmed_at=NOW)


def test_query_planning_prompt_registered_and_renders() -> None:
    registry = default_registry()
    assert registry.get("query_planning").version == "query_planning/v1"
    assert QUERY_PLANNING_V1.schema == "query_planning"
    rendered = QUERY_PLANNING_V1.render(
        {
            "profile": "定位：检索",
            "history": "（近 8 周没有已读评分记录）",
            "coverage": "（队列为空）",
            "focus": "评测方向",
            "round_note": "这是本轮挖掘的第 1 轮规划。",
            "today": TODAY.isoformat(),
            "date_from": "2025-10-23",
            "date_to": TODAY.isoformat(),
        }
    )
    # 检索语法知识注入：模型据此构造合法的来源检索式。
    assert "检索语法要点" in rendered
    assert "评测方向" in rendered
    assert "ANDNOT" in rendered and "ti:" in rendered


def test_gateway_plans_with_query_planning_schema() -> None:
    gateway = LLMGateway(
        FakeLLM([json.dumps(PLANNING, ensure_ascii=False)], model_id="fake-llm"),
        clock=lambda: NOW,
    )
    result = gateway.complete_json(
        "query_planning",
        {
            "profile": "定位：检索",
            "history": "h",
            "coverage": "c",
            "focus": "f",
            "round_note": "n",
            "today": TODAY.isoformat(),
            "date_from": "2025-10-23",
            "date_to": TODAY.isoformat(),
        },
    )
    assert isinstance(result.data, QueryPlanningOutput)
    assert result.data.queries[0].keywords == ["long-document retrieval"]


def test_clamp_queries_merges_excludes_clamps_and_dedupes() -> None:
    output = QueryPlanningOutput(
        queries=[
            PlannedQueryOutput(
                keywords=["retrieval"],
                exclude_keywords=["survey"],
                max_results=30,
                rationale="宽检索",
            ),
            PlannedQueryOutput(keywords=["  "], rationale="空关键词应被剔除"),
            PlannedQueryOutput(
                keywords=["retrieval"],
                exclude_keywords=["survey"],
                rationale="重复应被剔除",
            ),
            PlannedQueryOutput(
                keywords=["RAG"],
                operator="all",
                date_from=date(2026, 1, 1),
                max_results=5,
                rationale="窄检索",
            ),
        ],
        strategy_note="互补检索",
    )
    planned = _clamp_queries(output, profile=_profile(), today=TODAY, max_queries=6)
    assert [p.spec.keywords for p in planned] == [("retrieval",), ("RAG",)]
    first = planned[0].spec
    assert first.max_results == 30
    assert "survey" in first.exclude_keywords
    assert first.date_to == TODAY
    assert first.date_from == TODAY.replace(year=2025)  # 默认近一年窗口
    assert planned[1].spec.operator == "all"
    assert planned[1].spec.date_from == date(2026, 1, 1)  # 显式日期保留
    assert [p.rationale for p in planned] == ["宽检索", "窄检索"]


def test_clamp_queries_respects_max_queries() -> None:
    output = QueryPlanningOutput(
        queries=[PlannedQueryOutput(keywords=[f"kw-{index}"], rationale="r") for index in range(5)],
        strategy_note="多条",
    )
    planned = _clamp_queries(output, profile=_profile(), today=TODAY, max_queries=2)
    assert len(planned) == 2


def test_fallback_plan_uses_profile_expansion_without_model() -> None:
    plan = _fallback_plan(_profile(), reason="模型调用失败", round_index=2, max_queries=4)
    assert plan.origin == "profile"
    assert plan.fallback_reason == "模型调用失败"
    assert plan.round_index == 2
    assert plan.queries  # 档案确定性展开至少一条
    assert all("兜底" in query.rationale for query in plan.queries)


def test_query_plan_as_json_and_keyword_digest() -> None:
    plan = QueryPlan(
        queries=(
            PlannedQuery(
                spec=QuerySpec(keywords=("retrieval",), max_results=10),
                rationale="主检索式",
            ),
            PlannedQuery(
                spec=QuerySpec(keywords=("reranking", "rag"), max_results=10),
                rationale="次检索式",
            ),
        ),
        strategy_note="覆盖主方向",
        origin="agent",
        round_index=1,
    )
    payload = plan.as_json()
    assert payload["origin"] == "agent" and payload["fallback_reason"] is None
    assert payload["round"] == 1
    assert payload["queries"][0]["keywords"] == ["retrieval"]
    assert payload["queries"][0]["rationale"] == "主检索式"
    assert plan.keyword_digest() == "retrieval、reranking、rag"
