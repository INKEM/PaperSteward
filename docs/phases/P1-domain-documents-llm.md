# P1 领域模型、版本化文档与 LLM 网关

> **阶段**：P1｜**里程碑**：Alpha｜**状态**：P1-S1 完成；P1-S2–S6 待执行
> **依据**：[大阶段开发计划](../PaperSteward_大阶段开发计划.md) §3 P1、
> [设计方案](../PaperSteward_文献自动挖掘推送与优先级排序系统设计方案.md) §2.5、§3、§4.3–4.4、§5、§7、§10
> **上一阶段交接**：[P0-baseline.md](P0-baseline.md) §8.2
> **最后更新**：2026-10-21

## 1. 阶段目标

让系统拥有稳定的领域模型、可版本化的 profile/taxonomy 文档、可校验的评分配置，
以及可替换的 LLM 网关。P1 结束后，后续阶段不再“边写边改核心表”。

P1 不实现初始化 UI、真实文献源、摘要/分类/评分算法与日报推送；这些分别属于
P2、P3、P4/P5、P6。

## 2. 小阶段拆分

| 小阶段 | 目标 | 状态 | 依赖 | 主要验收 |
|--------|------|------|------|----------|
| P1-S1 | 实体、枚举、状态机、不变量 | **完成** | P0 | 单元测试；无 DB 依赖 |
| P1-S2 | 正式迁移、仓储、事件日志 | 待开始 | S1 | 迁移 up/down；约束与追加写 |
| P1-S3 | profile/taxonomy 解析、版本、diff | 待开始 | S1/S2 | validate/diff；确认事件 |
| P1-S4 | 评分 YAML 校验、JSON schema | 待开始 | S1/S3 | 非法权重拒绝；schema 正反例 |
| P1-S5 | LLM 网关、FakeLLM、prompt registry、修复重试 | 待开始 | S1/S4 | 非法 JSON 不进入业务表 |
| P1-S6 | 契约/迁移/不变量补测、阶段门与 handoff | 待开始 | S1–S5 | `make check` 等阶段门 |

> S2 起才引入数据库与 CLI 演示；S1 刻意保持为纯领域层，避免把迁移细节
> 反向污染业务契约。

## 3. 冻结契约（P1-S1）

### 3.1 枚举

字符串值即持久化契约，只允许追加，不允许重命名既有值。

| 枚举 | 值 |
|------|----|
| `DocumentStatus` | `draft` / `confirmed` / `archived` |
| `PaperStatus` | `discovered` / `deduped` / `rejected` / `screened` / `needs_review` / `enriched` / `summarized` / `classified` / `shelved` / `scored` / `queued` / `snoozed` / `pushed` / `reading` / `done` / `archived` |
| `TagLevel` | `primary` / `secondary` |
| `TagRole` | `primary` / `secondary` |
| `BucketType` | `topic` / `special` |
| `BucketRole` | `filing` / `membership` |
| `SpecialBucketKind` | `review` / `classic` / `distant`（优先级 3/2/1） |
| `AssignedBy` | `agent` / `user` / `system` |
| `OaStatus` | `open` / `closed` / `unknown` |
| `SummaryValidationStatus` | `pending` / `passed` / `failed` / `needs_review` |
| `ReadMode` | `deep` / `skim` |
| `ScoreTier` | `high` / `medium` / `low` |
| `RunStatus` | `running` / `succeeded` / `failed` / `cancelled` |
| `EventType` | `paper.*` 生命周期/用户动作、`profile.confirmed`、`taxonomy.confirmed`、`config.updated` |
| `RecalculationTrigger` | `profile_changed` / `taxonomy_changed` / `config_changed` / `metadata_corrected` |

### 3.2 实体

实体是不可变 `dataclass(frozen=True, slots=True)`，位于
`src/papersteward/core/entities.py`，不依赖 ORM/FastAPI。

| 实体 | 关键字段 | 说明 |
|------|----------|------|
| `ResearchProfile`（别名 `Profile`） | id, version, status, content_md, parsed_json, confirmed_at, change_log | Markdown 为源，JSON 为解析缓存 |
| `Taxonomy` | id, version, status, content_md, tags, confirmed_at | 标签代码版本内唯一 |
| `Tag` | code, name, level, definition, parent_code, include, exclude, examples, aliases, active | 主/副标签统一存储 |
| `Bucket` | id, name, bucket_type, tag_code, special_kind, quota, active | 主题桶/三个特殊桶 |
| `Paper` | id, title, status, authors, venue, published_at, first_seen_at, abstract, doi, arxiv_id, url, pdf_url, oa_status, canonical_id | 文献主记录 |
| `PaperTag` | paper_id, tag_code, role, taxonomy_version, confidence, assigned_by, provenance | 主标签恰好一条 |
| `PaperBucket` | paper_id, bucket_id, role, assigned_by, reason | filing 恰好一条 |
| `Summary` | paper_id, summary_100, what/how/finding/limitation, evidence, validation_status, provenance | 100 非空白字符上限 |
| `ScoreSnapshot` | paper_id, score_date, R/A/E/S/F/H/U/D, priority/tier, breakdown_json, config/profile/taxonomy_version, provenance | 每日追加；主题桶有 P，特殊桶有档位 |
| `Event` | id, event_type, created_at, paper_id, payload_json | 追加写；paper_id 可为空 |
| `Rating` | paper_id, score(1–5 或空), note, tags_at_read, read_mode, created_at | 主观分可空 |
| `Config` | key, version, value_json, updated_by, updated_at | 评分配置等 |
| `Run` | id, run_type, status, started_at, finished_at, stats/errors/cost_json | 终态必须有结束时间 |
| `Provenance` | model, prompt_version, generated_at, input_hash | Agent 产物溯源 |
| `ArchiveSnapshot` | paper_id, archived_at, primary_tag, filing_bucket_id, 版本三元组, content_hash, rating/note | 读完时生成，不可变 |

