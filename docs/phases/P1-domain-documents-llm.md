# P1 领域模型、版本化文档与 LLM 网关

> **阶段**：P1｜**里程碑**：Alpha｜**状态**：P1-S1–S4 完成（S1–S3 验收 F1–F5 已返工）；P1-S5–S6 待执行
> **独立验收**：[P1-S1-S3-acceptance.md](P1-S1-S3-acceptance.md)（2026-10-21，有条件通过；F1–F5 返工见 §7）
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
| P1-S2 | 正式迁移、仓储、事件日志 | **完成** | S1 | 迁移 up/down；约束与追加写 |
| P1-S3 | profile/taxonomy 解析、版本、diff | **完成** | S1/S2 | validate/diff；确认事件 |
| P1-S4 | 评分 YAML 校验、JSON schema | **完成** | S1/S3 | 非法权重拒绝；schema 正反例 |
| P1-S5 | LLM 网关、FakeLLM、prompt registry、修复重试 | 待开始 | S1/S4 | 非法 JSON 不进入业务表 |
| P1-S6 | 契约/迁移/不变量补测、阶段门与 handoff | 待开始 | S1–S5 | `make check` 等阶段门 |

> S1 刻意保持为纯领域层；S2 才引入正式数据库与仓储，避免把迁移细节反向污染
> 业务契约。

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
  文件数超过“≤8 个文件”、行数超过 400 行。S2 因 schema/仓储面较大同样超出，见 §5.6。
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

## 5. P1-S2 执行记录

### 5.1 目标

用正式领域表替换 P0 的 `papers` / `runs` stub，建立 ORM 行与领域实体之间的映射、
仓储接口和只追加的事件日志；DB 约束与 S1 不变量形成双重校验。

### 5.2 交付物

| 文件 | 作用 |
|------|------|
| `migrations/versions/0002_domain_schema.py` | 建立 14 张正式表；迁移 P0 最小字段；可降级回 stub |
| `src/papersteward/storage/models.py` | `*Row` ORM 模型、FK/CHECK/部分唯一索引 |
| `src/papersteward/storage/mappers.py` | 行 ↔ 不可变实体；枚举、JSON、UTC 转换 |
| `src/papersteward/storage/repositories.py` | 13 个仓储：论文、标签、桶、摘要、评分、事件、评分/阅读、配置、运行、档案、分类法、归档 |
| `src/papersteward/storage/__init__.py` | 仓储与行模型导出 |
| `src/papersteward/cli/demo.py` | demo seed 改用正式 `Paper` 与仓储 |
| `tests/integration/test_migrations.py` | 0002 up/down、P0 数据迁移、`alembic check` |
| `tests/integration/test_repositories.py` | 往返、事务边界、约束、追加写、JSON/UTC |
| `tests/integration/test_api.py`、`test_cli.py`、`test_database.py` | 适配正式模型与 0002 版本 |

### 5.3 数据库契约

14 张表：`research_profile`、`taxonomy`、`tag`、`bucket`、`paper`、`paper_tag`、
`paper_bucket`、`summary`、`score_snapshot`、`event`、`rating`、`config`、`run`、
`archive_snapshot`。

关键约束：

- `research_profile` / `taxonomy`：`(id, version)` 复合主键；每个 id 最多一个
  `confirmed` 版本（SQLite 部分唯一索引）；`draft` 不得有 `confirmed_at`；
- `tag`：`(taxonomy_id, taxonomy_version, code)` 复合主键，FK 级联到分类法版本；
- `paper_tag`：`(paper_id, tag_code, role)` 主键；`role='primary'` 时 `paper_id`
  部分唯一；置信度 0–1；溯源列全空或全非空；
- `paper_bucket`：`role='filing'` 时 `paper_id` 部分唯一；FK 指向 `paper`/`bucket`；
- `score_snapshot`：`(paper_id, score_date)` 主键；各分项 0–100；`priority` 与
  `tier` 恰好一个非空；
- `summary`：每篇一条；`event`：主键 ID，外键 `paper_id` 可为空；
- `run`：`running` 无结束时间，终态必须有且不早于开始时间；
- `rating`：分数为空或 1–5；`archive_snapshot`：每篇一条，仓储不提供更新/删除。

数据与兼容：

