import json
from datetime import date
from pathlib import Path

from papersteward.scoring.config import load_scoring_config_file
from papersteward.scoring.contracts import Exclusion, QueueItem, ScoreExplanation
from papersteward.scoring.dynamic import Completion, compute_dynamic
from papersteward.scoring.priority import RankConfig, component_priority, compute_priority

GOLDEN = json.loads(Path("tests/fixtures/scoring/priority_golden.json").read_text())
CONFIG = load_scoring_config_file("tests/fixtures/configs/scoring.yaml")


def test_design_golden_and_intermediate_rounding():
    example = GOLDEN["component_example"]
    assert component_priority(example["scores"], example["weights"]) == example["P"] == 75.1
    layers = GOLDEN["frozen_layers"]
    score = compute_priority(
        layers["S"], layers["D"], layers["U"], CONFIG, previous_rank=layers["previous_rank"]
    )
    assert score.priority == layers["P"] and score.rank_score == layers["rank_score"]
    dynamic = GOLDEN["dynamic"]
    day = date.fromisoformat(dynamic["as_of"])
    result = compute_dynamic(
        primary_tag="T01",
        published_on=date.fromisoformat(dynamic["published_on"]),
        as_of=day,
        shares={"T01": 0.2, "T02": 0.8},
        completions=[Completion("T02", day)] * 10,
        config=CONFIG,
    )
    assert (result.freshness, result.hunger, result.dynamic_score) == (
        dynamic["F"],
        dynamic["H"],
        dynamic["D"],
    )


def test_p5_contract_freeze():
    assert RankConfig().tie_break == ("relevance", "published_at", "authority", "title")
    assert set(QueueItem.model_fields) == {
        "paper_id",
        "title",
        "primary_tag",
        "summary_100",
        "published_on",
        "score_date",
        "priority",
        "rank_score",
        "explanation",
        "excluded_from_priority",
    }
    assert set(ScoreExplanation.model_fields) == {
        "paper_id",
        "score_date",
        "config_version",
        "profile_version",
        "taxonomy_version",
        "static_input_hash",
        "primary_tag",
        "components",
        "layer_weights",
        "scores",
        "dynamic",
        "quasi",
        "stability",
        "evidence",
        "reasons",
        "binding",
        "formula",
    }
    assert "priority" not in Exclusion.model_fields and "rank_score" not in Exclusion.model_fields