字段补充说明：

- `Summary.evidence` 是可核对的原文引文元组；持久化层可映射为 `evidence_json`。
- `ScoreSnapshot` 使用可读字段名；与设计文档的对应为
  `relevance=R`、`authority=A`、`extra_value=E`、`static_score=S`、
  `freshness=F`、`hunger=H`、`preference=U`、`dynamic_score=D`、`priority=P`。
- `ArchiveSnapshot` 不在设计文档 §3.2 的实体表中，但“归档快照不可变”需要
  可测试的载体，因此在 S1 一并冻结；P6 若增加字段必须走迁移 + ADR。

### 3.3 状态机

文献状态迁移白名单（`core/state_machine.py`）：

```text
discovered  → deduped | rejected
deduped     → screened | rejected
screened    → enriched | needs_review | rejected
needs_review→ enriched | summarized | classified | rejected
enriched    → summarized | needs_review
summarized  → classified | needs_review
classified  → scored | shelved | needs_review
scored      → queued | shelved | needs_review | rejected
queued      → pushed | snoozed | shelved | rejected
snoozed     → queued | rejected
pushed      → reading | queued | snoozed | rejected
reading     → done | snoozed | rejected
done        → archived
archived    → （终态）
rejected    → screened（用户手动恢复）
shelved     → classified | scored | pushed | reading | rejected
```

- `done` 是进入 `archived` 的唯一入口；`archived` 无出边；
- `needs_review` 不能直达 `queued`/`pushed`；
- `shelved` 允许用户把特殊桶文献提升回主题桶或直接阅读；提升必须先 `shelved → scored`
  并重新计算 P，不能直接进入 `queued`；
- 文档：`draft → confirmed → archived`，确认时显式写入 `confirmed_at`；
- 运行：`running → succeeded | failed | cancelled`。

### 3.4 不变量

| 不变量 | 落地函数 |
|--------|----------|
| 每篇恰好一个主标签 | `ensure_single_primary_tag` |
| 副标签 0–5 且不重复 | `ensure_secondary_tag_limit` |
| 主标签属于当前激活分类法、版本一致 | `ensure_tags_match_taxonomy` |
| 每篇恰好一个归属桶、引用存在且不重复 | `ensure_single_filing_bucket`、`ensure_bucket_references` |
| 分类聚合双重校验 | `ensure_paper_classification` |
| 特殊桶优先级（综述 > 经典 > 远邻） | `choose_special_filing_kind` |
| 主标签 5–9、禁止“其他”兜底 | `ensure_taxonomy_shape` |
| 主题桶算 P、特殊桶只分档 | `ensure_score_matches_bucket` |
| 只有 scored + R≥55 + 主题桶可入队 | `ensure_queue_eligible` |
| 只有 classified + 特殊桶可 shelved | `ensure_shelvable` |
| 置信度 <0.7 或与次高差 <0.1 需人工确认 | `classification_requires_review`、`ensure_low_confidence_routed_to_review` |
| 摘要未通过校验、低置信度不得推送 | `ensure_push_eligible` |
| 静态分冻结，重算需原因 + 确认 + 版本变化 | `ensure_static_score_frozen` |
| 评分快照追加写、同日唯一 | `ensure_score_snapshot_append_only` |
| 事件追加写、ID 唯一、时间非递减 | `ensure_event_history_append_only`、`ensure_event_appended` |
| 归档快照不可变 | `ensure_archive_snapshot_immutable` |
| Agent 标签/摘要必须带溯源 | 实体 `__post_init__` |

### 3.5 错误契约

领域异常位于 `core/errors.py`：

- `DomainValidationError`：单实体字段/取值非法；
- `InvariantViolation`：跨实体业务不变量被破坏；
- `InvalidTransition`：状态机不允许的迁移。

异常带 `field`（如 `paper_tag.confidence`）与 `message`，`str(exc)` 为
`field: message`。P1-S3 的文档校验可据此定位到字段；API 层后续再映射为统一
错误信封。

## 4. P1-S1 执行记录

### 4.1 交付物