- 升级时把 `papers` 的 `id/title/created_at` 迁入 `paper`，`status='new'` →
  `discovered`、`'ready'` → `screened`；把 `runs.kind` → `run_type`、旧状态映射到
  `RunStatus`，`error` 写入 `errors_json`；
- 降级只恢复空的 P0 stub 表，正式表数据不保留；
- 所有 JSON 字段保留 `*_json` 命名；SQLite naive datetime 在 mapper 中补 UTC；
- `GET /papers` 的字段信封不变，但 `status` 现在返回正式枚举值（如 `discovered`）。

### 5.4 仓储契约

- 构造函数接收 `Session`；只 `flush`，不 `commit`；
- `PaperRepository.transition()` 先经 S1 状态机校验，再更新状态并同事务追加
  `PAPER_STATUS_EVENTS` 事件；
- `EventRepository`、`ScoreSnapshotRepository`、`ArchiveSnapshotRepository` 只提供
  追加/读取，不提供 update/delete；
- `SummaryRepository.save()` 对同一 paper 覆盖摘要；
- `ProfileRepository` / `TaxonomyRepository` 按版本读写；`Taxonomy` 的标签从 `tag`
  表加载并按“主标签优先、代码排序”恢复；
- `ScoreSnapshotRepository.append()` 复用 `ensure_score_snapshot_append_only`；
- 行映射统一处理枚举、JSON 列表/dict 与 UTC aware datetime。

### 5.5 验收结果

2026-10-21 在本地执行：

- `make check`：**195 passed**，ruff/mypy 通过；
- `make test-integration`：**57 passed**；
- `make migrate-check`：upgrade/downgrade/upgrade OK；
- `make e2e`：1 passed；
- `make demo DEMO_PORT=8766`：现有 0001 库升级到 0002，`/health` 与 `/papers` 正常；
- `make secret-scan`：OK；
- `pytest --cov=papersteward.storage`：**96%**；
- `alembic check`（测试内）：无待生成迁移；
- 未新增依赖、未访问网络、未使用真实 LLM。

### 5.6 与计划的偏差与限制

- **数据迁移范围有限**：P0 stub 只有 `id/title/status/created_at` 与运行最小字段，
  因此只能迁移这些；P0 的 `error` 文本保留在 `errors_json.error`；
- **降级有损**：`0002 → 0001` 重建空的 `papers`/`runs`，正式表数据丢弃；这是开发期
  stub 替换的显式取舍，已在迁移 docstring 记录；
- **API 状态值变化**：`/papers` 从 stub 的 `new` 变为正式 `discovered`；信封不变；
- **服务编排仍缺失**：仓储只提供状态机原子迁移；“特殊桶提升需先重算静态分”仍是
  P5/P6 服务层职责，不能直接调用裸仓储入队；
- **SQLite 约束**：部分唯一索引依赖 SQLite 语法；目标仍是 SQLite 本地优先，不承诺
  Postgres；
- **文件/行数超指引**：S2 涉及 schema、映射、13 个仓储和约束测试，超过 8 文件/
  400 行；已在本节记录。

### 5.7 交接给 P1-S3

**已就绪**

- `ProfileRepository` / `TaxonomyRepository`：按 `(id, version)` add/get/list，支持
  `latest_confirmed()`；确认/编辑写入由 S3 在此之上补充；
- 分类法标签版本化存储，`taxonomy.tags_json` 保留可读快照；
- `EventRepository` 可写入 `profile.confirmed` / `taxonomy.confirmed`；
- 精确字段错误、UTC/JSON 映射和约束违反测试。

**P1-S3 需要完成**

1. Markdown + YAML front matter 解析器，输出 `ResearchProfile` / `Taxonomy`；
2. `profile validate` / `taxonomy validate` / `doc diff` CLI；
3. 版本创建、确认、归档流程；确认时通过 `EventRepository` 写入事件；
4. 字段级错误信息（复用 `DomainValidationError.field`）；
5. 正反例与 golden diff 测试。

**S3 不能做**

- 评分 YAML 校验与 JSON schema（S4）；
- LLM 网关（S5）；
- 真实文献源、摘要/分类/评分实现（P3 起）；
- 初始化门槛/UI（P2）。

**S3 实现提示**

