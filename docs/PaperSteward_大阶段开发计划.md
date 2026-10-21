# PaperSteward 大阶段开发计划（Coding Agent 执行版）

> **版本**：v1.0  
> **日期**：2026-10-21  
> **依据**：[PaperSteward 系统设计方案](PaperSteward_文献自动挖掘推送与优先级排序系统设计方案.md)  
> **用途**：把设计落成 9 个大阶段；每个大阶段届时拆为 0.5–2 天、可直接派发给 coding agent 的小阶段。  
> **默认假设**：单用户、本地优先、Python 3.11 + FastAPI + SQLite + HTMX/CLI、OpenAI-compatible LLM；单人 coding agent 每天有效工作 4–6 小时。

---

## 0. 执行摘要

本计划把系统开发切成 **9 个大阶段（P0–P8）**，每个阶段都以“可运行、可演示、可验收”为结束条件，而不是以“代码写完”为结束条件。

- **P0–P6 = Alpha**：跑通“初始化 → 采集 → 概括/分类/评分 → 日报 → 阅读反馈 → 归档”的核心闭环。
- **P7 = Beta**：补齐三个特殊桶、自定义排序与权重治理，达到设计方案的功能完整度。
- **P8 = Pilot**：评估、安全、备份、成本与试点发布，形成可长期运行的版本。

**核心原则**：大阶段只定义边界、契约和验收；小阶段在阶段开始前按模板即时拆分。每个小阶段必须能独立合并、独立测试、独立演示，不依赖“下一阶段再补”。

### 0.1 阶段地图

| 阶段 | 名称 | 主要产出 | 依赖 | 建议工期 | 里程碑 |
|------|------|----------|------|----------|--------|
| P0 | 工程基线与 Walking Skeleton | 可启动的 API/CLI/DB/CI/测试骨架 | — | 2–3 天 | Alpha 基础 |
| P1 | 领域模型、版本化文档与 LLM 网关 | 契约、迁移、文档版本、假 LLM | P0 | 4–6 天 | Alpha |
| P2 | 初始化协商与确认门 | G1–G4 门槛、校准集、试运行日报 | P1 | 5–7 天 | Alpha |
| P3 | 采集、去重与硬过滤 | 多源候选入库、去重、运行报告 | P1/P2 | 4–6 天 | Alpha |
| P4 | 富化、摘要、分类与静态评分 | 可审阅条目、100 字概括、R/S、证据 | P3 | 8–12 天 | Alpha |
| P5 | 优先级排序、动态分与解释 | 排序队列、分项解释、配置校验 | P4 | 5–7 天 | Alpha |
| P6 | 日报推送、阅读反馈与归档 | 日报、完成/评分/备注、归档与导出 | P5 | 6–8 天 | Alpha |
| P7 | 特殊桶、自定义排序与权重治理 | 三个特殊桶、视图模板、权重影响预览 | P5/P6 | 5–7 天 | Beta |
| P8 | 评估、硬化与试点发布 | eval、安全、备份、成本、发布清单 | P0–P7 | 5–7 天 | Pilot |

**总工期估算**：约 44–63 个有效工作日，加 20% 缓冲约 **53–76 个工作日（10–15 周）**。这是单人 coding agent 的规划区间，不是交付承诺；若两人并行，可按第 2.4 节拆分关键路径。

> 编号说明：设计文档 §16 的 P0–P3 是产品里程碑编号；本计划的 P0–P8 是工程阶段编号，二者不是同一套编号。实施与派发以本计划为准。

### 0.2 三个里程碑

| 里程碑 | 包含阶段 | 判定标准 | 可以做什么 |
|--------|----------|----------|------------|
| **Alpha** | P0–P6 | 用固定 fixture 或 1 个真实来源跑通“采集 → 概括 → 分类 → 评分 → 日报 → 完成 → 归档” | 内部试用；每天生成日报；收集第一批真实反馈 |
| **Beta** | P7 | 三个特殊桶可用；自定义排序/模板可用；特殊桶不参与 P；全局权重不被视图篡改 | 小范围试用；验证“主队列 + 特殊桶 + 自定义视图”的完整体验 |
| **Pilot** | P8 + 2 周试点观察 | 评估、备份恢复、安全、成本、运行手册全部通过；连续运行 2 周无阻断故障 | 4 周试点；决定是否进入 V2 |

> 与设计文档的对应：设计文档中的完整 MVP 功能面在 **Beta** 达到；**Alpha** 是可试用的核心闭环（不含完整特殊桶/自定义视图）。

### 0.3 一张图看完依赖

```text
P0 工程基线
   │
   ▼
P1 领域模型/文档/LLM 网关
   │
   ▼
P2 初始化协商与确认门
   │
   ▼
P3 采集/去重/硬过滤
   │
   ▼
P4 富化/摘要/分类/静态评分
   │
   ▼
P5 动态分/排序/解释
   │
   ├──────────────► P6 日报/反馈/归档 ──► P7 特殊桶/自定义视图 ──► P8 评估/硬化/发布
   │
   └──────────────► P8 的 eval harness 可在 P5 后提前启动
```

默认按顺序单线执行。若有两个 coding agent，可按第 2.4 节做有限并行，但**契约必须先冻结**。

### 0.4 目录