| 文件 | 作用 |
|------|------|
| `src/papersteward/core/enums.py` | 稳定枚举与特殊桶优先级 |
| `src/papersteward/core/errors.py` | 领域错误、`field` 定位 |
| `src/papersteward/core/entities.py` | 15 个实体/值对象与字段校验 |
| `src/papersteward/core/state_machine.py` | 文献/文档/运行状态机与状态事件映射 |
| `src/papersteward/core/invariants.py` | 分类、桶、评分、事件、归档不变量 |
| `src/papersteward/core/__init__.py` | 领域层公共导出 |
| `tests/unit/test_domain_entities.py` | 实体正反例、不可变性、溯源 |
| `tests/unit/test_state_machine.py` | 迁移白名单、门槛、终态 |
| `tests/unit/test_invariants.py` | 主标签/归属桶/评分/推送/追加重算 |
| `docs/phases/P1-domain-documents-llm.md` | 本阶段契约与交接 |
| `README.md`、`AGENTS.md`、`CHANGELOG.md` | 当前阶段、能力边界与用户可见变更 |

### 4.2 验收结果

2026-10-21 在本地执行：

```bash
make check
```

- `ruff format --check`：通过；
- `ruff check`：通过；
- `mypy`（strict，`src/papersteward`）：通过；
- `pytest`：**183 passed**，0 failed；`core/` 行/分支覆盖率 99%；
- `make test-integration`：45 passed；
- `make migrate-check`：upgrade/downgrade/upgrade OK（未新增迁移）；
- `make e2e`：1 passed；
- `make demo DEMO_PORT=8765`：`/health` 与 `/papers` 正常；
- `make secret-scan`：OK；
- 未新增依赖、未访问网络、未使用真实 LLM；
- 未修改 P0 的 `papers` / `runs` 表或 API 行为。

### 4.3 与计划的偏差

- **文件数与行数超过指引**：S1 是领域契约的基础设施，实体、校验与测试需同交；
  文件数超过“≤8 个文件”、行数超过 400 行。S2 起按常规小阶段控制。
- **额外加入 `Provenance` 与 `ArchiveSnapshot`**：分别落实“所有 Agent 产物带
  溯源”和“归档快照不可变”，否则不变量无法被测试。
- **未实现迁移/仓储/CLI**：属于 P1-S2/S3；S1 只冻结纯领域契约。
- **实体不是 Pydantic 模型**：领域层保持框架无关；S4 的 JSON schema 与 Pydantic
  契约单独实现，避免 ORM/HTTP 关注点污染实体。
- **`EventType` 采用带命名空间的值**（如 `paper.done`），比裸状态值更适合全局
  事件日志；状态值本身仍与设计文档一致。

### 4.4 交接给 P1-S2

**已就绪**

- 可映射到正式表的实体、枚举与字段校验；
- 状态机白名单、最终态与“done 才可归档”规则；
- 分类/桶/评分/推送/追加重算不变量函数；
- 领域错误携带字段定位。

**S2 需要完成**

1. 新增正式 Alembic 迁移，替换 P0 的 `papers` / `runs` stub；
2. 表：`research_profile`、`taxonomy`、`tag`、`bucket`、`paper`、`paper_tag`、
   `paper_bucket`、`summary`、`score_snapshot`、`event`、`rating`、`config`、`run`、
   `archive_snapshot`；
3. DB 层约束（至少：每篇唯一主标签、每篇唯一 filing、评分快照日期唯一、事件 ID 唯一）
   与服务层不变量双重校验；
4. 仓储：不自行提交事务，使用 `session_scope`；事件追加写；
5. 集成测试：临时 SQLite、迁移 up/down、约束违反、JSON 字段往返。

**S2 不能做**

- profile/taxonomy Markdown 解析、diff（S3）；
- 评分配置/YAML 校验与 JSON schema（S4）；
- LLM 调用（S5）；
- 真实文献源、摘要/分类/评分实现（P3 起）。

**建议的 S2 迁移要点**

- `paper.status` 使用 `PaperStatus` 值，原 stub 的 `new` 不再出现；
- `run.kind` 映射为 `run_type`，`run.status` 使用 `RunStatus`；
- 所有 JSON 字段保留 `*_json` 名称，值为合法 JSON 对象；
- 时间字段统一 UTC aware；SQLite 读取时由仓储恢复为 aware UTC；
- `archive_snapshot` 只允许 INSERT，不提供 UPDATE 仓储方法。

## 5. 风险与待确认

- **S2 的 DB 约束能力**：SQLite 对“每篇恰好一个主标签/一个 filing”的部分唯一索引
  支持有限，需在仓储事务与服务层双重校验；迁移测试必须覆盖。
- **特殊桶提升**：`shelved → scored` 是状态机允许边，但服务层必须强制重算静态分并
  生成新快照后再入队；S2 的仓储接口应只暴露“重新评分后入队”的用例，而不是裸迁移。
- **归档快照字段**：P6 可能要求更多阅读元数据；届时新增列/表迁移，不改既有字段。
- **EventType 扩展**：P2 的 G1–G4 门槛事件将追加值，不改已有值。
