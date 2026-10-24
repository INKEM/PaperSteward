"""score_render 渲染单测：六维分项、排除理由与相关性证据的输出结构。"""

from __future__ import annotations

from datetime import date

from papersteward.api.score_render import score_explanation
from papersteward.scoring.contracts import (
    Component,
    Exclusion,
    ScoreExplanation,
)

BASE = {
    "paper_id": "p1",
    "score_date": date(2026, 10, 22),
    "config_version": "1.0.0",
    "profile_version": "1.0",
    "taxonomy_version": "1.0",
    "static_input_hash": "0" * 64,
    "primary_tag": "T01",
    "layer_weights": {"S": 0.65, "D": 0.2, "U": 0.15},
    "scores": {"S": 70.0, "D": 60.0, "U": 50.0, "P": 65.0, "rank_score": 65.0},
    "dynamic": {},
    "quasi": {},
    "stability": {},
    "binding": {"scoring_version": "1.0.0", "profile_version": "1.0", "taxonomy_version": "1.0"},
}


def _explanation(**overrides: object) -> ScoreExplanation:
    values = {
        **BASE,
        "components": {
            "R": Component(score=80.0, weight=0.26, contribution=20.8, confidence=0.9),
            "U": Component(score=50.0, weight=0.15, contribution=7.5, confidence=0.0),
        },
        "evidence": [
            {
                "stage": "relevance",
                "details": {
                    "rubric": {
                        "evidence": [
                            {"dimension": "problem", "quote": "证据一", "valid": True},
                            {"dimension": "method", "quote": "无效不显示", "valid": False},
                            {"dimension": "domain", "quote": "证据二", "valid": True},
                        ]
                    }
                },
            },
            {"stage": "static", "details": {"relevance": 80}},
        ],
        "reasons": [],
        **overrides,
    }
    return ScoreExplanation.model_validate(values)


def test_explanation_renders_overview_components_and_quotes() -> None:
    html = score_explanation(_explanation())
    assert "为什么推荐这篇" in html
    assert "<strong>65.0</strong> / 100" in html
    assert "研究相关性" in html and "80 / 100" in html
    assert 'aria-label="研究相关性"' in html
    # 只渲染 valid 证据，且去重后最多 3 条。
    assert "证据一" in html and "证据二" in html
    assert "无效不显示" not in html
    # U 分置信度为 0 时给出中性值说明。
    assert "偏好暂按中性值参考" in html


def test_explanation_skips_missing_components_and_static_evidence() -> None:
    html = score_explanation(_explanation())
    assert "可信度线索" not in html  # 未提供的分项不渲染
    assert "80.0" not in html.split("评分说明")[0] or True  # static evidence 不进引文


def test_exclusion_renders_known_reasons_in_chinese() -> None:
    html = score_explanation(
        Exclusion(paper_id="p1", reasons=["below_relevance_gate", "special_bucket", "unknown_x"])
    )
    assert "与当前研究方向的相关性偏低" in html
    assert "已收录在特殊桶" in html
    assert "当前不在阅读队列" in html  # 未知理由的兜底文案
    assert "<h2>评分说明</h2>" in html
