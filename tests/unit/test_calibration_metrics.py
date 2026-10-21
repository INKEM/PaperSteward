import json
from copy import deepcopy
from math import sqrt
from pathlib import Path

import pytest
from pydantic import ValidationError

from papersteward.core.errors import DomainValidationError
from papersteward.onboarding.calibration import (
    AgentBatch,
    CalibrationPaper,
    UserBatch,
    calibration_report,
    spearman,
    validate_cohort,
    validate_labels,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/calibration/basic.json"
CODES = {f"T{i:02}" for i in range(1, 6)}


def inputs():
    body = json.loads(FIXTURE.read_text())
    return (
        [CalibrationPaper.model_validate(p) for p in body["papers"]],
        AgentBatch.model_validate(body["agent_batch"]).labels,
        UserBatch.model_validate(body["user_batch"]).labels,
    )


def test_fixture_metrics_golden_and_annotation_order():
    papers, agent, user = inputs()
    validate_cohort(papers, CODES, set())
    validate_labels(papers, agent, CODES)
    validate_labels(papers, user, CODES)
    report = calibration_report(papers, agent, user)
    assert report["spearman"] == pytest.approx(sqrt(125 / 133))
    assert report["top5_overlap"] == 5
    assert report["metrics_passed"]
    assert report["primary_tag_agreement"] == 1
    assert report["read_mode_agreement"] == 0.95
    assert report == calibration_report(papers[::-1], agent[::-1], user[5:] + user[:5])
    assert report["annotations"][0]["agent"] == agent[0].model_dump(mode="json")
    assert report["annotations"][-1]["user"] == user[-1].model_dump(mode="json")


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ([1, 2, 3], [1, 2, 3], 1),
        ([1, 2, 3], [3, 2, 1], -1),
        ([1, 1, 2], [2, 2, 1], -1),
        ([1, 1, 1], [1, 2, 3], None),
        ([1, 2, 3], [0, 0, 0], None),
    ],
)
def test_spearman_ties_and_undefined(left, right, expected):
    value = spearman(left, right)
    assert value is None if expected is None else value == pytest.approx(expected)


def test_constant_and_inverse_labels_fail_without_nan():
    papers, agent, user = inputs()
    for a in agent:
        a.relevance = 90
    report = calibration_report(papers, agent, user)
    assert report["spearman"] is None and not report["metrics_passed"]
    assert report["agent_top5"] == [f"cal-{i:02}" for i in range(1, 6)]
    for i, a in enumerate(agent):
        a.relevance = i * 5
    agent[12].primary_tag = "T05"
    report = calibration_report(papers, agent, user)
    assert report["spearman"] < -0.9 and report["top5_overlap"] == 0
    assert report["boundary_disputes"] == ["cal-13"]
    assert {"profile", "rubric", "taxonomy"} <= {x["target"] for x in report["recommendations"]}
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "coverage", "balance", "boundary", "negative", "irrelevant_negative", "seed"],
)
def test_cohort_structural_requirements(mutation):
    papers, _, _ = inputs()
    seeds = set()
    if mutation == "duplicate":
        papers[1].paper_id = papers[0].paper_id
    if mutation == "coverage":
        for p in papers:
            if p.reference_tag == "T05":
                p.reference_tag = "T01"
    if mutation == "balance":
        papers[0].stratum = "irrelevant"
        papers[1].stratum = "irrelevant"
    if mutation == "boundary":
        papers[12].boundary_tags = ["T03"]
    if mutation == "negative":
        papers[10].preference_negative = False
    if mutation == "irrelevant_negative":
        papers[19].preference_negative = True
    if mutation == "seed":
        seeds.add(papers[0].paper_id)
    with pytest.raises(DomainValidationError):
        validate_cohort(papers, CODES, seeds)


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "missing", "tag"])
def test_labels_must_exactly_cover_cohort(mutation):
    papers, agent, user = inputs()
    broken = deepcopy(agent)
    if mutation == "duplicate":
        broken[1].paper_id = broken[0].paper_id
    if mutation == "unknown":
        broken[1].paper_id = "unknown"
    if mutation == "missing":
        broken.pop()
    if mutation == "tag":
        broken[0].primary_tag = "T99"
    with pytest.raises(DomainValidationError):
        validate_labels(papers, broken, CODES)
    if mutation != "tag":
        with pytest.raises(DomainValidationError):
            calibration_report(papers, broken, user)


@pytest.mark.parametrize("value", [-1, 4, True, 1.5, "2"])
def test_user_score_requires_integer_zero_to_three(value):
    body = json.loads(FIXTURE.read_text())["user_batch"]
    body["labels"][0]["relevance"] = value
    with pytest.raises(ValidationError):
        UserBatch.model_validate(body)


@pytest.mark.parametrize("left,right", [([], []), ([1], [1]), ([1, 2], [1])])
def test_invalid_spearman_inputs(left, right):
    with pytest.raises(DomainValidationError):
        spearman(left, right)