- 确认后的版本不可原地修改；新版本用新行，旧版本置为 `archived`；
- `Taxonomy.tags` 的代码稳定，名称/别名变更不改 code；
- 解析失败不要写入仓储，错误需包含文件、字段与行列（如可用）；
- diff 基于实体而非 Markdown 文本，保证确认前后一致。

## 6. P1-S3 执行记录

### 6.1 目标

建立 profile/taxonomy 的 Markdown + YAML front matter 解析、字段级校验、版本
确认/归档、可读 diff 与 CLI，让 P2 能直接复用文档契约。

### 6.2 交付物

| 文件 | 作用 |
|------|------|
| `docs/adr/0001-yaml-front-matter.md` | PyYAML 依赖决策、替代方案与安全约束 |
| `pyproject.toml` | 运行时依赖 `PyYAML`；dev 依赖 `types-PyYAML` |
| `src/papersteward/documents/frontmatter.py` | front matter 切分、`safe_load`、`DocumentError`（source/line/field） |
| `src/papersteward/documents/profile.py` | profile Pydantic 契约与语义校验 |
| `src/papersteward/documents/taxonomy.py` | `## Txx` 主标签解析、副标签 front matter、结构校验 |
| `src/papersteward/documents/diff.py` | 实体 diff（dict/对象列表/标量列表）与稳定渲染 |
| `src/papersteward/documents/service.py` | import / confirm / archive，同事务追加事件 |
| `src/papersteward/cli/documents.py` | validate/import/confirm/archive/diff 命令实现 |
| `src/papersteward/cli/main.py` | `profile`、`taxonomy`、`doc diff` 子命令 |
| `tests/fixtures/profiles/`、`tests/fixtures/taxonomies/` | 合法/非法/版本 diff fixtures |
| `tests/unit/test_documents_*.py`、`tests/integration/test_documents_service.py`、`test_cli_documents.py` | 解析、错误定位、diff、事件、CLI 测试 |

### 6.3 文档格式契约

**Profile**

- 必须以 `---` 开头的 YAML front matter 开始，并有 Markdown 正文；
- 必填：`version`、3–5 个 `core_questions`、至少 1 个 `subdirections`、
  `include`、`reading.weekly_capacity`、`reading.deep_skim_ratio`；
- `subdirections[].target_share` 之和必须为 1.0；`deep_skim_ratio` 两项之和必须
  为 1.0；
- `core_questions[].id`、`subdirections[].id` 在各自列表内唯一；
- 未知 front matter 字段保留在 `parsed_json`，不静默丢弃；
- `profile_id` 可省略，回退到文件名 stem；CLI 可用 `--id` 覆盖；
- front matter 与正文不一致时以 Markdown 源为准（`content_md` 保存全文）。

**Taxonomy**

- front matter 必填 `version`，可选 `taxonomy_id`、`secondary_tags`；
- 正文每个主标签用 `## T01 名称` 分节，必填“定义/纳入/排除/正例”；
- 主标签 5–9 个，禁止“其他/Other”等兜底标签；
- 副标签放在 front matter 的 `secondary_tags` 列表，可带 `parent_code`；
- `边界`/`常见误分` 保留在 `content_md` 供人阅读，不进入 `Tag` 机器字段；
- 主标签代码稳定；名称/别名变更不改 code。

**错误**

所有解析错误为 `DocumentError`（继承 `DomainValidationError`），带
`source`、`line`、`field` 与中文 message，例如：

```text
tests/fixtures/profiles/invalid.md:5: front_matter.core_questions:
List should have at least 3 items after validation, not 1
```

### 6.4 版本、确认与归档

- `profile import` / `taxonomy import`：解析通过后写入 draft；解析失败不写库；
- `profile confirm` / `taxonomy confirm`：只允许 draft → confirmed；若已存在旧
  `confirmed` 版本，先将其转为 `archived`，再确认新版本；
- 确认/归档事件写入 `event` 表：`profile.confirmed`、`profile.archived`、
  `taxonomy.confirmed`、`taxonomy.archived`，payload 含 `document_id/version/status`；
- `archive` 只允许 confirmed → archived；draft 归档被状态机拒绝；
- 事件 ID 由 `kind/action/document_id/version` 的 SHA-256 前 16 位生成，稳定且
  重复确认不会产生第二条同 ID 事件（状态机也会拒绝重复确认）；
