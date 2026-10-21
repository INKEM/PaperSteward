"""校准输入与确定性指标；不调用数据库、网络或 LLM。"""

import hashlib
import json
from collections import Counter
from math import sqrt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from papersteward.core.enums import ReadMode
from papersteward.core.errors import DomainValidationError


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CalibrationPaper(InputModel):
    paper_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    abstract: str | None = Field(default=None, min_length=1, max_length=20000)
    reference_tag: str = Field(min_length=1, max_length=32)
    stratum: Literal["relevant", "boundary", "irrelevant"]
    boundary_tags: list[str] = Field(default_factory=list, max_length=8)
    preference_negative: bool = False


class AgentLabel(InputModel):
    paper_id: str = Field(min_length=1, max_length=64)
    relevance: float = Field(ge=0, le=100, allow_inf_nan=False)
    primary_tag: str | None
    read_mode: ReadMode
    reason: str = Field(min_length=1, max_length=2000)


class UserLabel(InputModel):
    paper_id: str = Field(min_length=1, max_length=64)
    relevance: int = Field(ge=0, le=3, strict=True)
    willing_to_read: bool = Field(strict=True)
    primary_tag: str | None
    read_mode: ReadMode


def report_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def validate_cohort(papers: list[CalibrationPaper], codes: set[str], seed_ids: set[str]) -> None:
    identifiers = [paper.paper_id for paper in papers]
    if not 15 <= len(papers) <= 20 or len(set(identifiers)) != len(papers):
        raise DomainValidationError("calibration.papers", "需要 15–20 个不同的留出样本")
    if seed_ids.intersection(identifiers):
        raise DomainValidationError("calibration.papers", "校准集不能复用已标注的 G2 种子")
    if {paper.reference_tag for paper in papers} != codes:
        raise DomainValidationError("calibration.reference_tag", "校准集必须覆盖全部启用主标签")
    counts: Counter[str] = Counter(paper.stratum for paper in papers)
    if any(
        abs(counts[group] - share * len(papers)) > 1.000001
        for group, share in (("relevant", 0.6), ("boundary", 0.2), ("irrelevant", 0.2))
    ):
        raise DomainValidationError("calibration.stratum", "相关/边界/无关样本应约为 60/20/20")
    if counts["boundary"] < 3:
        raise DomainValidationError("calibration.boundary_tags", "至少需要 3 个跨标签边界案例")
    for paper in papers:
        if (
            set(paper.boundary_tags) - codes
            or len(set(paper.boundary_tags)) != len(paper.boundary_tags)
            or (
                paper.stratum == "boundary"
                and (len(paper.boundary_tags) < 2 or paper.reference_tag not in paper.boundary_tags)
            )
        ):
            raise DomainValidationError(
                "calibration.boundary_tags", "边界需至少两个不同的有效标签并包含参考标签"
            )
        if paper.preference_negative and paper.stratum == "irrelevant":
            raise DomainValidationError(
                "calibration.preference_negative", "偏好负例必须是看似相关/边界样本"
            )
    if not 2 <= sum(paper.preference_negative for paper in papers) <= 3:
        raise DomainValidationError(
            "calibration.preference_negative", "需要 2–3 个看似相关的偏好负例"
        )


def validate_labels(
    papers: list[CalibrationPaper], labels: list[AgentLabel] | list[UserLabel], codes: set[str]
) -> None:
    identifiers = [label.paper_id for label in labels]
    if len(set(identifiers)) != len(identifiers) or set(identifiers) != {
        paper.paper_id for paper in papers
    }:
        raise DomainValidationError(
            "calibration.labels", "必须完整标注本轮全部样本，不能重复或添加未知 ID"
        )
    if any(label.primary_tag is not None and label.primary_tag not in codes for label in labels):
        raise DomainValidationError("calibration.primary_tag", "标注引用了未知主标签")


def _ranks(values: list[float]) -> list[float]:
    groups: dict[float, list[int]] = {}
    for index, value in enumerate(values):
        groups.setdefault(value, []).append(index)
    ranks = [0.0] * len(values)
    position = 1
    for value in sorted(groups):
        indices = groups[value]
        average = position + (len(indices) - 1) / 2
        for index in indices:
            ranks[index] = average
        position += len(indices)
    return ranks


def spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        raise DomainValidationError("calibration.metrics", "相关系数需要等长且至少两个样本")
    x, y = _ranks(left), _ranks(right)
    mx, my = sum(x) / len(x), sum(y) / len(y)
    denominator = sqrt(sum((v - mx) ** 2 for v in x) * sum((v - my) ** 2 for v in y))
    if denominator == 0:
        return None
    return max(
        -1.0, min(1.0, sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / denominator)
    )


def calibration_report(
    papers: list[CalibrationPaper], agent: list[AgentLabel], user: list[UserLabel]
) -> dict[str, Any]:
    agents, users = (
        {label.paper_id: label for label in agent},
        {label.paper_id: label for label in user},
    )
    identifiers = sorted(paper.paper_id for paper in papers)
    if (
        len(agents) != len(agent)
        or len(users) != len(user)
        or set(agents) != set(identifiers)
        or set(users) != set(identifiers)
    ):
        raise DomainValidationError("calibration.labels", "双方标注必须与样本逐一匹配")
    rho = spearman(
        [agents[key].relevance for key in identifiers],
        [float(users[key].relevance) for key in identifiers],
    )
    agent_top = sorted(identifiers, key=lambda key: (-agents[key].relevance, key))[:5]
    user_top = sorted(identifiers, key=lambda key: (-users[key].relevance, key))[:5]
    overlap = len(set(agent_top) & set(user_top))
    disputes: list[dict[str, Any]] = []
    for paper in sorted(papers, key=lambda item: item.paper_id):
        a, u = agents[paper.paper_id], users[paper.paper_id]
        targets = []
        if a.relevance >= 55 and (not u.willing_to_read or u.relevance <= 1):
            targets.append("profile")
        elif abs(a.relevance / 100 * 3 - u.relevance) >= 1:
            targets.append("rubric")
        if a.primary_tag != u.primary_tag:
            targets.append("taxonomy")
        if a.read_mode != u.read_mode:
            targets.append("reading")
        if targets:
            disputes.append(
                {
                    "paper_id": paper.paper_id,
                    "checks": targets,
                    "boundary": paper.stratum == "boundary",
                    "agent": a.model_dump(mode="json"),
                    "user": u.model_dump(mode="json"),
                }
            )
    messages = {
        "profile": "优先复核核心问题、纳入/排除标准与子方向目标；不要先调权重",
        "rubric": "复核相关度评分锚点与理由；不要用权重修复理解差异",
        "taxonomy": "复核主标签定义与边界案例",
        "reading": "核对愿读与精读/粗读的口径",
    }
    return {
        "sample_count": len(papers),
        "cohort": [
            paper.model_dump(mode="json")
            for paper in sorted(papers, key=lambda item: item.paper_id)
        ],
        "composition": dict(Counter(paper.stratum for paper in papers)),
        "spearman": rho,
        "spearman_defined": rho is not None,
        "top5_overlap": overlap,
        "agent_top5": agent_top,
        "user_top5": user_top,
        "tie_policy": "分数降序，paper_id 升序；Spearman 使用平均并列秩",
        "primary_tag_agreement": sum(
            agents[key].primary_tag == users[key].primary_tag for key in identifiers
        )
        / len(papers),
        "read_mode_agreement": sum(
            agents[key].read_mode == users[key].read_mode for key in identifiers
        )
        / len(papers),
        "boundary_disputes": [
            item["paper_id"]
            for item in disputes
            if item["boundary"] and "taxonomy" in item["checks"]
        ],
        "boundary_dispute_count": sum(
            item["boundary"] and "taxonomy" in item["checks"] for item in disputes
        ),
        "metrics_passed": rho is not None and rho >= 0.6 and overlap >= 3,
        "disputes": disputes,
        "recommendations": [
            {
                "target": target,
                "paper_ids": [item["paper_id"] for item in disputes if target in item["checks"]],
                "message": message,
            }
            for target, message in messages.items()
            if any(target in item["checks"] for item in disputes)
        ],
        "diagnosis_notice": "基于标注差异的复核建议，不是已证实的根因；不修改任何配置",
        "annotations": [
            {
                "paper_id": key,
                "agent": agents[key].model_dump(mode="json"),
                "user": users[key].model_dump(mode="json"),
            }
            for key in identifiers
        ],
    }


class AgentBatch(InputModel):
    source: str = Field(min_length=1, max_length=128)
    labels: list[AgentLabel] = Field(min_length=15, max_length=20)


class UserBatch(InputModel):
    submitted_by: str = Field(min_length=1, max_length=128)
    labels: list[UserLabel] = Field(min_length=15, max_length=20)
