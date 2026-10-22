"""跨实体不变量。

设计文档 §3.3 的不变量在此落地为可复用函数，供后续仓储（P1-S2）、文档确认
（P1-S3）与流水线（P3 起）调用：

1. 每篇文献恰好一个主标签，且属于当前激活分类法；
2. 每篇文献恰好一个归属桶；
3. 静态分按版本冻结，重算必须有确认过的原因；
4. 评分快照追加写；
5. 归档快照不可变；
6. 事件追加写；
7. 低置信度不得直达推送；
8. Agent 产物溯源（实体层校验）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from papersteward.core.entities import (
    ArchiveSnapshot,
    Bucket,
    Event,
    PaperBucket,
    PaperTag,
    ScoreSnapshot,
    StaticScoreSnapshot,
    Summary,
    Taxonomy,
)
from papersteward.core.enums import (
    SPECIAL_BUCKET_PRIORITY,
    BucketRole,
    BucketType,
    DocumentStatus,
    PaperStatus,
    RecalculationTrigger,
    SpecialBucketKind,
    SummaryValidationStatus,
    TagLevel,
    TagRole,
)
from papersteward.core.errors import InvariantViolation

DEFAULT_RELEVANCE_THRESHOLD = 55.0
DEFAULT_PRIMARY_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_CLASSIFICATION_MARGIN = 0.1
MAX_SECONDARY_TAGS = 5
MIN_PRIMARY_TAGS = 5
MAX_PRIMARY_TAGS = 9
_CATCH_ALL_NAMES = frozenset({"其他", "其它", "other", "others", "misc", "miscellaneous", "待确认"})


def _ensure_same_paper(tags: Sequence[PaperTag], paper_id: str | None) -> str | None:
    resolved = paper_id or (tags[0].paper_id if tags else None)
    if resolved is not None:
        for tag in tags:
            if tag.paper_id != resolved:
                raise InvariantViolation(
                    "paper_tag.paper_id", f"标签属于 {tag.paper_id}，不属于 {resolved}"
                )
    return resolved


def ensure_single_primary_tag(tags: Sequence[PaperTag], *, paper_id: str | None = None) -> PaperTag:
    """每篇文献必须恰好一条主标签。"""
    resolved = _ensure_same_paper(tags, paper_id)
    primary = tuple(tag for tag in tags if tag.role is TagRole.PRIMARY)
    if len(primary) != 1:
        subject = resolved or "文献"
        raise InvariantViolation(
            "paper_tag",
            f"{subject}必须恰好有一个主标签，实际 {len(primary)} 个",
        )
    return primary[0]


def ensure_secondary_tag_limit(
    tags: Sequence[PaperTag],
    *,
    paper_id: str | None = None,
    limit: int = MAX_SECONDARY_TAGS,
) -> tuple[PaperTag, ...]:
    """副标签 0–5 条，且同一篇内代码不重复。"""
    _ensure_same_paper(tags, paper_id)
    secondary = tuple(tag for tag in tags if tag.role is TagRole.SECONDARY)
    if len(secondary) > limit:
        raise InvariantViolation(
            "paper_tag.secondary",
            f"副标签最多 {limit} 个，实际 {len(secondary)} 个",
        )
    codes = [tag.tag_code for tag in secondary]
    if len(codes) != len(set(codes)):
        raise InvariantViolation("paper_tag.secondary", "副标签代码不能重复")
    return secondary


def ensure_tags_match_taxonomy(
    tags: Sequence[PaperTag],
    taxonomy: Taxonomy,
    *,
    paper_id: str | None = None,
    require_active: bool = True,
) -> PaperTag:
    """主标签必须存在于分类法、处于激活状态，且所有分配引用同一分类法版本。"""
    primary = ensure_single_primary_tag(tags, paper_id=paper_id)
    if require_active and taxonomy.status is not DocumentStatus.CONFIRMED:
        raise InvariantViolation("taxonomy.status", "新分配只能使用已确认的分类法")
    by_code = {tag.code: tag for tag in taxonomy.tags}
    for assignment in tags:
        if assignment.taxonomy_version != taxonomy.version:
            raise InvariantViolation(
                "paper_tag.taxonomy_version",
                f"标签引用 {assignment.taxonomy_version}，当前分类法为 {taxonomy.version}",
            )
        catalog_tag = by_code.get(assignment.tag_code)
        if catalog_tag is None:
            if assignment.role is TagRole.PRIMARY:
                raise InvariantViolation(
                    "paper_tag.tag_code",
                    f"主标签 {assignment.tag_code} 不在分类法 {taxonomy.version} 中",
                )
            # 副标签允许自由词，未进入受控词表时不校验。
            continue
        if assignment.role is TagRole.PRIMARY and catalog_tag.level is not TagLevel.PRIMARY:
            raise InvariantViolation("paper_tag.tag_code", f"{assignment.tag_code} 不是主标签")
        if assignment.role is TagRole.SECONDARY and catalog_tag.level is TagLevel.PRIMARY:
            raise InvariantViolation(
                "paper_tag.tag_code", f"主标签 {assignment.tag_code} 不能作为副标签"
            )
        if require_active and not catalog_tag.active:
            raise InvariantViolation("paper_tag.tag_code", f"标签 {assignment.tag_code} 已停用")
    return primary


def _ensure_same_paper_buckets(buckets: Sequence[PaperBucket], paper_id: str | None) -> str | None:
    resolved = paper_id or (buckets[0].paper_id if buckets else None)
    if resolved is not None:
        for bucket in buckets:
            if bucket.paper_id != resolved:
                raise InvariantViolation(
                    "paper_bucket.paper_id",
                    f"桶关系属于 {bucket.paper_id}，不属于 {resolved}",
                )
    return resolved


def ensure_single_filing_bucket(
    buckets: Sequence[PaperBucket], *, paper_id: str | None = None
) -> PaperBucket:
    """每篇文献必须恰好一个归属桶。"""
    resolved = _ensure_same_paper_buckets(buckets, paper_id)
    filing = tuple(bucket for bucket in buckets if bucket.role is BucketRole.FILING)
    if len(filing) != 1:
        subject = resolved or "文献"
        raise InvariantViolation(
            "paper_bucket",
            f"{subject}必须恰好有一个归属桶，实际 {len(filing)} 个",
        )
    return filing[0]


def ensure_bucket_references(
    buckets: Sequence[PaperBucket],
    buckets_by_id: Mapping[str, Bucket],
    *,
    paper_id: str | None = None,
    require_active: bool = True,
) -> PaperBucket:
    """桶关系必须引用存在的桶，且同一桶关系不重复。"""
    filing = ensure_single_filing_bucket(buckets, paper_id=paper_id)
    seen: set[tuple[BucketRole, str]] = set()
    for assignment in buckets:
        key = (assignment.role, assignment.bucket_id)
        if key in seen:
            raise InvariantViolation(
                "paper_bucket", f"桶 {assignment.bucket_id} 的 {assignment.role.value} 重复"
            )
        seen.add(key)
        bucket = buckets_by_id.get(assignment.bucket_id)
        if bucket is None:
            raise InvariantViolation("paper_bucket.bucket_id", f"桶 {assignment.bucket_id} 不存在")
        if require_active and not bucket.active:
            raise InvariantViolation("paper_bucket.bucket_id", f"桶 {assignment.bucket_id} 已停用")
    return filing


def ensure_paper_classification(
    *,
    tags: Sequence[PaperTag],
    buckets: Sequence[PaperBucket],
    taxonomy: Taxonomy,
    buckets_by_id: Mapping[str, Bucket],
    paper_id: str | None = None,
    require_active: bool = True,
) -> tuple[PaperTag, PaperBucket]:
    """服务层分类聚合校验：一个主标签 + 一个归属桶 + 分类法归属。"""
    primary = ensure_tags_match_taxonomy(
        tags, taxonomy, paper_id=paper_id, require_active=require_active
    )
    ensure_secondary_tag_limit(tags, paper_id=paper_id)
    filing = ensure_bucket_references(
        buckets, buckets_by_id, paper_id=paper_id, require_active=require_active
    )
    return primary, filing


def choose_special_filing_kind(
    kinds: Iterable[SpecialBucketKind],
) -> SpecialBucketKind | None:
    """按“跨层综述 > 经典/奠基 > 远邻领域”选择唯一归属的特殊桶类型。"""
    selected: SpecialBucketKind | None = None
    for kind in kinds:
        if selected is None or SPECIAL_BUCKET_PRIORITY[kind] > SPECIAL_BUCKET_PRIORITY[selected]:
            selected = kind
    return selected


def ensure_bucket_catalog(buckets: Sequence[Bucket], *, require_all_special: bool = False) -> None:
    """桶目录校验：ID/tag_code 唯一，特殊桶类型不重复，可选要求三类齐全。"""
    ids = [bucket.id for bucket in buckets]
    if len(ids) != len(set(ids)):
        raise InvariantViolation("bucket.id", "桶 ID 必须唯一")
    topic_codes = [bucket.tag_code for bucket in buckets if bucket.bucket_type is BucketType.TOPIC]
    if len(topic_codes) != len(set(topic_codes)):
        raise InvariantViolation("bucket.tag_code", "同一主标签只能有一个主题桶")
    special_kinds = [
        bucket.special_kind
        for bucket in buckets
        if bucket.bucket_type is BucketType.SPECIAL and bucket.active
    ]
    if len(special_kinds) != len(set(special_kinds)):
        raise InvariantViolation("bucket.special_kind", "特殊桶类型不能重复")
    if require_all_special and set(special_kinds) != set(SpecialBucketKind):
        raise InvariantViolation("bucket.special_kind", "三个特殊桶必须齐全且激活")


def ensure_taxonomy_shape(
    taxonomy: Taxonomy,
    *,
    minimum_primary: int = MIN_PRIMARY_TAGS,
    maximum_primary: int = MAX_PRIMARY_TAGS,
) -> None:
    """确认分类法的结构要求：5–9 个启用主标签、无“其他”兜底桶。"""
    primary = [tag for tag in taxonomy.tags if tag.level is TagLevel.PRIMARY and tag.active]
    if not minimum_primary <= len(primary) <= maximum_primary:
        raise InvariantViolation(
            "taxonomy.tags",
            f"主标签数量必须在 {minimum_primary}–{maximum_primary} 之间，实际 {len(primary)} 个",
        )
    for tag in primary:
        if tag.name.strip().lower() in _CATCH_ALL_NAMES or tag.code in {"OTHER", "MISC"}:
            raise InvariantViolation("taxonomy.tags", f"禁止使用兜底主标签：{tag.code} {tag.name}")


def ensure_score_matches_bucket(snapshot: ScoreSnapshot, filing_bucket: Bucket) -> None:
    """特殊桶快照有档位无 P；主题桶快照有 P 无档位。"""
    if filing_bucket.bucket_type is BucketType.SPECIAL:
        if snapshot.priority is not None or snapshot.tier is None:
            raise InvariantViolation("score_snapshot", "特殊桶文献不得计算 P，必须使用档位")
    else:
        if snapshot.tier is not None or snapshot.priority is None:
            raise InvariantViolation("score_snapshot", "主题桶文献必须计算 P，不得使用特殊桶档位")


def ensure_queue_eligible(
    *,
    status: PaperStatus,
    snapshot: ScoreSnapshot,
    filing_bucket: Bucket,
    threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
) -> None:
    """只有 scored 且 R ≥ 门槛、归属主题桶的文献能进入主队列。"""
    if status is not PaperStatus.SCORED:
        raise InvariantViolation("paper.status", f"只有 scored 文献能入队，当前为 {status.value}")
    if filing_bucket.bucket_type is BucketType.SPECIAL:
        raise InvariantViolation("paper_bucket", "特殊桶文献不得进入主队列")
    ensure_score_matches_bucket(snapshot, filing_bucket)
    if snapshot.relevance < threshold:
        raise InvariantViolation(
            "score_snapshot.relevance",
            f"R={snapshot.relevance:.1f} 低于相关度门槛 {threshold:.1f}",
        )


def ensure_shelvable(*, status: PaperStatus, filing_bucket: Bucket) -> None:
    """只有 classified 且归属特殊桶的文献能进入 shelved。"""
    if status is not PaperStatus.CLASSIFIED:
        raise InvariantViolation(
            "paper.status", f"只有 classified 文献能归档到特殊桶，当前为 {status.value}"
        )
    if filing_bucket.bucket_type is not BucketType.SPECIAL:
        raise InvariantViolation("paper_bucket", "主题桶文献不得进入 shelved")


def classification_requires_review(
    primary_confidence: float,
    runner_up_confidence: float | None = None,
    *,
    threshold: float = DEFAULT_PRIMARY_CONFIDENCE_THRESHOLD,
    margin: float = DEFAULT_CLASSIFICATION_MARGIN,
) -> bool:
    """主标签置信度 <0.7，或与次高标签差距 <0.1 时必须人工确认。"""
    if not 0.0 <= primary_confidence <= 1.0:
        raise InvariantViolation("paper_tag.confidence", "主标签置信度必须在 0–1 之间")
    if runner_up_confidence is not None and not 0.0 <= runner_up_confidence <= 1.0:
        raise InvariantViolation("paper_tag.confidence", "次高置信度必须在 0–1 之间")
    if primary_confidence < threshold:
        return True
    return runner_up_confidence is not None and primary_confidence - runner_up_confidence < margin


def ensure_low_confidence_routed_to_review(*, status: PaperStatus, requires_review: bool) -> None:
    """低置信度分类若未进入 needs_review，服务层必须拒绝。"""
    if requires_review and status is not PaperStatus.NEEDS_REVIEW:
        raise InvariantViolation(
            "paper.status", "低置信度分类必须先进入 needs_review，不能直接推送"
        )


def ensure_push_eligible(
    *,
    status: PaperStatus,
    snapshot: ScoreSnapshot,
    filing_bucket: Bucket,
    summary: Summary,
    primary_tag: PaperTag,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    confidence_threshold: float = DEFAULT_PRIMARY_CONFIDENCE_THRESHOLD,
) -> None:
    """推送前门槛：状态、摘要校验、分类置信度、相关度与特殊桶规则。"""
    if summary.paper_id != snapshot.paper_id or primary_tag.paper_id != snapshot.paper_id:
        raise InvariantViolation("paper_id", "摘要、分类与评分必须属于同一篇文献")
    if summary.validation_status is not SummaryValidationStatus.PASSED:
        raise InvariantViolation("summary.validation_status", "摘要未通过校验，不能推送")
    if primary_tag.role is not TagRole.PRIMARY:
        raise InvariantViolation("paper_tag.role", "推送必须提供主标签分配")
    if primary_tag.confidence < confidence_threshold:
        raise InvariantViolation("paper_tag.confidence", "低置信度分类必须先人工确认，不能直接推送")
    ensure_score_matches_bucket(snapshot, filing_bucket)
    if filing_bucket.bucket_type is BucketType.SPECIAL:
        if status is not PaperStatus.SHELVED:
            raise InvariantViolation(
                "paper.status", f"特殊桶推送前必须处于 shelved，当前为 {status.value}"
            )
        return
    if status is not PaperStatus.QUEUED:
        raise InvariantViolation(
            "paper.status", f"主队列推送前必须处于 queued，当前为 {status.value}"
        )
    if snapshot.relevance < relevance_threshold:
        raise InvariantViolation(
            "score_snapshot.relevance",
            f"R={snapshot.relevance:.1f} 低于相关度门槛 {relevance_threshold:.1f}",
        )


def ensure_static_score_frozen(
    *,
    previous: ScoreSnapshot | None,
    candidate: ScoreSnapshot,
    trigger: RecalculationTrigger | None = None,
    confirmed: bool = False,
) -> None:
    """静态分只允许在确认过的版本/元数据变更后重算。"""
    if previous is None:
        return
    if previous.paper_id != candidate.paper_id:
        raise InvariantViolation("score_snapshot.paper_id", "不能跨文献比较静态分")
    if previous.static_score == candidate.static_score:
        return
    if trigger is None or not confirmed:
        raise InvariantViolation(
            "score_snapshot.static_score", "静态分已冻结；重算必须有原因并经用户确认"
        )
    version_changed = {
        RecalculationTrigger.PROFILE_CHANGED: (
            previous.profile_version != candidate.profile_version
        ),
        RecalculationTrigger.TAXONOMY_CHANGED: (
            previous.taxonomy_version != candidate.taxonomy_version
        ),
        RecalculationTrigger.CONFIG_CHANGED: (previous.config_version != candidate.config_version),
        RecalculationTrigger.METADATA_CORRECTED: True,
    }[trigger]
    if not version_changed:
        raise InvariantViolation(
            "score_snapshot.static_score",
            f"重算原因 {trigger.value} 缺少对应版本变更",
        )


def ensure_static_score_snapshot_append_only(
    history: Sequence[StaticScoreSnapshot],
    candidate: StaticScoreSnapshot,
) -> None:
    """静态分快照只允许在版本变化后按日期追加，不能同版本重算。"""
    if not history:
        return
    latest = max(history, key=lambda item: item.score_date)
    if candidate.paper_id != latest.paper_id:
        raise InvariantViolation("static_score_snapshot.paper_id", "不能跨文献比较静态分")
    if candidate.score_date <= latest.score_date:
        raise InvariantViolation(
            "static_score_snapshot.score_date", "新快照日期必须晚于已有静态分快照"
        )
    if (
        candidate.profile_version,
        candidate.taxonomy_version,
        candidate.config_version,
    ) == (
        latest.profile_version,
        latest.taxonomy_version,
        latest.config_version,
    ):
        raise InvariantViolation(
            "static_score_snapshot.versions",
            "档案/分类法/评分配置版本未变，静态分已冻结，不能重算",
        )


def ensure_score_snapshot_append_only(
    history: Sequence[ScoreSnapshot],
    candidate: ScoreSnapshot,
    *,
    paper_id: str | None = None,
) -> None:
    """同一天只允许一条快照，且新快照日期必须晚于历史最大日期。"""
    if paper_id is None and history:
        paper_id = history[0].paper_id
    if paper_id is not None and candidate.paper_id != paper_id:
        raise InvariantViolation(
            "score_snapshot.paper_id", f"快照属于 {candidate.paper_id}，不属于 {paper_id}"
        )
    for snapshot in history:
        if snapshot.paper_id != candidate.paper_id:
            raise InvariantViolation("score_snapshot.paper_id", "评分历史混入了其他文献")
        if snapshot.score_date == candidate.score_date:
            raise InvariantViolation("score_snapshot.score_date", "同一天只能有一条评分快照")
        if snapshot.score_date > candidate.score_date:
            raise InvariantViolation(
                "score_snapshot.score_date", "评分快照必须按日期追加，不能回填旧日期"
            )


def ensure_event_history_append_only(events: Sequence[Event]) -> None:
    """事件 ID 唯一，时间非递减。"""
    seen: set[str] = set()
    last_created = None
    for event in events:
        if event.id in seen:
            raise InvariantViolation("event.id", f"事件 ID 重复：{event.id}")
        seen.add(event.id)
        if last_created is not None and event.created_at < last_created:
            raise InvariantViolation("event.created_at", "事件日志必须按时间追加")
        last_created = event.created_at


def ensure_event_appended(history: Sequence[Event], event: Event) -> None:
    """新事件不能覆盖已有 ID，且不能早于最后一条事件。"""
    ensure_event_history_append_only(history)
    if any(existing.id == event.id for existing in history):
        raise InvariantViolation("event.id", f"事件 ID 重复：{event.id}")
    if history and event.created_at < history[-1].created_at:
        raise InvariantViolation("event.created_at", "事件日志必须按时间追加")


def ensure_archive_snapshot_immutable(
    original: ArchiveSnapshot, candidate: ArchiveSnapshot
) -> None:
    """归档快照不可修改；修订必须追加新记录并保留原快照。"""
    if original != candidate:
        raise InvariantViolation("archive_snapshot", "归档快照不可变；修订必须追加新记录")


__all__ = [
    "DEFAULT_CLASSIFICATION_MARGIN",
    "DEFAULT_PRIMARY_CONFIDENCE_THRESHOLD",
    "DEFAULT_RELEVANCE_THRESHOLD",
    "MAX_PRIMARY_TAGS",
    "MAX_SECONDARY_TAGS",
    "MIN_PRIMARY_TAGS",
    "choose_special_filing_kind",
    "classification_requires_review",
    "ensure_archive_snapshot_immutable",
    "ensure_bucket_catalog",
    "ensure_bucket_references",
    "ensure_event_appended",
    "ensure_event_history_append_only",
    "ensure_low_confidence_routed_to_review",
    "ensure_paper_classification",
    "ensure_push_eligible",
    "ensure_queue_eligible",
    "ensure_score_matches_bucket",
    "ensure_score_snapshot_append_only",
    "ensure_secondary_tag_limit",
    "ensure_shelvable",
    "ensure_single_filing_bucket",
    "ensure_single_primary_tag",
    "ensure_static_score_frozen",
    "ensure_static_score_snapshot_append_only",
    "ensure_tags_match_taxonomy",
    "ensure_taxonomy_shape",
]