- 服务不 commit；CLI 通过 `session_scope` 控制事务。

### 6.5 Diff

- diff 基于实体/解析后的结构，不比较 Markdown 原始文本；
- dict 逐键比较；带唯一 `id`/`code` 的对象列表按标识比较；标量列表按多重集合
  比较，忽略顺序；
- 输出固定 `+ path: value`、`- path: value`、`~ path: old -> new`；无差异输出
  “无差异”；
- CLI 支持两个文件，或 `--id` + 两个数据库版本。

### 6.6 CLI

```bash
papersteward profile validate tests/fixtures/profiles/basic.md
papersteward taxonomy validate tests/fixtures/taxonomies/basic.md
papersteward doc diff profile tests/fixtures/profiles/basic.md tests/fixtures/profiles/v2.md
papersteward profile import tests/fixtures/profiles/basic.md
papersteward profile confirm 1.0 --id demo-profile --at 2026-10-21T09:00:00Z
papersteward profile archive 1.0 --id demo-profile
papersteward doc diff profile 1.0 1.1 --id demo-profile
```

- validate 成功输出 `OK profile/taxonomy ...`，失败退出 1 并打印字段/行；
- import/confirm/archive 失败退出 1，并给出可操作提示；
- `--json` 可用于 validate/diff 的机器读取。

### 6.7 验收结果

2026-10-21 在本地执行：

- `make check`：**267 passed**，ruff/mypy 通过；
- `make test-integration`：**83 passed**；
- `pytest -q`（含 e2e）：**268 passed**；
- `make migrate-check`：upgrade/downgrade/upgrade OK；
- `make e2e`：1 passed；
- `make demo DEMO_PORT=8768`：`/health` 与 `/papers` 正常；
- `make secret-scan`：OK；
- `pytest --cov=papersteward.documents`：**96%**；
- 演示命令 `profile validate` / `taxonomy validate` / `doc diff` 均通过；
- 新增运行时依赖仅 PyYAML（已有 ADR），未访问网络或真实 LLM。

### 6.8 与计划的偏差与限制

- **新增 PyYAML**：按规则先写 ADR，使用 `safe_load`；替代方案见
  [ADR 0001](../adr/0001-yaml-front-matter.md)；
- **taxonomy 副标签格式**：设计文档正文模板只展示主标签，S3 将副标签放在
  front matter 的 `secondary_tags`，避免解析 `###` 段落带来的歧义；
- **taxonomy 边界/误分说明**：保留在 `content_md`，不进入 `Tag` 字段；若 P2 需要
  边界案例库，需新增字段/迁移；
- **`updated`**：profile 写入 `parsed_json`；taxonomy 实体暂无 `updated` 字段，
  仅保留在 Markdown 源，diff 不比较该字段；
- **版本号是字符串**：diff 不按 semver 排序，确认也不自动递增；由用户/Agent 写入
  新 version；
- **确认需要显式 `--id`**：当前没有“默认 profile/taxonomy”概念，避免误确认；
- **文件/行数超指引**：S3 涉及解析器、diff、服务、CLI、fixtures 与测试，超过
  8 文件/400 行；按阶段 handoff 说明。

### 6.9 交接给 P1-S4

**已就绪**

- 文档 parser 的 Pydantic 模式、字段级错误与 fixture 组织方式；
- `Config` 表与 `ConfigRepository`（版本化追加写）；
- `EventRepository` 可写 `config.updated`，并强制事件时间非递减；
- 标签/桶仓储写入已做聚合校验（恰好一个主标签/filing、副标签 ≤5）；
- `DocumentError` 可作为后续 YAML/schema 错误的统一外壳。

**P1-S4 需要完成**

1. 评分 YAML 加载与校验：权重和、全局/分项权重、`wR`/`wS` 下限、`wA` 上限、
   gates 与 stability 范围；
2. JSON schema：分类、摘要、评分、事件（正反例）；
3. 配置版本创建（`Config`）与非法配置拒绝；
4. `config validate` 与必要的 CLI/测试；
5. 所有失败信息可定位到字段。

**S4 不能做**

