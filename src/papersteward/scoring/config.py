"""评分配置（scoring.yaml）加载与校验。

结构遵循设计方案 §10.5；先做字段/范围校验，再做跨字段权重和、策略下限/上限与
gates 关系校验。错误带 source/line/field，便于 CLI 与配置文件迭代。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from papersteward.core.errors import DomainValidationError

WeightTolerance = 1e-6
Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_TOP_LEVEL_KEY = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:")


class ConfigError(DomainValidationError):
    """评分配置错误；带来源文件与行号。"""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        field: str = "scoring",
        line: int | None = None,
    ) -> None:
        super().__init__(field, message)
        self.source = source
        self.line = line

    def __str__(self) -> str:
        location = f"{self.source}:{self.line}" if self.line is not None else self.source
        return f"{location}: {self.field}: {self.message}"


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HalfLifeDays(_ConfigModel):
    default: int = Field(gt=0)
    fast: int = Field(gt=0)
    slow: int = Field(gt=0)


class StaticWeights(_ConfigModel):
    weight: float = Field(ge=0.0, le=1.0)
    relevance: float = Field(ge=0.0, le=1.0)
    authority: float = Field(ge=0.0, le=1.0)
    extra_value: float = Field(ge=0.0, le=1.0)


class DynamicWeights(_ConfigModel):
    weight: float = Field(ge=0.0, le=1.0)
    freshness: float = Field(ge=0.0, le=1.0)
    hunger: float = Field(ge=0.0, le=1.0)
    half_life_days: HalfLifeDays
    hunger_window_days: int = Field(gt=0)
    prior_strength: float = Field(ge=0.0)


class QuasiDynamicWeights(_ConfigModel):
    weight: float = Field(ge=0.0, le=1.0)
    update_weekday: Weekday
    shrinkage_lambda: float = Field(gt=0.0)
    tag_weight: float = Field(ge=0.0, le=1.0)
    author_weight: float = Field(ge=0.0, le=1.0)
    venue_weight: float = Field(ge=0.0, le=1.0)

    @field_validator("update_weekday", mode="before")
    @classmethod
    def _normalize_weekday(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value


class Gates(_ConfigModel):
    min_relevance_for_push: float = Field(ge=0.0, le=100.0)
    max_daily_push: int = Field(ge=1)
    max_special_push: int = Field(ge=0)
    max_per_primary_tag: int = Field(ge=1)
    deep_cap: int = Field(ge=0)
    skim_cap: int = Field(ge=0)
    relevance_weight_floor: float = Field(ge=0.0, le=1.0)
    static_weight_floor: float = Field(ge=0.0, le=1.0)
    authority_weight_cap: float = Field(ge=0.0, le=1.0)


class Stability(_ConfigModel):
    rank_ema: float = Field(ge=0.0, le=1.0)
    max_daily_delta: float = Field(ge=0.0, le=100.0)


class ScoringConfig(_ConfigModel):
    version: str = Field(min_length=1)
    static: StaticWeights
    dynamic: DynamicWeights
    quasi_dynamic: QuasiDynamicWeights
    gates: Gates
    stability: Stability

    @field_validator("version", mode="before")
    @classmethod
    def _normalize_version(cls, value: object) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError("version 必须是字符串或数字")
        if isinstance(value, int | float):
            text = str(value)
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise ValueError("version 必须是字符串或数字")
        if not text:
            raise ValueError("version 不能为空")
        return text


def _line_for(text: str, field: str) -> int | None:
    top = field.split(".")[0]
    if top == "scoring":
        top = field.split(".")[1] if "." in field else field
    for offset, line in enumerate(text.splitlines()):
        match = _TOP_LEVEL_KEY.match(line)
        if match and match.group("key") == top:
            return offset + 1
    return None


def _raise_cross(message: str, *, source: str, text: str, field: str) -> None:
    raise ConfigError(message, source=source, field=field, line=_line_for(text, field))


def _validate_cross(config: ScoringConfig, *, source: str, text: str) -> None:
    def sum_to_one(values: list[float], field: str, label: str) -> None:
        total = sum(values)
        if abs(total - 1.0) > WeightTolerance:
            _raise_cross(
                f"{label} 权重之和必须为 1.0，实际为 {total:.6f}",
                source=source,
                text=text,
                field=field,
            )

    sum_to_one(
        [config.static.relevance, config.static.authority, config.static.extra_value],
        "scoring.static",
        "static 子权重",
    )
    sum_to_one(
        [config.dynamic.freshness, config.dynamic.hunger],
        "scoring.dynamic",
        "dynamic 子权重",
    )
    sum_to_one(
        [
            config.quasi_dynamic.tag_weight,
            config.quasi_dynamic.author_weight,
            config.quasi_dynamic.venue_weight,
        ],
        "scoring.quasi_dynamic",
        "quasi_dynamic 子权重",
    )
    sum_to_one(
        [config.static.weight, config.dynamic.weight, config.quasi_dynamic.weight],
        "scoring",
        "全局层权重",
    )

    if config.static.relevance < 0.50:
        _raise_cross(
            "static.relevance（wR）必须 ≥ 0.50",
            source=source,
            text=text,
            field="scoring.static.relevance",
        )
    if config.static.weight < 0.60:
        _raise_cross(
            "static.weight（wS）必须 ≥ 0.60",
            source=source,
            text=text,
            field="scoring.static.weight",
        )
    if config.static.authority > 0.20:
        _raise_cross(
            "static.authority（wA）必须 ≤ 0.20",
            source=source,
            text=text,
            field="scoring.static.authority",
        )
    if config.gates.relevance_weight_floor < 0.50:
        _raise_cross(
            "gates.relevance_weight_floor 不能低于 0.50",
            source=source,
            text=text,
            field="scoring.gates.relevance_weight_floor",
        )
    if config.gates.static_weight_floor < 0.60:
        _raise_cross(
            "gates.static_weight_floor 不能低于 0.60",
            source=source,
            text=text,
            field="scoring.gates.static_weight_floor",
        )
    if config.gates.authority_weight_cap > 0.20:
        _raise_cross(
            "gates.authority_weight_cap 不能高于 0.20",
            source=source,
            text=text,
            field="scoring.gates.authority_weight_cap",
        )
    if config.static.relevance < config.gates.relevance_weight_floor:
        _raise_cross(
            "static.relevance 低于 gates.relevance_weight_floor",
            source=source,
            text=text,
            field="scoring.static.relevance",
        )
    if config.static.weight < config.gates.static_weight_floor:
        _raise_cross(
            "static.weight 低于 gates.static_weight_floor",
            source=source,
            text=text,
            field="scoring.static.weight",
        )
    if config.static.authority > config.gates.authority_weight_cap:
        _raise_cross(
            "static.authority 高于 gates.authority_weight_cap",
            source=source,
            text=text,
            field="scoring.static.authority",
        )
    if config.gates.deep_cap + config.gates.skim_cap > config.gates.max_daily_push:
        _raise_cross(
            "deep_cap + skim_cap 不能超过 max_daily_push",
            source=source,
            text=text,
            field="scoring.gates.deep_cap",
        )
    if config.gates.max_per_primary_tag > config.gates.max_daily_push:
        _raise_cross(
            "max_per_primary_tag 不能超过 max_daily_push",
            source=source,
            text=text,
            field="scoring.gates.max_per_primary_tag",
        )
    half_life = config.dynamic.half_life_days
    if not half_life.fast <= half_life.default <= half_life.slow:
        _raise_cross(
            "half_life_days 必须满足 fast ≤ default ≤ slow",
            source=source,
            text=text,
            field="scoring.dynamic.half_life_days",
        )


def validate_scoring_config(config: ScoringConfig, *, source: str = "<memory>") -> ScoringConfig:
    """对已构造的配置执行跨字段校验。"""
    _validate_cross(config, source=source, text="")
    return config


def _validation_error(exc: ValidationError, *, source: str, text: str) -> ConfigError:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ()) if str(part))
    field = f"scoring.{location}" if location else "scoring"
    message = str(error.get("msg", "校验失败"))
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    return ConfigError(message, source=source, field=field, line=_line_for(text, field))


def load_scoring_config_text(text: str, *, source: str = "<memory>") -> ScoringConfig:
    """解析并校验 scoring YAML；失败抛 ``ConfigError``。"""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"YAML 解析失败：{problem}", source=source, line=line) from exc
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是 YAML mapping", source=source, line=1)
    try:
        config = ScoringConfig.model_validate(data)
    except ValidationError as exc:
        raise _validation_error(exc, source=source, text=text) from exc
    _validate_cross(config, source=source, text=text)
    return config


def load_scoring_config_file(path: str | Path) -> ScoringConfig:
    source_path = Path(path)
    return load_scoring_config_text(
        source_path.read_text(encoding="utf-8"), source=str(source_path)
    )


def config_value_json(config: ScoringConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


__all__ = [
    "ConfigError",
    "DynamicWeights",
    "Gates",
    "HalfLifeDays",
    "QuasiDynamicWeights",
    "ScoringConfig",
    "Stability",
    "StaticWeights",
    "config_value_json",
    "load_scoring_config_file",
    "load_scoring_config_text",
    "validate_scoring_config",
]
