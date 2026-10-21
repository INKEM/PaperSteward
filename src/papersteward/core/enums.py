"""领域枚举。

枚举的字符串值是持久化与事件契约的一部分：只允许追加，不允许重命名既有值。
新增值需要同步迁移、JSON schema 与阶段文档。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class DocumentStatus(StrEnum):
    """Profile / Taxonomy 文档的生命周期。"""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    ARCHIVED = "archived"


class PaperStatus(StrEnum):
    """文献流水线状态；迁移规则见 ``core.state_machine``。"""

    DISCOVERED = "discovered"
    DEDUPED = "deduped"
    REJECTED = "rejected"
    SCREENED = "screened"
    NEEDS_REVIEW = "needs_review"
    ENRICHED = "enriched"
    SUMMARIZED = "summarized"
    CLASSIFIED = "classified"
    SHELVED = "shelved"
    SCORED = "scored"
    QUEUED = "queued"
    SNOOZED = "snoozed"
    PUSHED = "pushed"
    READING = "reading"
    DONE = "done"
    ARCHIVED = "archived"


class TagLevel(StrEnum):
    """标签在分类法中的层级。"""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class TagRole(StrEnum):
    """某条文献上的标签角色；每篇恰好一条 ``PRIMARY``。"""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class BucketType(StrEnum):
    """主题桶与三个特殊桶。"""

    TOPIC = "topic"
    SPECIAL = "special"


class BucketRole(StrEnum):
    """文献与桶的关系；每篇恰好一条 ``FILING``。"""

    FILING = "filing"
    MEMBERSHIP = "membership"


class SpecialBucketKind(StrEnum):
    """特殊桶类型；归属优先级由 ``SPECIAL_BUCKET_PRIORITY`` 定义。"""

    REVIEW = "review"
    DISTANT = "distant"
    CLASSIC = "classic"


# 数值越大优先级越高：跨层综述 > 经典/奠基 > 远邻领域。
SPECIAL_BUCKET_PRIORITY: Final[Mapping[SpecialBucketKind, int]] = MappingProxyType(
    {
        SpecialBucketKind.REVIEW: 3,
        SpecialBucketKind.CLASSIC: 2,
        SpecialBucketKind.DISTANT: 1,
    }
)


class AssignedBy(StrEnum):
    """标签、桶与配置的写入来源。"""

    AGENT = "agent"
    USER = "user"
    SYSTEM = "system"


class OaStatus(StrEnum):
    """开放获取状态；未知时不惩罚文献。"""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class SummaryValidationStatus(StrEnum):
    """摘要校验状态；只有 ``PASSED`` 可以进入推送。"""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class ReadMode(StrEnum):
    """阅读建议/实际阅读方式。"""

    DEEP = "deep"
    SKIM = "skim"


class ScoreTier(StrEnum):
    """特殊桶内部档位；主题桶使用 P 排序，不使用档位。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RunStatus(StrEnum):
    """运行记录状态。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    """追加写事件类型。

    文献状态事件与 ``PaperStatus`` 一一对应；用户操作与文档确认使用独立事件，
    避免把“跳过/撤销”等未改变状态的动作伪装成状态迁移。
    """

    PAPER_DISCOVERED = "paper.discovered"
    PAPER_DEDUPED = "paper.deduped"
    PAPER_REJECTED = "paper.rejected"
    PAPER_SCREENED = "paper.screened"
    PAPER_NEEDS_REVIEW = "paper.needs_review"
    PAPER_ENRICHED = "paper.enriched"
    PAPER_SUMMARIZED = "paper.summarized"
    PAPER_CLASSIFIED = "paper.classified"
    PAPER_SHELVED = "paper.shelved"
    PAPER_SCORED = "paper.scored"
    PAPER_QUEUED = "paper.queued"
    PAPER_SNOOZED = "paper.snoozed"
    PAPER_PUSHED = "paper.pushed"
    PAPER_READING = "paper.reading"
    PAPER_DONE = "paper.done"
    PAPER_ARCHIVED = "paper.archived"
    PAPER_SKIPPED = "paper.skipped"
    PAPER_RATED = "paper.rated"
    PAPER_NOTE_ADDED = "paper.note_added"
    PAPER_BUCKET_MOVED = "paper.bucket_moved"
    PAPER_TAG_EDITED = "paper.tag_edited"
    PAPER_RESTORED = "paper.restored"
    PAPER_REVERTED = "paper.reverted"
    PAPER_METADATA_UPDATED = "paper.metadata_updated"
    PROFILE_CONFIRMED = "profile.confirmed"
    PROFILE_ARCHIVED = "profile.archived"
    TAXONOMY_CONFIRMED = "taxonomy.confirmed"
    TAXONOMY_ARCHIVED = "taxonomy.archived"
    CONFIG_UPDATED = "config.updated"
    ONBOARDING_STARTED = "onboarding.started"
    ONBOARDING_G1_CONFIRMED = "onboarding.g1.confirmed"
    ONBOARDING_G2_CONFIRMED = "onboarding.g2.confirmed"
    ONBOARDING_G3_CONFIRMED = "onboarding.g3.confirmed"
    ONBOARDING_G4_CONFIRMED = "onboarding.g4.confirmed"
    ONBOARDING_TAXONOMY_DRAFTED = "onboarding.taxonomy.drafted"
    ONBOARDING_DRY_RUN_CREATED = "onboarding.dry_run.created"
    ONBOARDING_DRY_RUN_FEEDBACK = "onboarding.dry_run.feedback"
    ONBOARDING_SEEDS_IMPORTED = "onboarding.seeds.imported"
    ONBOARDING_CALIBRATION_CREATED = "onboarding.calibration.created"
    ONBOARDING_AGENT_LABELED = "onboarding.calibration.agent_labeled"
    ONBOARDING_USER_LABELED = "onboarding.calibration.user_labeled"
    ONBOARDING_CALIBRATION_REPORTED = "onboarding.calibration.reported"


class RecalculationTrigger(StrEnum):
    """允许重算冻结静态分的原因；没有原因或未经确认不得重算。"""

    PROFILE_CHANGED = "profile_changed"
    TAXONOMY_CHANGED = "taxonomy_changed"
    CONFIG_CHANGED = "config_changed"
    METADATA_CORRECTED = "metadata_corrected"


__all__ = [
    "AssignedBy",
    "BucketRole",
    "BucketType",
    "DocumentStatus",
    "EventType",
    "OaStatus",
    "PaperStatus",
    "ReadMode",
    "RecalculationTrigger",
    "RunStatus",
    "SPECIAL_BUCKET_PRIORITY",
    "ScoreTier",
    "SpecialBucketKind",
    "SummaryValidationStatus",
    "TagLevel",
    "TagRole",
]