- LLM 网关与 FakeLLM（S5）；
- 真实评分算法/队列（P5）；
- 真实文献源、摘要/分类实现（P3/P4）；
- 初始化 UI/门槛（P2）。

**实现提示**

- 权重校验先做结构校验，再做跨字段和；错误信息带配置路径；
- JSON schema 与 Pydantic 文档模型保持字段一致；
- 配置版本只追加，不覆盖历史；`config.updated` 事件写入同一事务；
- 不引入除 PyYAML 之外的 YAML/schema 运行时依赖，除非另有 ADR。

## 7. P1-S1–S3 验收返工记录

独立验收报告见 [P1-S1-S3-acceptance.md](P1-S1-S3-acceptance.md)。F1–F5 已在
P1-S3 分支内返工；F6（httpx 弃用警告）与 F7（Docker/CI 运行证据）不属于本轮
代码缺陷，继续保持为依赖/发布债务。

| ID | 状态 | 修复 | 回归测试 |
|----|------|------|----------|
| F1 | **已修复** | `confirm_taxonomy()` 在确认前调用 `ensure_taxonomy_shape()`；4/10 主标签 draft 不能确认，且不写状态/事件 | `test_confirm_taxonomy_revalidates_primary_tag_count` |
| F2 | **已修复** | 正文中 `## T06` 等“代码但无名称”的标题抛 `DocumentError`（field=`taxonomy.heading`，带行号）；`## 使用规则` 等非标签标题仍忽略 | `test_malformed_tag_heading_is_rejected` |
| F3 | **已修复** | `EventRepository.append()` 比较最后事件时间并拒绝回填；`PaperRepository.transition()` 先追加事件再改状态，失败不留半更新 | `test_event_log_is_queryable_and_append_only`、`test_transition_rejects_backdated_event_without_changing_status` |
| F4 | **已修复** | 文档 DB 子命令捕获 Pydantic `ValidationError`，复用 `format_settings_error()` 输出 `PAPERSTEWARD_*` 提示，不再打印 traceback | `test_document_db_commands_report_settings_errors`（import/confirm/archive/diff） |
| F5 | **已修复** | `PaperTagRepository` 写入后校验恰好一个主标签 + 副标签 ≤5；`PaperBucketRepository` 校验恰好一个 filing；批量写入要求同一文献 | `test_tag_repository_requires_primary_before_secondaries_and_caps_count`、`test_tag_repository_add_many_rejects_oversized_or_mixed_batch`、`test_bucket_repository_requires_filing_before_memberships` |

### 7.1 行为变化

- 仓储标签/桶写入是聚合校验：首次写标签必须是 primary，首次写桶关系必须是
  filing；非法中间态由调用方回滚。P3/P4 的 classification service 应使用
  `add_many`/`set_classification` 风格的整批写入，而不是逐条制造非法状态。
- 事件日志全局要求 `created_at` 非递减；回填旧时间会抛
  `InvariantViolation("event.created_at", ...)`。
- 分类法确认会重新校验 5–9 个主标签；不能再用仓储直接构造可确认的非法分类法。
- 格式错误的 `## Txx` 标题不再静默丢弃，validate/import 会失败并定位行号。

### 7.2 验收结果（返工后）

- `make check`：**267 passed**，ruff/mypy 通过；
- `make test-integration`：**83 passed**；
- `pytest -q`（含 e2e）：**268 passed**；
- `make migrate-check`：upgrade/downgrade/upgrade OK；
- `make e2e`：1 passed；
- `make demo`：`/health` 与 `/papers` 正常；
- `make secret-scan`：OK；
- `pytest --cov=papersteward.documents`：**96%**；
- 未新增依赖；未访问网络或真实 LLM。

验收报告中的关闭条件 1–4 已全部处理；P1-S1–S3 可交回独立验收复核。F6/F7 仍按
原报告保留。

## 8. P1-S4 执行记录

### 8.1 目标

建立可版本化、可审计的评分配置加载/校验，以及分类、摘要、评分、事件四类
Agent 输出的 JSON schema；非法配置和非法 JSON 不进入业务表。

### 8.2 交付物