1. [如何使用这份计划](#1-如何使用这份计划)
2. [开发策略](#2-开发策略)
3. [大阶段明细](#3-大阶段明细)
4. [里程碑、工期与裁剪](#4-里程碑工期与裁剪)
5. [小阶段拆分协议](#5-小阶段拆分协议)
6. [测试与质量策略](#6-测试与质量策略)
7. [阶段交接与发布](#7-阶段交接与发布)
8. [开发风险与应对](#8-开发风险与应对)
9. [明确不做](#9-明确不做当前-9-个阶段)
10. [需求覆盖对照](#10-需求覆盖对照)
11. [附录 A–E](#附录-a阶段-handoff-模板)

> 锚点按常见 Markdown 渲染器生成；若编辑器不支持中文锚点，以章节号为准。

---

## 1. 如何使用这份计划

### 1.1 三层结构

| 层级 | 粒度 | 谁来写 | 例子 |
|------|------|--------|------|
| 大阶段 | 1–2 周；一个里程碑 | 本计划预先定义 | P3 采集、去重与硬过滤 |
| 小阶段 | 0.5–2 天；一个 PR | 阶段开始前按模板拆分 | P3-S3 arXiv 适配器 |
| 任务 | 0.5–4 小时；可同一 PR | 小阶段执行时再拆 | P3-S3-T1 定义 adapter 返回模型 |

**大阶段不直接派发。** 派发前必须先把当前大阶段拆成小阶段，每个小阶段满足第 1.2 节的“可直接派发”标准。

### 1.2 “可直接派发”的标准

一个小阶段必须满足 **Definition of Ready（DoR）**：

- [ ] 一句话目标明确，且有用户可见或系统可验证的结果；
- [ ] 输入、输出、接口/契约、涉及文件已写明；
- [ ] 依赖的小阶段已合并，或明确标注为 stacked PR；
- [ ] 测试 fixture / 假数据已存在或在本阶段内创建；
- [ ] 验收命令可复制执行；
- [ ] 不在范围（out of scope）已写明；
- [ ] 不需要再做产品决策；
- [ ] 预估 ≤2 天、净变更 ≤400 行、涉及文件 ≤8 个。

**Definition of Done（DoD）**：

- [ ] 代码、测试、必要文档一起提交；
- [ ] `make check` 通过；
- [ ] 新增/修改行为有对应测试；
- [ ] 演示命令可运行；
- [ ] 无回归、无未处理的 P0/P1 缺陷；
- [ ] 更新 `CHANGELOG.md`（若用户可见）；
- [ ] PR 描述包含目标、变更、测试、演示、风险、回滚；
- [ ] 阶段 handoff 中记录“能做什么、不能做什么”。

> **判定口诀**：下一个 coding agent 拿到这张卡，不需要再问“我该改哪个文件、怎么验收”，就算可直接派发。

### 1.3 编号、目录与分支约定

| 对象 | 约定 | 示例 |
|------|------|------|
| 大阶段 | `P0`–`P8` | `P3` |
| 小阶段 | `P{n}-S{m}` | `P3-S3` |
| 任务 | `P{n}-S{m}-T{k}` | `P3-S3-T1` |
| 分支 | `feat/p{n}-s{m}-{slug}` | `feat/p3-s3-arxiv-adapter` |
| 提交 | Conventional Commits | `feat(p3-s3): add arXiv adapter` |
| 阶段文档 | `docs/phases/P{n}-{slug}.md` | `docs/phases/P3-ingestion.md` |
| 决策记录 | `docs/adr/NNNN-{slug}.md` | `docs/adr/0003-use-sqlite-for-mvp.md` |
| 阶段 tag | `p{n}-done` | `p3-done` |
| 里程碑 tag | `v0.1.0-alpha` / `v0.2.0-beta` / `v0.3.0-pilot` | — |

**PR 模板**：

```markdown
## Goal
## Changes
## Contracts / migrations
## Tests
## Demo command
## Risks / rollback
## Out of scope
```

建议 issue/PR 标签：`phase:P3`、`slice:P3-S3`、`type:feature|test|docs`、`priority:P0|P1`、`blocked`。

### 1.4 开工前必须确认的决策

以下决策会影响 P0/P1 的目录与契约。**建议直接采用推荐默认值**；若不同意，在 P0 开始前改，不要等到 P4 再改。

| # | 决策 | 推荐默认 | 备选 | 影响 |
|---|------|----------|------|------|
| 1 | UI 形态 | CLI + FastAPI + Jinja2/HTMX 服务端页面 | 纯 API/CLI；React/Next.js | React 会显著增加 P2/P6/P7 工期 |
| 2 | LLM 接入 | OpenAI-compatible API + `FakeLLM` | 本地模型；多 provider 路由 | 影响 P1 LLM 网关与测试策略 |
| 3 | Embedding | API embedding，接口可插拔 | 本地 bge-m3 | 影响 P4 相关度与离线运行 |
| 4 | 推送渠道 | 本地 Markdown 日报 + SMTP 邮件 | IM Webhook；仅 Web | 邮件需要 SMTP 配置；IM 需要额外适配 |
| 5 | 部署 | Docker Compose，单机，绑定 `127.0.0.1` | 云服务器；Postgres | MVP 不做多用户/鉴权 |
| 6 | 数据库 | SQLite + Alembic；预留 Postgres 兼容 | 直接 Postgres | SQLite 降低本地部署门槛 |
| 7 | 语言/时区 | 中文 UI 与文档；英文代码标识；`Asia/Shanghai` | 英文 UI；UTC | 影响文案、时间显示与 fixture |
| 8 | 种子数据 | 用户提供 20 篇，或使用合成 fixture 先跑通 | 仅用真实数据 | 没有种子会阻塞 P2 校准与 P4 golden |

### 1.5 执行节奏

| 时点 | 动作 | 产出 |
|------|------|------|
| 大阶段开始前 | 按第 5 章把本阶段拆成 5–8 个小阶段卡片 | `docs/phases/P{n}-{slug}.md` 的任务表 |
| 每个工作日 | 完成 1–2 个小阶段；`make check`；更新任务状态 | 1–2 个 PR |
| 每周五 | 跑 `make e2e`/`make demo`；回顾阻塞与范围 | 周报、调整下周卡片 |
| 大阶段结束 | 跑阶段门清单；写 handoff；打 tag | `p{n}-done`、下一阶段输入 |

> 小阶段卡片不是一次性写死的；执行中发现依赖/风险，可以拆得更细，但不能跳过 DoR/DoD。

---

## 2. 开发策略

### 2.1 六条工程原则

1. **Contract-first**：先定 schema/接口，再写实现；跨阶段依赖只通过契约耦合。
2. **Vertical slice**：每个小阶段尽量交付“一条可演示的竖切”，而不是只改一层。
3. **Fake-first**：测试默认使用 `FakeLLM`、HTTP cassettes、固定 fixture；CI 不依赖外网。
4. **CLI/API before UI**：先让能力可通过命令/接口验证，再做页面；页面只做“调用 + 展示”。
5. **Feature flag + 可逆迁移**：未完成的能力用配置开关隐藏；数据库迁移必须可回滚/可恢复。
6. **Docs-as-code**：每个阶段有 handoff、ADR、CHANGELOG；不把关键决策留在聊天记录里。

### 2.2 技术基线

| 层 | 选型 | 说明 |
|----|------|------|
| 语言 | Python 3.11+ | 类型标注、Pydantic v2 |
| API | FastAPI + Pydantic v2 | OpenAPI 自动生成 |
| ORM/迁移 | SQLAlchemy 2.x + Alembic | 迁移纳入 CI |
| 数据库 | SQLite（MVP） | 单文件、易备份；避免 Postgres 专属语法 |
| 调度 | APScheduler | 单机；不引入 Celery/Redis |
| HTTP | httpx | 超时、重试、限流 |
| LLM | LiteLLM 或自建 provider 适配层 | 统一 `model_id`、成本记录、fake provider |
| 前端 | Jinja2 + HTMX | 不引入 Node 构建链 |
| 测试 | pytest + pytest-cov | 单元/契约/集成/E2E |
| 质量 | ruff + mypy + pre-commit | 格式、lint、类型 |
| 部署 | Docker Compose | 数据卷持久化；localhost |

**MVP 明确不用**：Celery/Redis、Postgres、React/Next.js、多用户鉴权、PDF 全文解析、向量数据库集群。它们不是“永远不用”，而是不进当前 9 个阶段。

### 2.3 推荐仓库结构

```text
papersteward/
  pyproject.toml
  Makefile
  docker-compose.yml
  .env.example
  AGENTS.md
  README.md
  CHANGELOG.md
  docs/
    design/                 # 设计方案
    phases/                 # 每个大阶段的执行文档与 handoff
    adr/                    # 架构决策记录
  src/papersteward/
    main.py
    config.py
    core/                   # 实体、枚举、状态机、不变量
    storage/                # DB、迁移、repositories
    documents/              # profile/taxonomy 解析、版本、diff
    llm/                    # gateway、prompts、schemas、fake
    sources/                # arxiv/openalex/s2 适配器
    pipeline/               # dedupe/screen/enrich/summarize/classify/special
    scoring/                # static/dynamic/quasi/ranker/explain
    delivery/               # digest/render/push/archive
    api/                    # 路由
    cli/                    # 命令行
    jobs/                   # 每日/每周任务
  tests/
    unit/
    contract/
    integration/
    e2e/
    fixtures/               # papers/sources/llm/profiles/taxonomies/expected
```

### 2.4 质量门与并行策略

**每个 PR 必须通过**：

```bash
make check          # ruff format --check + ruff check + mypy + pytest
make test-integration
make migrate-check
```

**每个阶段结束必须通过**：

```bash
make e2e
make demo
```

**覆盖率目标**（指导值，不为了数字写无意义测试）：

- `core/`、`scoring/`、`documents/`：≥90%；
- 整体：≥75%；
- 关键路径（评分、摘要校验、归档、门槛）必须有 golden/回归测试。

**并行策略**：

- 单人：严格 P0→P8 顺序，P4 内部按 2.1 的竖切顺序。
- 双人：P2 与 P3 适配器可并行（P3 用 fixture 和 P1 契约）；P4 的 enrichment 与 summary/classification 可并行；P6 的 UI 与 digest 可并行；但 **P1 契约、P4 schema、P5 评分公式必须先冻结**。
- 不并行：P5 排序与 P6 日报的验收依赖同一份队列契约；P7 特殊桶依赖 P5 的“特殊不参与 P”规则。

### 2.5 契约冻结点

| 阶段完成后冻结 | 内容 | 为什么 |
|----------------|------|--------|
| P1 | 领域实体、状态机、`Profile`/`Taxonomy`/`Paper`/`Summary`/`ScoreSnapshot` schema | 后续所有阶段都依赖 |
| P2 | onboarding API、G1–G4 状态、确认事件 | 防止绕过初始化门 |
| P3 | `SourceAdapter` 接口、`Paper` canonical 字段、去重结果 | 适配器与 pipeline 的边界 |
| P4 | `Summary`、`Classification`、`RelevanceResult`、`StaticScore` schema | 评分与日报依赖 |
| P5 | `QueueItem`、`ScoreExplanation`、`RankConfig` | 自定义视图与日报依赖 |
| P6 | `Digest`、`Action`、`ArchiveRecord` | 反馈与导出依赖 |
| P7 | `ViewConfig`、`SpecialShelf`、`Tier` | 发布前稳定 |
| P8 | 公共 API/CLI 参数、配置格式 | 试点兼容 |

> 冻结不等于永远不改；变更必须走 ADR + 迁移 + 兼容期，不能在某个小阶段里“顺手改字段”。

### 2.6 Coding Agent 工作规则

1. **先读**：设计文档 → 本计划 → 当前阶段文档 → `AGENTS.md` → 上一个阶段 handoff。
2. **只做当前小阶段**：不顺手重构、不扩大范围、不提前实现下一阶段。
3. **契约先行**：先改 schema/接口/测试，再改实现；跨模块改动先写 ADR。
4. **测试同交**：没有测试的行为不算完成；测试不使用真实网络/真实 LLM。
5. **一个 PR 一个小阶段**：分支、提交、PR 标题都带 `P{n}-S{m}`。
6. **卡住就停**：遇到产品歧义、契约冲突、外部 API 不可用，写 blocker note，不臆造行为。
7. **可回滚**：迁移、配置、prompt 都要有回滚方式；未完成能力用 feature flag。
8. **不提交秘密**：API key 只进 `.env`；`.env.example` 只放占位符。
9. **控制 diff**：超过 400 行/8 个文件就拆；确实需要更大，PR 里说明理由。
10. **阶段收尾**：更新 handoff、CHANGELOG、阶段 tag；给下一阶段留下“可直接开始”的状态。

---

## 3. 大阶段明细

> 以下每个阶段都按同一结构：目标 → 演示 → 范围 → 交付物 → 工作包 → 验收 → 测试 → 风险/回滚 → 交接。  
> **工作包不是最终小阶段清单**，而是拆分方向；阶段开始前再按第 5 章模板拆成 5–8 个小阶段。

### P0 工程基线与 Walking Skeleton

**里程碑**：Alpha 基础｜**依赖**：无｜**建议工期**：2–3 天｜**主要模块**：`main/ config/ storage/ api/ cli/ tests/`

**目标**：新机器 clone 后 10 分钟内跑起 API、CLI、数据库、迁移和测试；建立后续所有阶段的工程约束。

**演示**：

```bash
make bootstrap
make check
make demo
curl http://127.0.0.1:8000/health
docker compose up --build
```

**范围内**：

- `pyproject.toml`、包结构、`Makefile`、`.env.example`、`.gitignore`、`README.md`、`AGENTS.md`、`CHANGELOG.md`；
- FastAPI app、`/health`、统一错误响应、结构化日志；
- SQLAlchemy + Alembic 初始化，SQLite 连接，临时测试库；
- 最小 `papers` 表（id/title/status/created_at）与一个 `runs` 表，够跑通 health → repo → DB；
- CLI `papersteward doctor`、`papersteward demo seed`；
- pytest/fixture/fake clock/fake ID 工具；
- Docker Compose、CI（lint/typecheck/test）。

**范围外**：

- 真实文献源、LLM prompt、评分、日报、UI 页面；
- 多用户、鉴权、Postgres、Celery。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 可启动 API | `curl /health` 返回版本与 DB 状态 |
| 可运行 CLI | `papersteward doctor` 检查配置/DB/迁移 |
| 迁移骨架 | `alembic upgrade head` + `alembic downgrade base` |
| 测试骨架 | 单元/集成各至少 1 个，CI 通过 |
| 演示数据 | `demo seed` 插入 fixture paper，`GET /papers` 可见 |
| 工程文档 | `README` 快速开始、`AGENTS.md` 工作规则 |

**工作包（后续拆小阶段）**：

1. `P0-S1` 包结构、配置、日志、错误响应；
2. `P0-S2` DB 连接、Alembic、临时测试库；
3. `P0-S3` API 骨架、health、papers stub；
4. `P0-S4` CLI 骨架、demo seed；
5. `P0-S5` Makefile、Docker、CI、AGENTS/README。

**验收标准**：

- [ ] 全新 clone 后按 README 一次成功；
- [ ] `make check` 通过；
- [ ] `make demo` 可复现；
- [ ] `docker compose up` 健康检查通过；
- [ ] 无真实 API key 也能跑测试；
- [ ] 数据库迁移可上可下。

**测试要求**：config 解析、health、repo CRUD、迁移；CI 不联网。

**风险/回滚**：P0 容易过度设计。只建“能跑通”的骨架，不提前做 P1 的领域模型；所有 stub 表在 P1 用迁移替换。

**交接给 P1**：仓库结构、配置读取、DB 连接、迁移命令、测试 fixture 约定、`AGENTS.md`。

---

### P1 领域模型、版本化文档与 LLM 网关

**里程碑**：Alpha｜**依赖**：P0｜**建议工期**：4–6 天｜**主要模块**：`core/ storage/ documents/ llm/`

**目标**：系统拥有稳定的领域模型、可版本化的 profile/taxonomy 文档、可校验的配置，以及可替换的 LLM 网关。P1 结束后，后续阶段不再“边写边改核心表”。

**演示**：

```bash
papersteward profile validate tests/fixtures/profiles/basic.md
papersteward taxonomy validate tests/fixtures/taxonomies/basic.md
papersteward doc diff profile v1 v2
papersteward llm ping --fake
```

**范围内**：

- 实体：Profile、Taxonomy、Tag、Bucket、Paper、PaperTag、PaperBucket、Summary、ScoreSnapshot、Event、Rating、Config、Run；
- 状态机与不变量：每篇一个主标签/一个归属桶；静态分冻结；事件追加写；归档快照不可变；
- 仓储接口、Alembic 迁移、事件日志；
- Markdown + YAML front matter 解析、版本、diff、确认状态；
- 评分 YAML 加载与校验（权重和、门槛、上限）；
- JSON schema：分类、摘要、评分、事件；
- LLM 网关：provider 接口、`FakeLLM`、prompt registry、schema 校验/修复、超时/重试、成本记录。

**范围外**：

- 初始化 UI/流程编排（P2）；
- 文献源、摘要/分类实现（P3/P4）；
- 评分算法（P5）。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 领域模型与迁移 | 建库、迁移、约束测试 |
| 文档解析/版本 | `profile/taxonomy validate`、`diff` |
| 配置校验 | 非法权重被拒绝；合法配置生成版本 |
| LLM 网关 | fake 返回合法/非法 JSON，均能被正确处理 |
| 契约测试 | schema 正反例；迁移 up/down |

**工作包（后续拆小阶段）**：

1. `P1-S1` 实体、枚举、状态机、不变量；
2. `P1-S2` 迁移、仓储、事件日志；
3. `P1-S3` profile/taxonomy 解析、版本、diff；
4. `P1-S4` 配置加载/校验、JSON schema；
5. `P1-S5` LLM 网关、fake provider、prompt registry、修复重试；
6. `P1-S6` 契约/迁移/不变量测试。

**验收标准**：

- [ ] 每篇文献恰好一个主标签、一个归属桶的约束在 DB/服务层双重校验；
- [ ] 非法 profile/taxonomy 被拒绝，错误信息可定位到字段；
- [ ] 版本 diff 可读，确认事件写入 event log；
- [ ] 非法 LLM JSON 不会进入业务表；
- [ ] 迁移可上可下，测试库隔离；
- [ ] 所有核心 schema 有正反例测试。

**测试要求**：不变量单元测试；Alembic 集成测试；schema 契约测试；FakeLLM 测试。

**风险/回滚**：领域模型容易“先写宽再收窄”。P1 结束前做一次 schema review，冻结 2.5 节的契约；后续字段变更走迁移 + ADR。

**交接给 P2**：实体/仓储 API、文档解析器、配置格式、LLM 网关、schema、迁移基线。

---

### P2 初始化协商与确认门

**里程碑**：Alpha｜**依赖**：P1｜**建议工期**：5–7 天｜**主要模块**：`documents/ api/ cli/ onboarding`

**目标**：用户能完成“研究方向档案 → 主标签分类法 → 校准集 → 试运行”的初始化；系统在 G4 之前绝不自动推送。

**演示**：

```bash
papersteward onboarding start --seed tests/fixtures/seeds.json
papersteward onboarding profile edit
papersteward onboarding taxonomy validate
papersteward onboarding calibrate
papersteward onboarding dry-run
papersteward onboarding confirm --gate g4
```

**范围内**：

- onboarding 状态机：P0 种子 → P1 档案 → G1 → P2 分类法 → G2 → P3 校准 → G3 → P4 参数/试运行 → G4；
- 档案/分类法草案、编辑、确认、版本 diff；
- 种子文献导入、分类法覆盖率/均衡性校验、影响预览；
- 校准集：15–20 篇 fixture/真实文献，Agent 标注 vs 用户标注，Spearman/Top-5 报告；
- 试运行日报（只展示、不推送）；
- 门槛强制：未过 G4 不得入队/推送；
- 最小 CLI/HTMX 页面。

**范围外**：

- 真实采集、摘要、评分（P3–P5）；
- 复杂权限/多用户。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| onboarding 状态机 | 状态迁移测试；跳过门槛被拒绝 |
| 档案/分类法编辑确认 | CLI/API 走通；版本 diff 可读 |
| 分类法校验 | 覆盖率/单桶占比/边界案例报告 |
| 校准报告 | 指标、争议案例、调整建议 |
| 试运行日报 | 用 fixture 生成，不发送 |
| 门槛 e2e | G4 前 push/enqueue 返回明确错误 |

**工作包（后续拆小阶段）**：

1. `P2-S1` onboarding 状态机与 G1–G4 门槛；
2. `P2-S2` 档案草案/编辑/确认；
3. `P2-S3` 分类法草案/校验/确认/影响预览；
4. `P2-S4` 种子导入与校准集 harness；
5. `P2-S5` 试运行日报 + 门槛 e2e；
6. `P2-S6` 最小 UI/CLI 体验。

**验收标准**：

- [x] 用户不确认 G1/G2，系统不能进入下一步；
- [x] 分类法不满足 5–8 个主标签/覆盖率 ≥90%/单桶 ≤50% 时，明确提示而非静默通过；
- [x] 校准报告能定位“档案问题”还是“rubric 问题”；
- [x] G4 前 enqueue/push 均被拒绝；
- [x] 试运行日报可被用户标注“太多/太少/不准/分类不对”。

**测试要求**：状态机单元测试；门槛 e2e；文档校验正反例；校准指标 golden。

**风险/回滚**：初始化流程容易做成“大而全的表单”。优先 CLI/最小页面；复杂交互放到 P7 之后。若用户没有种子，使用合成 fixture 完成流程，校准指标标注“仅流程验证”。

本地验收：2026-10-22，详见 [P2 验收报告](phases/P2-acceptance.md)。Docker/CI 本轮未验证，未打阶段 tag。

**交接给 P3**：已确认的 profile/taxonomy、门槛状态、校准 fixture、试运行日报样例。

---

### P3 采集、去重与硬过滤

**里程碑**：Alpha｜**依赖**：P1/P2｜**建议工期**：4–6 天｜**主要模块**：`sources/ pipeline/dedupe/ pipeline/screen/ jobs/`

**目标**：每天从 2–3 个来源拉取候选，规范化为 canonical `Paper`，去重、硬过滤，并产出可审计的运行报告。

**演示**：

```bash
papersteward ingest --source arxiv --fixture tests/fixtures/sources/arxiv.json
papersteward ingest --source openalex --query "retrieval augmented generation" --dry-run
papersteward runs show --last
```

**范围内**：

- `SourceAdapter` 接口、统一元数据模型、来源 provenance；
- arXiv、OpenAlex、Semantic Scholar 适配器（至少 2 个，建议 3 个）；
- httpx 客户端：超时、重试、限流、User-Agent、缓存；
- 从 profile 生成关键词/布尔/关注列表查询；
- 去重：DOI、arXiv ID、标题模糊、作者+年份、摘要近似；
- 硬过滤：语言、日期、类型、排除词、predatory、已归档/已拒绝；
- 运行报告：请求数、新增、去重、过滤、错误、耗时；
- fixture/cassette 测试；live smoke 可选。

**范围外**：

- 摘要、分类、评分（P4/P5）；
- 引文网络可视化、全文 PDF（V2）。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 适配器接口 + 2–3 个实现 | fixture 测试；live smoke 可选 |
| 查询生成器 | 给定 profile 输出稳定查询 |
| 去重器 | 5 类重复案例全部命中 |
| 硬过滤器 | 语言/日期/排除词/黑名单 |
| 运行报告 | `runs show` 可读；失败有原因 |
| 幂等 ingest | 同一天重跑不产生重复 |

**工作包（后续拆小阶段）**：

1. `P3-S1` `SourceAdapter` 契约 + fixture harness；
2. `P3-S2` 查询生成器；
3. `P3-S3` arXiv 适配器；
4. `P3-S4` OpenAlex 适配器；
5. `P3-S5` Semantic Scholar 适配器（可后置）；
6. `P3-S6` canonicalization + 去重；
7. `P3-S7` 硬过滤 + 运行报告 + `ingest` CLI e2e。

**验收标准**：

- [ ] 同一 fixture 重跑结果稳定；
- [ ] 5 类重复案例全部去重；
- [ ] 硬过滤命中原因可追溯；
- [ ] CI 不访问真实 API；
- [ ] 失败来源不阻断其他来源，运行报告标记 `partial`；
- [ ] 新文献的 provenance（来源、URL、抓取时间）完整。

**测试要求**：适配器 contract tests（cassette）、去重单元测试、过滤规则表驱动测试、ingest 集成测试。

**风险/回滚**：来源 API 字段/限流变化。每个适配器隔离在 `sources/`，失败只影响该来源；保留 raw metadata 以便重放。

**交接给 P4**：canonical `Paper`、来源/provenance、去重后的候选池、运行报告。

---

### P4 富化、摘要、分类与静态评分

**里程碑**：Alpha｜**依赖**：P3｜**建议工期**：8–12 天｜**主要模块**：`pipeline/enrich/ summarize/ classify/ special/ scoring/static/`

**目标**：把候选文献变成“可审阅、可分类、可静态评分”的条目：补全元数据 → 相关度 R → 100 字概括 → 主/副标签 → 特殊桶提名 → 权威/额外价值/静态分。

> **这是最大的阶段**，预计拆 6–8 个小阶段，**不要作为一个 PR 派发**。拆分顺序建议：enrichment → R → summary → classification → special → static score → review queue。

**演示**：

```bash
papersteward process --paper tests/fixtures/papers/p001.json --fake-llm
papersteward show p001 --fields summary,tags,r_score
papersteward review list
```

**范围内**：

- 富化：作者/团队、venue 等级、引用、代码/数据、OA 状态、实验质量线索；
- 相关度 R：语义相似度 + 四维 rubric + 证据 + 校准；
- 100 字概括：三槽、字符校验、证据、失败重试；
- 分类：主标签/副标签、置信度、低置信度转 `needs_review`；
- 特殊桶提名：review/distant/classic，附理由；远邻附桥接假设；
- 静态分：A/E/S、快照、版本；
- 人工确认队列（低置信度/特殊提名/边界案例）。

**范围外**：

- 动态排序、日报、UI 完整页面（P5/P6）；
- 特殊桶的配额/展示（P7）。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 富化 provider | 缺失字段用中性值；来源可追溯 |
| R 评分 | golden 案例；证据字段完整 |
| 摘要生成/校验 | 100% 长度合规；非法摘要不落库 |
| 分类器 | 主标签唯一；低置信度进 review |
| 特殊提名 | 三类 criteria；远邻有 bridge memo |
| 静态分 | 确定性计算；快照含版本 |
| 审核队列 | 可列出/通过/改标签 |

**工作包（后续拆小阶段）**：

1. `P4-S1` 富化数据模型与 provider；
2. `P4-S2` 相关度 R（embedding + rubric + 证据）；
3. `P4-S3` 摘要生成 + 100 字/证据校验；
4. `P4-S4` 分类 + 置信度 + 边界案例；
5. `P4-S5` 特殊桶提名 + 桥接假设；
6. `P4-S6` 静态分 A/E/S + 快照；
7. `P4-S7` 审核队列 + `process` CLI e2e。

**验收标准**：

- [ ] 摘要长度合规率 100%，三槽字段非空；
- [ ] 数字/结论有证据；无证据的 claim 被标记；
- [ ] 主标签唯一；低置信度不进入主队列；
- [ ] 特殊桶提名有理由/证据；远邻有可证伪的桥接假设；
- [ ] 静态分对同一输入/配置可复现；
- [ ] 缺失元数据用中性值，不惩罚文献；
- [ ] 失败文献进入 review，不静默丢失。

**测试要求**：R/摘要/分类 golden fixtures；FakeLLM 正反例；静态分纯函数测试；审核队列集成测试；prompt 版本快照。

**风险/回滚**：LLM 输出不稳定。用 `FakeLLM` + 严格 schema + 修复重试 + golden；prompt 版本化，可回滚到上一版；摘要/分类失败不推送。

**交接给 P5**：`Summary`、`Classification`、`RelevanceResult`、`StaticScore`、审核队列、golden fixtures。

---

### P5 优先级排序、动态分与解释

**里程碑**：Alpha｜**依赖**：P4｜**建议工期**：5–7 天｜**主要模块**：`scoring/ api/ cli/`

**目标**：把静态分、每日动态分、准动态分合成为可解释的 P/rank_score，输出按优先级排序的队列；特殊桶文献不参与 P。

**演示**：

```bash
papersteward queue --date 2026-10-21
papersteward score explain p001
papersteward scores snapshot --date 2026-10-21
```

**范围内**：

- D/F/H：新鲜度、饥饿/饱和度；目标占比映射；
- U 聚合：评分亲和度、置信度收缩、冷启动中性 50；无评分数据时中性；
- P：分层权重 + 组件权重；配置校验（和=1、R≥0.5、S≥0.6、A≤0.2）；
- rank_score：EMA、单日变化上限、同分排序；
- 分数快照、解释 payload、队列 API/CLI；
- 特殊桶标记为 `excluded_from_priority`。

**范围外**：

- 日报/推送/归档（P6）；
- 自定义视图/权重预览（P7）；
- 文本偏好信号审核、真实评分数据调优（P8）。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 动态分 D | 固定日期/阅读历史 golden |
| 综合分 P | 与设计文档示例一致 |
| rank_score | EMA/上限/同分规则 |
| 配置校验 | 非法权重拒绝保存 |
| 解释 | 分项、权重、贡献、置信度、版本 |
| 队列 | 只含合格候选；特殊桶不参与 |

**工作包（后续拆小阶段）**：

1. `P5-S1` D/F/H；
2. `P5-S2` P + rank_score + tie-break；
3. `P5-S3` U 聚合/置信度收缩/冷启动；
4. `P5-S4` 配置校验与 guardrails；
5. `P5-S5` 分数快照 + 解释 + queue API；
6. `P5-S6` golden/e2e。

**验收标准**：

- [ ] 设计文档 10.10 的示例可复现（75.1）；
- [ ] 无评分/无阅读时 U/H 中性；
- [ ] 缺失 A/E 不导致负分；
- [ ] 特殊桶文献不出现在 P 队列；
- [ ] 权重非法时拒绝保存并给出原因；
- [ ] 每个队列项可展开看到分项与权重。

**测试要求**：评分纯函数表驱动测试；配置校验正反例；EMA/tie-break；队列 e2e；快照 golden。

**风险/回滚**：公式漂移。以设计文档 §10 为准；所有公式参数进配置；加 golden；变更走 ADR + 配置版本。

**交接给 P6**：`QueueItem`、`ScoreExplanation`、`RankConfig`、队列 API、golden 分数。

---

### P6 日报推送、阅读反馈与归档

**里程碑**：Alpha｜**依赖**：P5｜**建议工期**：6–8 天｜**主要模块**：`delivery/ api/ cli/ jobs/`

**目标**：每天生成一份可执行的日报；用户可标记完成、打分、写备注；完成即归档，保留标签/主观分/备注/评分快照。

**演示**：

```bash
papersteward digest --date 2026-10-21 --out out/digest.md
papersteward done p001 --mode deep --score 4 --note "可复用到 S1"
papersteward archive list
papersteward archive export --format bibtex --out out/archive.bib
```

**范围内**：

- 日报编排：主队列上限、单标签上限、多样性、饥饿槽、特殊桶占位（P7 完整）；
- 精读/粗读建议规则与配额；
- 渲染 Markdown/HTML；推送文件/SMTP；幂等键；
- 用户操作：done、score、note、snooze、skip、reject；
- 评分写入与周度 U 聚合任务触发（调用 P5 实现）；
- 事件日志、归档快照、导出；
- 最小今日/队列/详情/归档页面（HTMX）。

**范围外**：

- 特殊桶完整档位/配额（P7）；
- 自定义排序/权重预览（P7）；
- 真实评分数据调优、文本偏好信号审核（P8）。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 日报编排 | 固定队列生成稳定 digest |
| 推送 | 文件/邮件；重复日期不重发 |
| 操作 API | done/score/note/snooze/skip/reject |
| 归档 | 标签/主观分/备注/评分快照齐全 |
| 导出 | BibTeX/CSV/Markdown |
| 最小页面 | 可完成一次阅读闭环 |

**工作包（后续拆小阶段）**：

1. `P6-S1` 日报编排规则；
2. `P6-S2` Markdown/HTML 渲染；
3. `P6-S3` 推送渠道 + 幂等；
4. `P6-S4` 用户操作 API + event log + 评分写入；
5. `P6-S5` 归档快照 + 导出；
6. `P6-S6` 最小 UI + 阅读闭环 e2e；
7. `P6-S7` 周度 U 任务触发 + 评分数据检查。

**验收标准**：

- [ ] 主队列 7 篇、特殊桶 ≤2、精读 ≤2/粗读 ≤5；
- [ ] 单标签 ≤2；候选足够时至少 2 个标签；
- [ ] 同一日期重复运行不重复推送；
- [ ] done 后归档字段完整，标签/分数/备注不可静默丢失；
- [ ] 归档不可变；备注修订有历史；
- [ ] 周度 U 任务可用合成评分运行，且不回写已归档 P；
- [ ] 导出包含标签、评分、备注与链接。

**测试要求**：digest 编排表驱动；渲染 golden；幂等测试；action→archive e2e；导出快照。

**风险/回滚**：UI/推送范围膨胀。先文件日报 + CLI，再 SMTP/HTMX；邮件失败不阻断 Web/文件。归档字段变更走迁移。

**交接给 P7**：日报/队列 UI、归档、操作事件、digest golden、特殊桶占位点。

---

### P7 特殊桶、自定义排序与权重治理

**里程碑**：Beta｜**依赖**：P5/P6｜**建议工期**：5–7 天｜**主要模块**：`delivery/special/ scoring/ranker/ api/ config/`

**目标**：补齐三个特殊桶（跨层综述/远邻/经典）与自定义排序；特殊桶不参与 P，桶内只分高/中/低；自定义视图不改全局权重。

**演示**：

```bash
papersteward shelf review
papersteward shelf distant --explain
papersteward view preview --preset reproducible
papersteward view save --name "my-repro" --weights R:35,E:40,F:15,A:10
papersteward config preview --set static.relevance=0.60
```

**范围内**：

- 特殊桶提名审核、档位 rubric、配额/轮换、桥接假设展示；
- 特殊桶视图/API；`excluded_from_priority` 强制执行；
- 自定义视图：勾选 R/A/E/F/H/U、权重归一化、预设模板、保存/加载；
- 权重变更影响预览、配置版本、确认；
- 特殊桶提升/降级与主标签保留。

**范围外**：

- 新的采集/摘要/评分算法；
- 多用户/权限。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| 三特殊桶 | 内容、档位、理由、配额 |
| 特殊桶不评分 | 不出现在 P 队列；无 P 字段 |
| 自定义视图 | 预设/自定义权重可重排 |
| 权重预览 | 改权重的 Top-N 影响可见 |
| 配置版本 | 变更需确认；视图不改全局 |

**工作包（后续拆小阶段）**：

1. `P7-S1` 特殊桶数据模型/提名审核；
2. `P7-S2` 档位 rubric/配额/轮换；
3. `P7-S3` 特殊桶视图/展示；
4. `P7-S4` 自定义视图/预设/保存；
5. `P7-S5` 权重影响预览/版本/确认；
6. `P7-S6` 特殊桶 + 自定义视图 e2e。

**验收标准**：

- [ ] 三个特殊桶都有内容/档位/理由；
- [ ] 特殊桶文献不出现 P/rank_score；
- [ ] 自定义视图只改当前排序，不改全局配置；
- [ ] 全局权重变更需确认 + 版本 + 预览；
- [ ] 特殊桶可提升到主题桶并重新评分；
- [ ] 远邻必须有桥接假设，经典必须有奠基证据。

**测试要求**：档位阈值表驱动；特殊桶排除 P；视图权重归一化；配置预览 golden；e2e。

**风险/回滚**：P7 范围较大。若时间紧，先做“特殊桶 + 默认视图”，自定义视图后置；不得为了视图牺牲“特殊桶不参与 P”的规则。

**交接给 P8**：特殊桶/视图/权重治理、Beta 功能完整度、已知缺口。

---

### P8 评估、硬化与试点发布

**里程碑**：Pilot｜**依赖**：P0–P7｜**建议工期**：5–7 天｜**主要模块**：`tests/eval/ observability/ docs/ jobs/`

**目标**：让系统“可测量、可恢复、可安全运行”，完成 4 周试点前的发布检查。

**演示**：

```bash
make eval
make backup
make restore-drill
make pilot-check
```

**范围内**：

- 离线 eval：摘要长度/忠实性抽检、R 相关性、分类一致率、评分 golden、digest golden；
- 偏好模型质量：合成评分回归、真实评分覆盖率、U 对排序的影响；
- 运行指标：成功率、耗时、成本、队列深度、无关推送率；
- 安全/隐私：localhost 绑定、密钥管理、备注外发授权/脱敏、prompt injection 测试；
- 备份/恢复演练、迁移回滚；
- 成本预算/降级；
- README/quickstart/runbook/known issues/pilot checklist；
- 发布 tag。

**范围外**：

- 新功能；V2（PDF、引文图、Zotero、多用户、本地模型、移动端）。

**交付物**：

| 交付物 | 验收方式 |
|--------|----------|
| eval harness | `make eval` 可重复 |
| 指标报告 | 成功率/成本/无关率/覆盖 |
| 安全清单 | 无秘密、localhost、脱敏、注入测试 |
| 备份/恢复 | 演练通过，RTO/RPO 记录 |
| 运行手册 | 启动/排障/回滚/备份 |
| 发布包 | tag + release notes + pilot checklist |

**工作包（后续拆小阶段）**：

1. `P8-S1` eval harness + golden 集；
2. `P8-S2` 指标/日志/运行报告；
3. `P8-S3` 安全/隐私/prompt injection；
4. `P8-S4` 备份/恢复/迁移回滚；
5. `P8-S5` 成本/性能/降级；
6. `P8-S6` 文档/发布/试点清单。

**验收标准**：

- [ ] 设计文档 14.2 的验收清单全部有证据；
- [ ] `make eval` 通过设定阈值；
- [ ] 备份可在 30 分钟内恢复；
- [ ] 无秘密入库；备注默认不外发；
- [ ] 预算超限有降级与通知；
- [ ] 试点期连续 2 周运行无阻断故障（Pilot 出口条件，可跨出 P8 工期）；
- [ ] pilot checklist 完成。

**测试要求**：eval、恢复演练、安全扫描、成本回归、pilot dry run。

**风险/回滚**：硬化容易无限扩张。优先“安全、恢复、成本、运行手册”；评估指标可先覆盖核心 6 项，完整 eval 放 V1.1。

**交接给试点**：发布包、runbook、已知问题、试点指标基线、V2 backlog。

---

## 4. 里程碑、工期与裁剪

### 4.1 工期汇总

| 阶段 | 建议工期 | 累计（中值） |
|------|----------|--------------|
| P0 | 2–3 天 | 3 |
| P1 | 4–6 天 | 8 |
| P2 | 5–7 天 | 14 |
| P3 | 4–6 天 | 19 |
| P4 | 8–12 天 | 29 |
| P5 | 5–7 天 | 35 |
| P6 | 6–8 天 | 42 |
| P7 | 5–7 天 | 48 |
| P8 | 5–7 天 | 54 |

加 20% 缓冲：约 65 个有效工作日。若每天 4–6 小时有效编码，约 10–15 周。**不要把估算当承诺**；P4 和 P8 是最容易超期的两个阶段。以上不含 4 周试点观察与用户评审等待时间。

### 4.2 如果进度落后，按这个顺序裁剪

| 优先级 | 可裁剪项 | 代价 |
|--------|----------|------|
| 1 | P7 自定义视图缩为“一个默认视图 + 预设模板” | 灵活性下降，但核心闭环不受影响 |
| 2 | P6 邮件推送缩为“本地 Markdown 文件” | 需要手动打开，但可先试用 |
| 3 | P3 只保留 2 个来源（arXiv + OpenAlex） | 召回略降 |
| 4 | P8 eval 缩为 6 项核心指标 + 抽检 | 评估不够全面 |
| 5 | P8 安全/备份不能裁 | 这是长期运行底线 |

**绝对不能裁**：

- 初始化 G1–G4 门槛；
- 相关度门槛与摘要校验；
- 特殊桶不参与 P；
- 归档快照与主观分/备注；
- 评分可解释与配置版本；
- 备份/恢复。

### 4.3 需要用户参与的节点

| 时点 | 用户需要做什么 | 阻塞什么 |
|------|----------------|----------|
| P2 中 | 确认研究方向档案/分类法；完成 15–20 篇校准 | 无法进入自动采集/推送 |
| P4 后 | 抽检 20 篇摘要/分类；给相关性反馈 | 无法校准 R/分类 |
| P6 后 | 审阅第一份日报；确认阅读闭环 | 无法进入 Beta |
| P8 前 | 确认试点范围、推送时间、隐私选项 | 无法发布 |

---

## 5. 小阶段拆分协议

### 5.1 拆分规则

1. **按“可演示的行为”拆，不按“层”拆**：不要“先写所有 repository，再写所有 API”；要“一个 adapter 从 fixture 到入库可演示”。
2. **契约先行**：若小阶段引入新字段/接口，先提交 schema/迁移/测试，再实现。
3. **每个小阶段一个 PR**：≤2 天、≤400 行、≤8 个文件、≤1 个迁移。
4. **依赖显式化**：前置小阶段未合并时，用 stacked PR 或先做 fixture/interface，不复制代码。
5. **测试与实现同交**：至少 1 个单元测试 + 1 个集成/契约测试；UI 任务至少 1 个 e2e 或截图。
6. **失败路径也要测**：超时、非法 JSON、空结果、重复、权限/门槛拒绝。
7. **不做“顺便重构”**：重构必须单独小阶段，且不改变行为。
8. **不引入新依赖**：确需引入，先写 ADR，说明替代方案与体积/安全影响。

### 5.2 小阶段派发卡模板

````markdown
## P{n}-S{m}：{标题}

**目标（一句话）**：

**依赖**：
- P{n}-S{m-1}（已合并 / stacked）
- 需要的 fixture/契约：

**输入**：
- 文件/接口/数据：

**输出**：
- 新增/修改的文件：
- API/CLI：
- 数据迁移：

**契约**：
- schema/接口：
- 错误码/边界：

**测试**：
- 单元：
- 契约/集成：
- e2e/演示：

**验收命令**：
```bash
make check
pytest tests/... -q
papersteward ...
```

**不在范围**：

**风险/回滚**：

**预估**：0.5–2 天

**交付物**：代码 + 测试 + fixture + 文档片段 + CHANGELOG（如用户可见）
````

### 5.3 示例：P3 采集阶段的拆分预览

> 这是“拆分示例”，不是最终任务清单；实际派发时再补文件路径、fixture 名和验收命令。

| 小阶段 | 目标 | 关键交付 | 验收 | 预估 |
|--------|------|----------|------|------|
| P3-S1 | 定义 `SourceAdapter` 契约与 fixture harness | 接口、统一元数据、cassette 加载器 | contract test + 假 adapter e2e | 1 天 |
| P3-S2 | 从 profile 生成查询 | 关键词/布尔/关注列表 | 表驱动测试；空 profile 报错 | 0.5–1 天 |
| P3-S3 | arXiv 适配器 | 搜索/解析/分页/错误处理 | cassette 测试；live smoke 可选 | 1–2 天 |
| P3-S4 | OpenAlex 适配器 | 元数据/venue/引用 | cassette 测试；字段缺失降级 | 1–2 天 |
| P3-S5 | Semantic Scholar 适配器（可选） | 引用/TLDR | cassette 测试 | 1–2 天 |
| P3-S6 | canonicalization + 去重 | DOI/arXiv/标题/作者/摘要 | 5 类重复案例 | 1–2 天 |
| P3-S7 | 硬过滤 + 运行报告 + CLI e2e | 过滤原因、run 表、`ingest` | e2e + 幂等重跑 | 1–2 天 |

**拆分顺序的理由**：先冻结适配器契约，再接真实来源；先去重/过滤，再让 pipeline 消费干净数据；每个 adapter 都能独立演示，不依赖 P4。

### 5.4 一个小阶段的完整示例（P3-S3）

````markdown
## P3-S3：arXiv 适配器

**目标**：给定查询与日期范围，从 arXiv fixture/API 拉取候选，转换为统一 `Paper` 元数据，并保留 provenance。

**依赖**：P3-S1（adapter 契约）已合并；`tests/fixtures/sources/arxiv.json` 已存在。

**输入**：`QuerySpec`（keywords、date_range、max_results）；fixture 或 live HTTP。

**输出**：
- `src/papersteward/sources/arxiv.py`
- `tests/contract/test_arxiv_adapter.py`
- `tests/fixtures/sources/arxiv.json`
- `docs/phases/P3-ingestion.md` 的 adapter 说明

**契约**：返回 `list[PaperCandidate]`；字段缺失用 `None`；不抛裸异常，统一 `SourceError`。

**测试**：
- 单元：Atom 解析、日期、作者、DOI/arXiv ID；
- 契约：fixture 全量解析；空结果；分页；
- 集成：ingest 后 DB 有 provenance。

**验收命令**：
```bash
pytest tests/contract/test_arxiv_adapter.py -q
papersteward ingest --source arxiv --fixture tests/fixtures/sources/arxiv.json --dry-run
```

**不在范围**：PDF 下载、摘要生成、去重策略调整。

**风险/回滚**：arXiv 字段变化 → adapter 隔离，失败只影响该来源；raw metadata 保留。

**预估**：1–2 天。
````

### 5.5 拆分时的常见反模式

| 反模式 | 后果 | 正确做法 |
|--------|------|----------|
| “先写所有模型，再写所有 API” | 长时间无法演示，风险后置 | 按一条竖切：模型→repo→API→测试 |
| “这个 PR 顺便重构一下” | diff 膨胀，review 困难 | 重构单独小阶段，行为不变 |
| “先合并，测试下阶段补” | 缺陷累积，阶段门失效 | 测试与实现同交 |
| “真实 API 测试放 CI” | 不稳定、限流、泄密 | cassette/fixture；live 单独 marker |
| “先做 UI，接口以后再说” | 返工 | CLI/API 先行，UI 只做薄层 |
| “一个 PR 改 3 个迁移” | 回滚困难 | 一小阶段一迁移，或明确拆开 |

---

## 6. 测试与质量策略

### 6.1 测试金字塔

| 层级 | 范围 | 工具/方式 | 运行时机 |
|------|------|-----------|----------|
| 单元 | 纯函数、校验、评分、去重、解析 | pytest | 每次 PR |
| 契约 | schema、adapter 返回、LLM 输出 | pytest + fixtures | 每次 PR |
| 集成 | DB、迁移、repository、pipeline | 临时 SQLite | 每次 PR |
| E2E | 初始化→采集→处理→排序→日报→归档 | pytest + fake LLM/cassette | 每次 PR/阶段门 |
| Live | 真实来源/真实 LLM | `pytest -m live` | 手动/夜间，不进 CI |
| Eval | 摘要/分类/排序质量 | golden + 人工抽检 | 阶段门/发布前 |

### 6.2 Fake LLM 与 HTTP cassette

- `FakeLLM` 按 `prompt_type + fixture` 返回预设 JSON；同时支持“非法 JSON/超时/空结果”模式。
- 所有 prompt 通过 registry 取版本；测试断言 prompt 版本与输出 schema。
- HTTP 使用 cassette（如 `pytest-recording`/自定义 JSON）；CI 不访问真实 API。
- 真实调用只用于 `make smoke-live`，手动触发，记录成本。

### 6.3 数据迁移

- 每个 schema 变更一个 Alembic migration；
- 迁移必须可 `upgrade`/`downgrade`（至少测试空库→head→base）；
- 迁移前自动备份（P8 起强制）；
- 不删除列/表，先加新列、双写/回填、再切换；MVP 可简化，但必须有回滚方案；
- 迁移测试使用临时 SQLite 文件，不用生产库。

### 6.4 Golden 测试

| 对象 | Golden 内容 |
|------|-------------|
| 摘要 | 长度、三槽、证据字段、失败重试 |
| 分类 | 主标签、副标签、置信度、低置信度队列 |
| 评分 | 设计文档示例 75.1、缺失数据、边界值 |
| 日报 | 篇数、标签分布、精读/粗读、特殊桶占位 |
| 归档 | 标签/主观分/备注/快照字段 |
| 特殊桶 | 档位阈值、无 P、桥接假设 |

### 6.5 安全与隐私测试

- 论文文本注入：fixture 中包含“忽略以上指令”等文本，确认输出 schema 不被劫持；
- 备注外发：默认不发送；只有用户授权才进入 LLM；
- 密钥：`git grep`/secret scan 无命中；
- 绑定地址：默认 `127.0.0.1`；
- 归档删除：需要二次确认/软删除策略；
- 依赖：P8 运行 `pip-audit`（或等价工具）。

---

## 7. 阶段交接与发布

### 7.1 每个大阶段结束时必须产出

`docs/phases/P{n}-{slug}.md` 的 handoff 部分，至少包含：

```markdown
# P{n} Handoff

## 状态
- 完成 / 部分完成 / 阻塞
- 日期、tag、负责人

## 现在能做什么
- 可运行命令
- 可演示路径

## 契约与迁移
- 冻结的 schema/接口
- 迁移版本、回滚方式

## 测试与指标
- 通过的测试
- 覆盖率/关键 golden
- 已知失败/跳过

## 已知问题与债务
- P0/P1 缺陷
- 临时方案与到期时间

## 下一阶段的输入
- 已就绪的 fixture/接口
- 仍需用户确认的决策

## 风险
- 可能阻塞下一阶段的事项
```

### 7.2 阶段门清单

- [ ] 所有小阶段 PR 已合并；
- [ ] `make check` / `make test-integration` / `make e2e` 通过；
- [ ] `make demo` 可复现；
- [ ] 迁移 up/down 通过；
- [ ] 本阶段验收标准全部有证据；
- [ ] handoff/CHANGELOG 更新；
- [ ] 无未处理的 P0/P1 缺陷；
- [ ] 打 tag `p{n}-done`。

### 7.3 发布检查清单（P8）

- [ ] 全新 clone + `make bootstrap` 成功；
- [ ] `.env.example` 完整，无真实密钥；
- [ ] `make check` / `make e2e` / `make eval` 通过；
- [ ] 备份/恢复演练通过；
- [ ] 每日运行连续 2 周无阻断故障；
- [ ] README/quickstart/runbook/known issues 完整；
- [ ] pilot checklist 完成；
- [ ] tag `v0.3.0-pilot`。

### 7.4 试点协议

- 选 1 个主方向 + 1 个远邻方向；
- 导入 20 篇种子（10 核心、5 经典、5 负例）；
- 连续 4 周，每周复盘一次；
- 第 2 周只观察，不调权重；
- 核心指标：每周读完篇数/推送篇数、4–5 分占比、无关推送率、摘要抽检；
- 试点结束决定：继续 V2 / 先修质量 / 暂停。

---

## 8. 开发风险与应对

| 风险 | 表现 | 应对 |
|------|------|------|
| 范围蔓延 | P4/P6/P7 越做越大 | 每阶段 out-of-scope 写死；小阶段 ≤2 天；新需求进 backlog |
| 数据模型反复 | 迁移打架、返工 | P1 契约 review；变更走 ADR；先加列后切换 |
| LLM 输出不稳 | 摘要/分类 JSON 失败 | FakeLLM + schema + 修复重试 + golden；失败不推送 |
| 来源 API 变化 | 采集失败/字段缺失 | adapter 隔离；cassette；raw metadata；fallback |
| CI 不稳定 | 真实网络/时间/随机 | 固定时钟/UUID；cassette；无 live CI |
| UI 拖慢核心 | P6/P7 页面耗时 | CLI/API 先行；HTMX 薄层；页面只做调用/展示 |
| 评分不可解释 | 用户不信任排序 | golden；分项/权重/证据；配置版本；不静默改 |
| 成本失控 | LLM 账单 | 预算、缓存、先筛后算、fake 测试、成本日志 |
| 安全/隐私 | 备注外发、密钥泄露 | 默认不发送；脱敏；localhost；secret scan |
| 单人瓶颈 | 关键路径长 | 双人时 P2/P3、P4 内部、P6 UI 并行；先冻契约 |
| 估算乐观 | P4/P8 超期 | 20% 缓冲；按 4.2 裁剪；每周复盘 |

---

## 9. 明确不做（当前 9 个阶段）

| 不做 | 原因 | 何时考虑 |
|------|------|----------|
| 多用户/鉴权/团队共享 | 单用户本地优先 | V2 |
| PDF 全文解析/图表抽取 | 成本高，先摘要+元数据 | V2 |
| 引文网络可视化 | 非核心闭环 | V2 |
| Zotero/Obsidian/Notion 同步 | 集成工作量大 | V2 |
| 本地大模型完全离线 | 先跑通 API 模型 | V2 |
| 自动综述草稿 | 高风险、非核心 | V2+ |
| 移动端 App | Web/邮件足够 | V2+ |
| Postgres/Celery/Redis | MVP 不需要 | 数据量/并发需要时 |
| React/Next.js | HTMX 足够 | 需要复杂交互时 |
| 自动权重调参 | 违反“权重稳定”原则 | 不做；只给建议 |

---

## 10. 需求覆盖对照

| 设计需求 | 主要阶段 | 验收证据 |
|----------|----------|----------|
| 1. 研究方向文档作为相关度基准 | P1/P2/P4 | profile 版本、确认事件、R golden |
| 2. 主/副标签、主题桶、统一规则 | P1/P2/P4/P7 | taxonomy 校验、分类一致率、桶视图 |
| 3. 初始化充分协商 | P2 | G1–G4、校准报告、试运行 |
| 4. 100 字概括 | P4 | 长度 100%、证据、抽检 |
| 5. 三个特殊桶 | P4/P7 | 提名、档位、桥接假设、无 P |
| 6. 精读/粗读、完成、评分、备注 | P6 | 日报卡片、action、归档 |
| 7. 三层评分/权重/自定义排序 | P5/P7 | 评分快照、配置版本、视图 |
| 8. 归档保留标签/主观分/备注 | P6 | 归档记录、导出 |

---

## 附录 A：阶段 Handoff 模板

````markdown
# P{n} Handoff

## 状态
- 完成度：
- 日期：
- tag：

## 现在能做什么
```bash
# 可运行命令
```

## 契约与迁移
- 冻结 schema：
- 迁移版本：
- 回滚：

## 测试与指标
- 通过：
- 跳过/live：
- 覆盖率：
- golden：

## 已知问题
- P0/P1：
- 临时方案：

## 下一阶段输入
- 已就绪：
- 待确认：

## 风险
````

## 附录 B：小阶段派发卡模板

见第 5.2 节；实际派发时复制到 issue/任务系统，并补全文件路径、fixture 名、验收命令。

## 附录 C：P3 拆分示例

见第 5.3/5.4 节。其他阶段按同一方法拆分：

- P0：骨架 → DB → API → CLI → Docker/CI；
- P1：实体 → 迁移/仓储 → 文档 → 配置 → LLM 网关 → 契约测试；
- P2：状态机 → 档案 → 分类法 → 校准 → 试运行 → UI；
- P4：富化 → R → 摘要 → 分类 → 特殊提名 → 静态分 → 审核队列；
- P5：D → P → U 聚合 → 配置 → 快照/解释 → golden；
- P6：编排 → 渲染 → 推送 → 操作 → 归档/导出 → UI；
- P7：特殊桶数据 → 档位/配额 → 视图 → 自定义排序 → 权重预览 → e2e；
- P8：eval → 指标 → 安全 → 备份 → 成本 → 文档/发布。

## 附录 D：阶段门检查表

- [ ] 小阶段全部合并；
- [ ] `make check` 通过；
- [ ] `make e2e` 通过；
- [ ] `make demo` 可复现；
- [ ] 迁移 up/down；
- [ ] 验收标准有证据；
- [ ] handoff/CHANGELOG；
- [ ] 无 P0/P1 缺陷；
- [ ] tag。

## 附录 E：术语

| 术语 | 含义 |
|------|------|
| 大阶段 | P0–P8，1–2 周，里程碑级 |
| 小阶段 | 0.5–2 天，一个 PR，可直接派发 |
| Walking Skeleton | 端到端最薄的可运行骨架 |
| Golden | 固定输入/输出的回归样本 |
| Cassette | 录制的 HTTP 响应，用于离线测试 |
| FakeLLM | 按 fixture 返回预设输出的假模型 |
| G1–G4 | 初始化四道确认门 |
| DoR/DoD | 可直接派发的准备标准 / 完成标准 |
