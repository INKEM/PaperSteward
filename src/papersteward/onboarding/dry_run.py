"""试运行输入与纯展示；只校验预选 fixture，不执行排序、评分或发送。"""

import re
from collections import Counter
from datetime import date
from html import escape
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from papersteward.core.enums import ReadMode
from papersteward.core.errors import DomainValidationError
from papersteward.onboarding.calibration import InputModel
from papersteward.scoring.config import ScoringConfig, validate_scoring_config

ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Share = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Issue = Literal["too_many", "too_few", "inaccurate", "wrong_category"]


class RuntimeParameters(InputModel):
    push_time: ClockTime
    timezone: str = Field(min_length=1, max_length=64)
    quiet_start: ClockTime | None
    quiet_end: ClockTime | None
    paused: bool = Field(strict=True)
    target_shares: dict[str, Share] = Field(min_length=1, max_length=8)
    channel: Literal["local_markdown"]
    external_model_data: Literal["disabled", "metadata_abstract"]
    share_notes: Literal[False]

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("需要有效的 IANA 时区") from exc
        return value

    @model_validator(mode="after")
    def coherent_schedule(self) -> "RuntimeParameters":
        start, end = self.quiet_start, self.quiet_end
        if (start is None) != (end is None):
            raise ValueError("安静时段起止必须同时提供或同时关闭")
        if start is not None and end is not None:
            if start == end:
                raise ValueError("安静时段起止不能相同")
            within = (
                start <= self.push_time < end
                if start < end
                else (self.push_time >= start or self.push_time < end)
            )
            if within:
                raise ValueError("推送时间不能落在安静时段内")
        if abs(sum(self.target_shares.values()) - 1) > 1e-6:
            raise ValueError("目标主标签占比之和必须为 1，不会自动归一化")
        return self


class DigestItem(InputModel):
    paper_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    primary_tag: str = Field(min_length=1, max_length=32)
    relevance: float = Field(ge=0, le=100, allow_inf_nan=False)
    read_mode: ReadMode
    summary: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class DryRunRequest(InputModel):
    version: int = Field(ge=1, le=9999, strict=True)
    digest_date: date
    sample_kind: Literal["synthetic"]
    source: str = Field(min_length=1, max_length=128)
    parameters: RuntimeParameters
    scoring: ScoringConfig
    items: list[DigestItem] = Field(min_length=1, max_length=10)


class DryRunFeedback(InputModel):
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_by: str = Field(min_length=1, max_length=128)
    accepted: bool = Field(strict=True)
    issues: list[Issue] = Field(max_length=4)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def coherent_feedback(self) -> "DryRunFeedback":
        if len(set(self.issues)) != len(self.issues):
            raise ValueError("反馈问题不得重复")
        if self.accepted and self.issues:
            raise ValueError("有未解决问题时不能认可日报")
        if not self.accepted and not self.issues and not self.comment:
            raise ValueError("未认可时需填写问题或说明")
        return self


def validate_preview(body: DryRunRequest, codes: set[str]) -> None:
    validate_scoring_config(body.scoring)
    gates = body.scoring.gates
    if set(body.parameters.target_shares) != codes:
        raise DomainValidationError("dry_run.target_shares", "目标占比必须覆盖全部启用主标签")
    if not 5 <= gates.max_daily_push <= 10 or gates.max_special_push > 2:
        raise DomainValidationError("dry_run.quotas", "每日主队列容量须为 5–10，特殊桶最多 2")
    items = body.items
    if len({item.paper_id for item in items}) != len(items):
        raise DomainValidationError("dry_run.items", "日报不能重复文献 ID")
    if any(item.primary_tag not in codes for item in items):
        raise DomainValidationError("dry_run.primary_tag", "日报引用了未知主标签")
    counts = Counter(item.primary_tag for item in items)
    modes = Counter(item.read_mode for item in items)
    if (
        len(items) > gates.max_daily_push
        or max(counts.values()) > gates.max_per_primary_tag
        or modes[ReadMode.DEEP] > gates.deep_cap
        or modes[ReadMode.SKIM] > gates.skim_cap
    ):
        raise DomainValidationError("dry_run.quotas", "预选日报超过每日/标签/精读/粗读配额")
    if any(item.relevance < gates.min_relevance_for_push for item in items):
        raise DomainValidationError("dry_run.relevance", "预选主队列含低于相关度门槛的条目")


def _text(value: str) -> str:
    # 接收的论文标题/摘要/理由只作为文本；防止 HTML、链接或换行注入日报结构。
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", escape(" ".join(value.splitlines())))


def build_report(body: DryRunRequest, binding: dict[str, Any], codes: set[str]) -> dict[str, Any]:
    validate_preview(body, codes)
    p, g, s = body.parameters, body.scoring.gates, body.scoring
    quiet = "关闭" if p.quiet_start is None else f"{p.quiet_start}–{p.quiet_end}"
    lines = [
        f"# 试运行日报 · {body.digest_date}（仅流程验证）",
        "",
        "只展示，不发送；条目与分数来自合成 fixture，未运行真实评分或排序。",
        "",
        f"时间：{p.push_time}（{_text(p.timezone)}）；安静时段：{quiet}；"
        f"暂停：{'是' if p.paused else '否'}。",
        f"每日主队列上限：{g.max_daily_push}；精读：{g.deep_cap}；粗读：{g.skim_cap}；单标签：{g.max_per_primary_tag}。",
        f"特殊桶上限：{g.max_special_push}（本次未展示）；相关度门槛：{g.min_relevance_for_push:g}。",
        f"全局权重：静态 {s.static.weight:g} / 动态 {s.dynamic.weight:g} / "
        f"准动态 {s.quasi_dynamic.weight:g}。",
        "目标占比："
        + "、".join(f"{_text(k)} {v:.1%}" for k, v in sorted(p.target_shares.items()))
        + "。",
        "渠道：本地 Markdown；模型数据范围："
        + ("禁用" if p.external_model_data == "disabled" else "仅元数据与摘要")
        + "；备注不外发。",
        "",
    ]
    for index, item in enumerate(body.items, 1):
        mode = "精读" if item.read_mode is ReadMode.DEEP else "粗读"
        lines += [
            f"## {index}. {_text(item.title)}",
            "",
            f"主标签：{_text(item.primary_tag)}；相关度：{item.relevance:g}；建议：{mode}。",
            f"概括：{_text(item.summary)}",
            f"理由：{_text(item.reason)}",
            "",
        ]
    lines.append("反馈：太多 / 太少 / 不准 / 分类不对。认可本版日报和参数后，再显式确认 G4。")
    return {
        **body.model_dump(mode="json"),
        "binding": binding,
        "display_only": True,
        "sample_notice": "仅流程验证",
        "markdown": "\n".join(lines),
        "tag_counts": dict(Counter(item.primary_tag for item in body.items)),
    }