| 文件 | 作用 |
|------|------|
| `src/papersteward/scoring/config.py` | scoring YAML 模型、字段/范围校验、跨字段权重和/门槛/上限 |
| `src/papersteward/scoring/service.py` | 配置版本追加写、`config.updated` 事件、读取时复验 |
| `src/papersteward/schemas/models.py` | 分类/摘要/评分快照/事件 Pydantic schema 与校验入口 |
| `src/papersteward/schemas/__init__.py` | schema 注册表导出 |
| `src/papersteward/cli/config.py` | `config validate/import`、`schema show/validate` |
| `src/papersteward/cli/main.py` | 新子命令接线 |
| `tests/fixtures/configs/*.yaml` | 合法配置、违反 wR 下限、子权重和错误 |
| `tests/unit/test_scoring_config.py` | 配置正反例、字段/行号、策略分支 |
| `tests/contract/test_json_schemas.py` | 四类 schema 正反例 |
| `tests/integration/test_scoring_service.py`、`test_cli_config.py` | 版本/事件/CLI |

### 8.3 评分配置契约

`scoring.yaml` 固定结构（设计方案 §10.5），字段范围与跨字段规则：

- 四个权重组：`static`、`dynamic`、`quasi_dynamic` 内部权重和 = 1.0；
- `static.weight + dynamic.weight + quasi_dynamic.weight = 1.0`；
- 全局策略：`static.relevance ≥ 0.50`、`static.weight ≥ 0.60`、
  `static.authority ≤ 0.20`；
- `gates.relevance_weight_floor ≥ 0.50`、`gates.static_weight_floor ≥ 0.60`、
  `gates.authority_weight_cap ≤ 0.20`，实际权重必须满足对应 gate；
- `deep_cap + skim_cap ≤ max_daily_push`、`max_per_primary_tag ≤ max_daily_push`；
- `half_life_days` 满足 `fast ≤ default ≤ slow`；
- 未知字段拒绝；`version` 可数字/字符串但统一为字符串；
- 错误带 `source:line: field: message`。

`config import` 在 `Config(key='scoring', version=...)` 追加一行，并在同一事务写入
`config.updated` 事件；重复版本由主键拒绝；读取时重新跑 schema 与跨字段校验。

### 8.4 JSON schema 契约

| schema | 关键字段/规则 |
|--------|----------------|
| `classification` | `primary_tag` 稳定代码、`primary_confidence` 0–1、副标签 ≤5（facet/name/confidence）、特殊桶候选 ≤3、`boundary_note` |
| `summary` | `summary_100` ≤100 非空白字符、what/how/finding/limitation 非空、evidence ≥1（field/quote） |
| `scoring` | paper/date/三版本；static R/A/E/S、dynamic F/H/D、quasi U/confidence、priority P/rank_score 均 0–100（confidence 0–1）；explanation ≥1 |
| `event` | `EventType`、payload object、带时区 `created_at`；`paper.*` 事件必须有 `paper_id` |

所有模型 `extra="forbid"`；`schema_for(name)` 生成标准 JSON Schema，
`schema validate` 校验 JSON 文件；S5 的 FakeLLM/修复重试复用
`validate_json_text`。

### 8.5 CLI

```bash
papersteward config validate tests/fixtures/configs/scoring.yaml
papersteward config import tests/fixtures/configs/scoring.yaml --at 2026-10-21T09:00:00Z
papersteward schema show classification
papersteward schema validate classification /tmp/classification.json
papersteward schema validate summary /tmp/summary.json
papersteward schema validate scoring /tmp/score.json
papersteward schema validate event /tmp/event.json
```

`config validate` 不写库；`config import` 追加版本并写事件；schema 校验失败退出 1，
错误路径形如 `schema.summary.evidence.0.quote`。

### 8.6 验收结果

- `make check`：**309 passed**，ruff/mypy 通过；
- `make test-integration`：**95 passed**；
- `pytest -q`（含 e2e）：**310 passed**；
- `make migrate-check`：upgrade/downgrade/upgrade OK；
- `make e2e`：1 passed；
- `make demo`：`/health` 与 `/papers` 正常；
- `make secret-scan`：OK；
- `pytest --cov=papersteward.scoring --cov=papersteward.schemas`：**99%**；
- 未新增依赖；未访问网络或真实 LLM。

### 8.7 与计划的偏差与限制

- **schema 不落静态文件**：JSON Schema 由 Pydantic 模型在运行时生成，CLI `schema
  show` 可导出；如外部系统需要冻结文件，后续可加 `schema export`。
- **不引入 `jsonschema` 依赖**：运行时用 Pydantic 校验，避免新增依赖；边界仍是
  JSON Schema 语义（`extra="forbid"`、required/enum/range）。
- **评分公式不复算**：`scoring` schema 只校验字段/范围/结构；P/S/D 公式由 P5 实现。
- **配置策略硬编码**：0.50/0.60/0.20 是设计文档约束，变更需 ADR + 测试。
- **事件类型命名空间**：schema 使用 P1-S1 的 `paper.done` 等值，而不是设计示例的
  裸 `done`，避免与文档/配置事件混淆。
- **文件数超指引**：S4 新增配置、schema、CLI、fixtures 与三层测试，超过 8 文件/
  400 行；按 handoff 说明。

### 8.8 交接给 P1-S5

**已就绪**

- `schema_for` / `validate_mapping` / `validate_json_text`，错误字段可定位；
- `FakeLLM` 可直接返回合法/非法 JSON，S5 只需调用校验入口；
- `ConfigRepository` + `read_scoring_config`，可读取当前评分版本；
- `config.updated` 事件与配置版本追加约定；
- 配置/schema 的 CLI 与正反例 fixture。

**P1-S5 需要完成**

1. `LLMProvider` 协议、`FakeLLM`、prompt registry（版本化）；
2. `complete_json` 风格的调用入口：超时/重试/修复重试/成本记录；
3. 非法 JSON 不进入业务表；修复失败返回结构化错误；
4. `llm ping --fake` 与 FakeLLM 契约测试；
5. 记录模型、prompt、输入哈希（`Provenance`）。

**S5 不能做**

- 真实网络/真实 LLM 调用（默认禁用；live 测试手动运行）；
- 摘要/分类/评分算法与队列（P4/P5）；
- 真实文献源（P3）；
- 初始化 UI/门槛（P2）。

**实现提示**

- 先校验再落库；`schemas.validate_json_text` 是唯一入口；
- 修复重试最多 1 次，失败返回 `needs_review` 或结构化错误；
- prompt registry 与配置一样版本化，变更记录 prompt_version；
- 成本记录只存可审计的数字/模型名，不记录密钥或完整 prompt 中的敏感文本。

## 9. 风险与待确认

- **DB 约束能力**：SQLite 部分唯一索引已在 0002 落地并有集成测试；跨列/跨行规则
  仍必须由服务层不变量兜底。
- **特殊桶提升**：`shelved → scored` 是状态机允许边，但服务层必须强制重算静态分并
  生成新快照后再入队；S2 的仓储接口应只暴露“重新评分后入队”的用例，而不是裸迁移。
- **归档快照字段**：P6 可能要求更多阅读元数据；届时新增列/表迁移，不改既有字段。
- **EventType 扩展**：P2 的 G1–G4 门槛事件将追加值，不改已有值；P1-S3 已追加
  `profile.archived` / `taxonomy.archived`。
- **front matter 安全**：只使用 `safe_load`，不构造自定义对象；锚点/别名可被
  YAML 展开，超大文档需在 P2 导入层限制大小。
- **taxonomy 正文解析**：当前只识别单行 `- 字段：值`，多行/嵌套说明保留在
  `content_md`；P2 若需要结构化边界案例，应扩展模型并走迁移/ADR。
- **版本字符串**：不解析 semver；`1.10` 与 `1.9` 的排序由用户输入决定，P2 需要
  版本序号时再引入明确规则。
- **F6**：Starlette/httpx 弃用警告属于依赖债务，留待依赖升级窗口处理。
- **F7**：Docker/CI 运行证据仍未在本地关闭；打发布 tag 前必须补齐。
- **schema 导出**：当前 schema 运行时生成；外部消费者若需要稳定文件，需在 P1-S5
  或后续增加 `schema export` 与版本快照。
- **配置策略**：`wR/wS/wA` 与 gates 的硬约束来自设计方案 §10.5，修改需 ADR 与
  迁移/兼容说明。
- **事件 schema 命名**：使用 `EventType` 命名空间值（如 `paper.done`），与设计示例
  的裸状态值不同；事件落库与 schema 以此为准。
