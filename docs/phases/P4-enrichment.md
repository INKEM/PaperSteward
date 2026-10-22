# P4 富化、摘要、分类与静态评分

> 依据：大阶段开发计划 §3 P4、§5.3；设计 §6.5–6.7、§10。
> 状态：**P4-S1–S8 实现完成；独立验收 F1–F5 已返工，待独立复验；P5 不开始。**
> 验收报告：[P4-acceptance.md](P4-acceptance.md)。
> 前置：P3 验收通过；真实 LLM 接入不改变 P3 采集、去重与 G4 门禁。

## 小阶段卡片

P4 是最大阶段，拆成 8 张卡；每张卡单独分支/提交，默认离线测试。原计划 P4-S1
“富化数据模型与 provider”同时包含真实网络接入和 schema 变更，拆成两张卡以控制
单卡规模：S1 冻结真实 LLM 配置/provider 与富化输入契约，S2 再落富化持久化。

原计划 7 个工作包与当前 8 张卡的对应关系：

| 原计划 | 当前编号 | 内容 |
|---|---|---|
| P4-S1 | P4-S1 + P4-S2 | provider/契约 + enrichment 持久化（拆分） |
| P4-S2 | P4-S3 | 相关度 R |
| P4-S3 | P4-S4 | 100 字摘要 |
| P4-S4 | P4-S5 | 分类与标签/桶 |
| P4-S5 | P4-S6 | 特殊桶提名 |
| P4-S6 | P4-S7 | 静态分 A/E/S |
| P4-S7 | P4-S8 | 审核队列 + process CLI e2e |

### P4-S1：真实 LLM provider、配置入口与富化输入契约（本次）

- 目标：提供可配置的 OpenAI-compatible 真实 LLM 入口，并冻结富化 provider 的
  中性值输入/输出契约；默认仍为 FakeLLM。
- 输入/依赖：P1 的 `LLMProvider`/`LLMGateway`/FakeLLM；P3 的 `Paper`、来源元数据；
  `Settings` 配置体系。
- 输出：
  - `Settings` 的 `PAPERSTEWARD_LLM_*` 字段、校验与 `.env.example`；
  - `llm/openai_compatible.py`（httpx `POST /chat/completions`，硬超时、usage/成本、
    脱敏错误）、`llm/factory.py`（显式 `live` 授权门）；
  - `pipeline/enrich.py`（`EnrichmentSignals`、`EnrichmentProvider`、`NeutralEnricher`）；
  - `papersteward llm ping --fake|--live`；
  - ADR、配置指南、单元/集成 CLI 测试。
- 契约：
  - 默认 `llm_provider=fake`；真实调用必须显式 `--live`，不允许隐式联网；
  - 测试只用 `FakeLLM`/`httpx.MockTransport`，CI 不访问真实 LLM；
  - API key 只从 `PAPERSTEWARD_LLM_API_KEY` 读取并以 `SecretStr` 保存，不写日志/报告；
  - 非本机 endpoint 必须 HTTPS；不跟随重定向、不读代理环境；
  - 网关负责重试预算；provider 只做一次补全，错误只抛固定文案；
  - 富化缺失字段用 `None`/`()`/`OaStatus.UNKNOWN`/`confidence=0`，不伪造信号。
- 验收：
  - `pytest tests/unit/test_openai_compatible.py tests/unit/test_llm_factory.py tests/unit/test_enrich_contract.py tests/integration/test_cli_llm.py -q`
  - `papersteward llm ping --fake`（离线）与 `--live`（显式配置后手动）；
  - `make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、
    `make secret-scan`。
- 范围外：富化持久化/迁移、embedding、R/摘要/分类/特殊桶/静态分、成本预算、流式输出、
  function calling、真实模型质量验收。
- 风险/回滚：真实调用可能产生费用和隐私外发；默认 fake + `--live` 授权 + 不提交 key。
  回滚删除 provider/配置/CLI 分支即可，无 schema 变更；P4 阶段门不依赖已删除的密钥。

### P4-S2：富化信号持久化与中性值（完成）

- 目标：把 `EnrichmentSignals` 持久化到独立表，保留 confidence/source/provenance；
- 输出：迁移 `0006`、`Enrichment`/`EnrichmentSignals`、ORM/映射/仓储、幂等 upsert、
  中性值回填；不调用真实 LLM；
- 验收：迁移 up/down、缺失字段中性值、来源可追溯、重复运行幂等；
- 范围外：R/摘要/分类；预估 1–2 天。

### P4-S3：相关度 R（embedding + rubric + 证据）（完成）

- 目标：按设计 §10.2 计算 R，证据缺失降级/转人工；先接离线/Fake embedding；
- 输出：`pipeline/embeddings.py`、`pipeline/relevance.py`、`relevance` prompt/schema、
  golden/正反例 fixtures；
- 范围外：真实 embedding 供应商选择（需单独 ADR）；预估 2–3 天。

### P4-S4：100 字概括与四槽/证据校验（完成）

- 目标：生成 ≤100 非空白字符摘要 + what/how/finding/limitation + evidence；
- 输出：`summary/v2` prompt、`pipeline/summarize.py`、逐字段证据/数字/空泛词校验、
  持久化与 `needs_review`；schema 复用 P1 `SummaryOutput`；
- 范围外：全文解析、真实 LLM 质量验收；预估 1–2 天。

### P4-S5：分类、置信度与边界案例（完成）

- 目标：主/副标签唯一性、置信度阈值、低置信度转人工；绑定已确认 taxonomy 版本；
- 输出：`classification/v2` prompt/schema（runner-up + evidence）、`pipeline/classify.py`、
  分类正反例、标签/桶幂等写入；
- 范围外：特殊桶提名；预估 1–2 天。

### P4-S6：特殊桶提名与桥接假设（完成）

- 目标：review/distant/classic 三类提名、理由/证据与远邻 bridge memo；
- 输出：`special_nomination/v1` prompt/schema（review layers、classic basis、bridge memo）、
  `pipeline/special.py`、提名正反例；
- 范围外：配额/展示（P7）、最终归属写入；预估 1–2 天。

### P4-S7：静态分 A/E/S 与快照（完成）

- 目标：权威/额外价值/静态分确定性计算、版本冻结、快照追加写；
- 输出：`scoring/static.py`、A/E/S 分项解释、golden、迁移 `0007` 与静态快照仓储；
- 范围外：动态分/排序（P5）；预估 1–2 天。

### P4-S8：审核队列与 `process` CLI e2e（完成）

- 目标：低置信度/特殊提名/边界案例可列出、通过、改标签；完整 process 离线 e2e；
- 输出：`review_item` 迁移 `0008`、review API/CLI、`process` CLI、真实 Uvicorn e2e、
  操作文档；
- 范围外：日报/推送（P6）、特殊桶最终配额/展示（P7）；预估 1–2 天。

## P4-S1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PAPERSTEWARD_LLM_PROVIDER` | `fake` | `fake` / `openai_compatible`（`openai` 为别名） |
| `PAPERSTEWARD_LLM_BASE_URL` | `https://api.openai.com/v1` | API 根地址；非本机必须 HTTPS |
| `PAPERSTEWARD_LLM_API_KEY` | 空 | 只放环境/`.env`；`SecretStr`，不打印 |
| `PAPERSTEWARD_LLM_MODEL` | 空 | `openai_compatible` 时必填 |
| `PAPERSTEWARD_LLM_TIMEOUT_S` | `30` | 单次请求硬超时（秒） |
| `PAPERSTEWARD_LLM_MAX_ATTEMPTS` | `3` | 网关 provider 重试上限 |
| `PAPERSTEWARD_LLM_PROMPT_COST_PER_1K_USD` | `0` | 仅用于成本估算 |
| `PAPERSTEWARD_LLM_COMPLETION_COST_PER_1K_USD` | `0` | 仅用于成本估算 |

配置示例、OpenAI/DeepSeek/Ollama 用法与故障排查见
[LLM provider 配置指南](../llm-provider.md)。

## P4-S1 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s1-make-check'`：
  ruff format/check、strict mypy（**83 源文件**）通过；**753 passed / 4 e2e excluded**
  （P3 优化后基线 700，新增 53 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s1-integration'`：
  **233 passed**（基线 228，新增 5 项 CLI live/fake 路由测试）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s1-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0005 → base → 0005 通过（无新迁移）。
- `make demo DEMO_PORT=8056`：临时库迁移/seed、`/health`、`/papers` 正常。
- `make secret-scan`、`git diff --check` 通过；未使用真实厂商 key/网络。
- 手工 loopback 验证：本机临时 HTTP server 模拟 `/v1/chat/completions`，
  `PAPERSTEWARD_LLM_PROVIDER=openai_compatible` + `llm ping --live` 返回
  `provider=openai_compatible`，请求路径正确、Bearer header 正确、usage 7/3 与
  latency 正常；**未调用任何真实厂商 API**。

### 能做什么

- 通过 `.env`/环境变量配置 OpenAI-compatible provider；默认 fake，真实调用需
  `llm ping --live` 或后续服务显式 `live=True`；
- 使用 `FakeLLM`/`MockTransport` 做确定性测试；`llm ping --fake` 无网络；
- 通过 `EnrichmentSignals`/`EnrichmentProvider`/`NeutralEnricher` 传递中性富化信号；
- 网关记录 token/成本/延迟，认证失败等不可重试错误不消耗重试预算；
- 端点/模型/key/超时/重试/单价均可配置，错误信息不含密钥或原始响应体。

### 不能做什么

- P4-S1 本身未持久化富化信号（P4-S2 已补齐）；未实现 R/摘要/分类/特殊桶/静态分；
- 未实现流式输出、function calling、embedding、多 provider 路由、每日成本硬上限；
- 未进行真实模型质量/价格/限流验收；默认不自动调度真实 LLM；
- 不读取备注，不把论文文本当指令执行。

### P4-S2 输入与开工约束

- 输入：`EnrichmentSignals`/`NeutralEnricher`、`Settings.llm_*`、`LLMProvider`/`LLMGateway`；
- 新增 `enrichment` 表必须走 **0006 迁移**，保留 confidence/source/provenance；
- 缺失字段继续用 `None`/`()`/`OaStatus.UNKNOWN`/`confidence=0`；重复富化必须幂等；
- P4-S2 默认仍用 `NeutralEnricher`/fake，真实富化需显式授权，测试离线；
- 建议文件：`core/entities.py`、`storage/models.py`、`storage/repositories.py`、
  `migrations/versions/0006_*.py`、映射与集成测试；不要提前实现 R。

### 回滚

- P4-S1 回滚：撤销代码/配置/文档即可；当时无 schema/依赖/数据变更；
- P4-S2 回滚：`alembic downgrade 0005` 删除 `enrichment` 表；先备份已有富化结果；
- 已配置的真实 key 留在用户 `.env`/环境中，不由代码删除；撤销后默认回到 FakeLLM。

### 规模说明

P4-S1 超过“≤8 文件/≤400 行”指引：真实 provider（网络/认证/超时/成本/错误边界）、
配置校验、CLI 双模式、富化中性契约、ADR 与离线测试/文档共同构成本次竖切；
未实现 P4-S2 的持久化或任何 R/摘要/分类/评分逻辑。

## P4-S2 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s2-make-check'`：
  ruff format/check（166 文件）、strict mypy（**84 源文件**）通过；
  **757 passed / 4 e2e excluded**（P4-S1 后基线 753，新增 4 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s2-integration'`：
  **237 passed**（P4-S1 后基线 233）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s2-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → **0006** → base → 0006 通过，
  包含 upgrade/downgrade 往返。
- `make demo DEMO_PORT=8057`：临时库自动升级到 0006，`/health`、`/papers` 正常。
- `make secret-scan`、`git diff --check` 通过；无真实 LLM/厂商调用。

### 数据与行为

- 新增 `enrichment` 表（每篇一条，PK `paper_id`，FK 级联删除）：
  teams/experiment_quality JSON、venue_level、citation_count/velocity、code_url/data_url、
  oa_status、confidence、source、created_at/updated_at，以及四列 all-or-none provenance；
- DB 约束覆盖 confidence 0–1、引用数/速度非负、OA 枚举、provenance 全空或全非空；
- `Enrichment`/`EnrichmentSignals` 移入 `core.entities`，校验保持中性值语义；
- `EnrichmentRepository.save` 幂等：同内容重复保存不推进 `updated_at`；内容变化时保留
  `created_at`、只前进 `updated_at`，不自行 commit；
- `persist_enrichment` 用 `NeutralEnricher` 完成第一次中性值回填，不调用网络/LLM；
- 无 `enrichment` 的存量文献不伪造行；由后续处理阶段按需生成。

### 能做什么

- 对已存在 `Paper` 幂等写入/更新中性或明确来源的富化信号；
- 读取单篇/按 paper_id 稳定排序列出富化结果；
- 保留 confidence/source 与可选 LLM provenance，供 P4-S3+ 评分/审计使用；
- 缺失论文、非法信号或 provenance 不完整时由 FK/CHECK/领域校验拒绝。

### 不能做什么

- 不自动为全部 screened 文献批量回填，也不改变 `PaperStatus`；
- 不判断 R/摘要/分类，不做真实论文页面/API 富化；
- 未实现 enrichment 删除/版本历史（每篇保留最新一条）；
- 不调用真实 LLM；真实富化 provider 仍需显式授权。

### P4-S3 输入与开工约束

- 输入：`Enrichment`/`EnrichmentSignals`、`EnrichmentRepository.get/list`、screened 文献；
- R 计算应只读取已确认 profile/taxonomy/config 的精确版本，证据缺失转人工/降级；
- embedding provider 选型需单独 ADR，测试用 fake/cassette，不访问真实网络；
- 新字段/表继续新增迁移（当前 0006），不要重写 0005/0006；
- 建议输出：`pipeline/relevance.py`、rubric prompt/schema、golden fixtures。

### 回滚

- `alembic downgrade 0005` 只删除 `enrichment` 表；先备份已有富化结果；
- 撤销 P4-S2 代码/映射/仓储即可回到 P4-S1 状态；`Paper`/`Run`/采集数据不变；
- 不自动删除用户 `.env` 中的真实 LLM 配置。

### 规模说明

P4-S2 超过 8 文件指引：领域实体、迁移、ORM/映射/仓储、中性值服务、CLI/迁移测试与
文档共同构成持久化竖切；没有实现 P4-S3 的 R、embedding 或任何摘要/分类/评分逻辑。

## P4-S3 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s3-make-check'`：
  ruff format/check（170 文件）、strict mypy（**86 源文件**）通过；
  **780 passed / 4 e2e excluded**（P4-S2 后基线 757，新增 23 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s3-integration'`：
  **237 passed**（无新增集成用例；本阶段为纯计算/prompt）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s3-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0006 → base → 0006 通过（无新迁移）。
- `make demo DEMO_PORT=8058`：`/health`、`/papers` 正常；head 仍为 0006。
- `make secret-scan`、`git diff --check` 通过；未调用真实 embedding/LLM。
- 离线演示：basic profile + 合成论文 + `FakeLLM` + 哈希 embedding 得到
  `R=79.6`、`band=normal`、四维证据全部有效、`needs_review=false`（仅记录
  `uncalibrated_similarity`）。

### 实现

- `pipeline/embeddings.py`：`EmbeddingProvider` 协议 + `HashingEmbeddingProvider`
  （英文词/中文单字的确定性有符号哈希 + L2）与余弦；不访问网络。
- `pipeline/relevance.py`：profile facet 提取、语义 max/top2、rubric 证据校验、
  R 计算、55/65 band、`needs_review` 原因；只接受 confirmed profile。
- `schemas`：新增 `relevance` 输出 schema（四维 0–5 整数、逐维证据、confidence）；
  prompt registry 新增 `relevance/v1`。
- fixtures：有效 rubric、缺失证据、伪造证据、低置信度、R golden。

### 关键规则

- `sem = 0.7·max_sim + 0.3·mean_top2_sim`（负余弦按 0）；
- `rubric = 0.35·problem + 0.25·method + 0.25·domain + 0.15·actionability`；
- `R = 100·(0.40·sem + 0.60·rubric/5)`，四舍五入 1 位小数；
- 每个维度必须有逐字出现在标题/摘要中的证据；缺失/伪造证据用中性 2.5，并标记
  `missing_evidence:<dim>`/`invalid_evidence`；
- rubric confidence < 0.6、缺摘要也进入 review；R 仍计算，不静默丢弃；
- `R < 55` below_threshold、`55–65` low、`>=65` normal；不改变 `PaperStatus`；
  `uncalibrated_similarity` 只作为审计提示，不单独强制 review。

### 能做什么

- 对已确认 profile + 文献计算可复现 R、band、rubric 分项与证据有效性；
- 用 FakeLLM/离线哈希 embedding 做确定性测试和 golden 回归；
- 把 LLM rubric 结果、evidence 与 provenance 交给后续静态分/审核队列；
- 通过 `calibrator` 注入未来真实分位数/isotonic 校准。

### 不能做什么

- 哈希 embedding 只是词法重合，不是语义模型；默认 `calibrated=False`，未做历史标注校准；
- 未选择/调用真实 embedding 供应商（需单独 ADR）；
- 未持久化 R/score_snapshot，未改变状态、入队或推送；
- 只使用标题/摘要，不解析全文；venue/作者等权威信号不参与 R；
- `relevance` prompt 的真实 LLM 调用仍需显式 `--live`/调用方授权，测试只用 FakeLLM。

### P4-S4 输入与开工约束

- 输入：`RelevanceResult`（含 `needs_review`/reasons）、`SummaryOutput`/summary prompt、
  confirmed profile/taxonomy 精确版本；
- 摘要生成失败/证据不足应保留元数据并进入 review，不推送；
- 继续使用 FakeLLM/golden fixtures；真实 LLM 需显式授权；
- 新字段/表继续新增迁移（当前 0006），不要重写 0006。

### 回滚

- 无迁移；撤销 embeddings/relevance 模块、relevance schema/prompt/fixtures 即可；
- 已配置的真实 LLM 环境不受影响；不删除用户 `.env`。

### 规模说明

P4-S3 超过 8 文件指引：embedding 协议/实现、R 管线、schema/prompt、contract freeze
更新、golden/正反例与文档共同构成本次竖切；未实现 P4-S4 摘要或任何持久化/入队逻辑。

## P4-S4 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s4-make-check'`：
  ruff format/check（173 文件）、strict mypy（**87 源文件**）通过；
  **792 passed / 4 e2e excluded**（P4-S3 后基线 780，新增 12 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s4-integration'`：
  **240 passed**（P4-S3 后基线 237；新增 3 项摘要持久化）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s4-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0006 → base → 0006 通过（无新迁移；
  复用 P1 的 `summary` 表）。
- `make demo DEMO_PORT=8059`：`/health`、`/papers` 正常；head 仍为 0006。
- `make secret-scan`、`git diff --check` 通过；未调用真实 LLM。
- 离线演示：`summary/v2` + `FakeLLM` 生成 63 字四槽摘要，what/how/finding/limitation
  四条证据均逐字有效、数字 2–4 有据、`validation_status=passed`、`needs_review=false`。

### 实现

- `summary/v2` prompt：要求 60–95 目标长度、四槽非空、每槽至少一条逐字证据、
  数字有据、无空泛评价词；`summary/v1` 保留。
- `pipeline/summarize.py`：`generate_summary` 调用 gateway（含一次 schema 修复），
  然后做业务校验；`persist_summary` 复用 `SummaryRepository`。
- 校验项：四字段非空（schema/entity）、summary_100 ≤100 非空白字符、每条 quote 必须
  逐字出现在标题/摘要、每个槽位至少一条有效证据、summary 中的数字 token 必须出现在
  有效证据中、空泛词黑名单。
- 失败降级：任一校验失败写入 `SummaryValidationStatus.NEEDS_REVIEW`，不丢弃摘要；
  证据列表只持久化有效 quote，`SummaryResult` 保留字段映射/无效 quote/原因。
- 缺少摘要时在调用 LLM 前拒绝，由调用方转人工；schema 修复后仍失败时向上抛出
  `LLMValidationError`，不写非法摘要。

### 能做什么

- 从标题/摘要生成受校验的 100 字概括与 what/how/finding/limitation 四槽；
- 区分 PASSED 与 NEEDS_REVIEW，并保留 provenance、有效证据与 review 原因；
- 对已有摘要幂等覆盖（每篇一行），支持后续 P4-S8 审核队列读取。

### 不能做什么

- 不解析全文；只基于标题/摘要，缺少摘要直接失败；
- 不实现与摘要的余弦相似度阈值（需要真实/校准 embedding，后续 ADR）；
- 不改变 `PaperStatus`、不入队/推送；摘要失败后的状态迁移由 P4-S8 编排；
- 持久化只保存有效 quote 列表，字段映射仅保留在内存 `SummaryResult`；如需结构化
  evidence 表需新增迁移；
- 不调用真实 LLM；FakeLLM/golden 覆盖正反例与 repair。

### P4-S5 输入与开工约束

- 输入：`SummaryResult`/`SummaryRepository`、`relevance`/`needs_review`、已确认 taxonomy；
- 分类只读取精确确认的 taxonomy 版本；主标签唯一、副标签 0–5；
- 低置信度/边界案例进入 review，不直接入队；
- 继续使用 FakeLLM/fixtures；新字段/表走新增迁移（当前 0006）。

### 回滚

- 无迁移；撤销 `summary/v2`、`pipeline/summarize.py`、fixtures/测试即可；
- 已写入的 `summary` 行保留，回滚代码后仍可读；不删除用户 `.env` 或 LLM 配置。

### 规模说明

P4-S4 超过 8 文件指引：prompt 版本、摘要服务/校验、持久化、正反例与文档共同构成
本次竖切；没有提前实现 P4-S5 分类逻辑。

## P4-S5 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s5-make-check'`：
  ruff format/check（176 文件）、strict mypy（**88 源文件**）通过；
  **803 passed / 4 e2e excluded**（P4-S4 后基线 792，新增 11 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s5-integration'`：
  **242 passed**（P4-S4 后基线 240；新增 2 项分类持久化）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s5-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0006 → base → 0006 通过（无新迁移；
  复用 P1 的 `paper_tag`/`paper_bucket`）。
- `make demo DEMO_PORT=8060`：`/health`、`/papers` 正常；head 仍为 0006。
- `make secret-scan`、`git diff --check` 通过；未调用真实 LLM。
- 离线演示：`classification/v2` + `FakeLLM` 得到 primary T02（0.86）、runner-up T01
  （0.62）、secondary S01/S02、主题桶 b-t02，evidence 逐字有效、`needs_review=false`。

### 实现

- `classification/v2` prompt：主标签、次优主标签/置信度、副标签、逐字 evidence 与
  boundary_note；保留 v1。
- `ClassificationOutput` 扩展可选 `runner_up_tag`/`runner_up_confidence` 与 evidence；
  二者必须同时提供且 runner-up 不能等于 primary。
- `pipeline/classify.py`：taxonomy block、置信度/边界/证据校验、标签与主题桶写入。
- review 原因：`low_primary_confidence`、`ambiguous_primary`、`invalid_runner_up`、
  `boundary_case`、`missing_evidence`、`invalid_evidence`、`missing_abstract`、
  `invalid_secondary_tag:*`、`duplicate_secondary_tag:*`。
- taxonomy 主标签必须在已确认且激活的受控词表中；主标签没有激活主题桶时直接拒绝。
- 副标签优先按 taxonomy code/name/alias 归一；未命中受控词表时按自由词保存（≤64 字符）。
- `persist_classification` 首次写入主/副标签与归属桶；完全相同幂等，已有不同分类/桶
  时拒绝静默覆盖；不自行 commit。

### 能做什么

- 对已确认 taxonomy 生成主/副标签、置信度、runner-up、逐字证据与 boundary note；
- 低置信度/接近次优/边界/证据不足自动进入 `needs_review`，分类结果仍可保存供审核；
- 幂等写入 `paper_tag`/`paper_bucket`，保留 provenance 与主题桶归属；
- 输出结构化 `ClassificationResult.as_json()` 供 P4-S8 审核队列使用。

### 不能做什么

- 不改变 `PaperStatus`、不入队/推送；状态迁移由 P4-S8 编排；
- 不处理特殊桶提名（`special_candidates` 留空，P4-S6）；
- 不支持已有不同分类的自动重分类/覆盖，重分类需要后续显式流程；
- 不解析全文，只用标题/摘要作为证据来源；
- 未做分类准确率/kappa 的真实模型验收；测试使用 FakeLLM 与合成 fixtures。

### P4-S6 输入与开工约束

- 输入：`ClassificationResult`（含 boundary_note、special_candidates 字段位置）、
  taxonomy/bucket 目录、`paper_tag`/`paper_bucket` 写入结果；
- 特殊桶提名需理由/证据与远邻 bridge memo；只写提名，不改变最终归属；
- 继续使用 FakeLLM/fixtures；新字段/表走新增迁移（当前 0006）。

### 回滚

- 无迁移；撤销 `classification/v2`、`pipeline/classify.py`、fixtures/测试即可；
- 已写入的 `paper_tag`/`paper_bucket` 保留；回滚代码不会自动删除用户分类数据。

### 规模说明

P4-S5 超过 8 文件指引：prompt/schema 扩展、分类服务、taxonomy/证据校验、标签/桶
持久化、正反例与文档共同构成本次竖切；没有提前实现 P4-S6 特殊桶逻辑。

## P4-S6 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s6-make-check'`：
  ruff format/check（178 文件）、strict mypy（**89 源文件**）通过；
  **812 passed / 4 e2e excluded**（P4-S5 后基线 803，新增 9 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s6-integration'`：
  **242 passed**（本阶段为纯提名/schema，无新增集成用例）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s6-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0006 → base → 0006 通过（无新迁移）。
- `make demo DEMO_PORT=8061`：`/health`、`/papers` 正常；head 仍为 0006。
- `make secret-scan`、`git diff --check` 通过；未调用真实 LLM。
- 离线演示：`special_nomination/v1` + `FakeLLM` 得到 review（0.82，method+evaluation）
  与 distant（0.70，四段 bridge memo）；`selected_kind=review`，证据逐字有效。

### 实现

- `special_nomination/v1` prompt：三类特殊桶定义、review/classic/distant 条件、
  bridge memo 四段、证据与 confidence；明确不改主标签/配额。
- 新 `special_nomination` schema：每条提名包含 bucket/confidence/reason/evidence；
  review 至少两个层级、classic 至少一个经典依据、distant 必须四段 bridge memo；
  同一桶最多一次，最多三条。
- `pipeline/special.py`：逐字标题/摘要证据校验、置信度阈值（默认 0.6）、
  无效/伪造证据/缺失 bridge memo 保留提名但 `accepted=false`；
  `selected_kind` 按 `review > classic > distant` 选择；只提名，不写库、不改归属。
- `needs_review` 汇总无效提名/低置信度/缺摘要原因；全部有效时可为 false。

### 能做什么

- 为论文生成 0–3 条特殊桶提名，附理由、逐字证据、review 层级/classic 依据；
- 远邻提名强制四段可证伪 bridge memo；
- 用 `selected_kind` 表达默认归属优先级，供 P4-S8 决定是否写入/展示；
- 结构化 `SpecialNominationResult.as_json()` 供审核队列/配额阶段使用。

### 不能做什么

- 不写 `paper_bucket`、不改变主标签/论文状态；最终归属由 P4-S8 决定；
- 不处理配额、展示、周限制（P7）；
- 不调用真实 LLM；准确率/召回率未做真实模型验收；
- 不自动把 distant/classic 提名转成成员关系；无迁移/新依赖。

### P4-S7 输入与开工约束

- 输入：`SpecialNominationResult.selected_kind`、`ClassificationResult`、R、摘要、
  enrichment、已确认配置/版本；
- 静态分 A/E/S 只做确定性计算，版本冻结；不因特殊桶提名改写主标签；
- 新字段/表继续新增迁移（当前 0006），测试保持 FakeLLM/fixtures。

### 回滚

- 无迁移；撤销 `special_nomination` schema/prompt、`pipeline/special.py`、fixtures/测试即可；
- 已存在的标签/桶/摘要数据不受影响。

### 规模说明

P4-S6 超过 8 文件指引：schema/prompt、三类提名校验、bridge memo、优先级选择、
正反例与文档共同构成本次竖切；未实现 P4-S7 静态分或 P7 配额/展示。

## P4-S7 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s7-make-check'`：
  ruff format/check（182 文件）、strict mypy（**90 源文件**）通过；
  **822 passed / 4 e2e excluded**（P4-S6 后基线 812，新增 10 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s7-integration'`：
  **243 passed**（P4-S6 后基线 242；新增 1 项静态快照持久化）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s7-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → **0007** → base → 0007 通过。
- `make demo DEMO_PORT=8062`：自动升级到 0007，`/health`、`/papers` 正常。
- `make secret-scan`、`git diff --check` 通过；未调用真实 LLM。
- golden 演示：venue A* + 作者 1.0/0.5 + citation 0.8 → A=92.0；
  code/data/repro/rigor/open 组合 → E=70.0；R=80、A=92、E=70、权重
  0.65/0.20/0.15 → S=80.9。

### 实现与决策

- [ADR 0014](../adr/0014-static-score-snapshot.md)：静态分使用独立 `static_score_snapshot` 表，不伪造完整
  `score_snapshot` 的动态分/priority；P5 组合完整日快照。
- `scoring/static.py`：
  - A = 100·(0.40·venue + 0.40·author_team + 0.20·citation)；
    venue 分档 A*/A/B/C/unknown/predatory；author_team 用 max/top2；
    <183 天或缺失引用用中性 0.5 并降低 confidence；
  - E = 100·(0.30·code + 0.20·data + 0.20·repro + 0.20·rigor + 0.10·open)；
    None 用中性 0.5，OA open/closed/unknown 映射 1/0/0.5；
  - S = R·wR + A·wA + E·wE，权重来自已校验 `ScoringConfig`；
  - breakdown/confidence/原因与 SHA256 输入哈希进入快照。
- 迁移 `0007`：`static_score_snapshot`，PK `(paper_id, score_date)`，
  CHECK 分值/置信度范围；仓储只追加、无 update/delete。
- 版本冻结：同版本不重算；日期必须晚于已有快照；档案/分类法/配置版本变化后才允许
  追加新快照。
- golden fixtures：[static_golden.json](../../tests/fixtures/scoring/static_golden.json) 与
  `tests/unit/test_static_score.py`。

### 能做什么

- 用确定性 A/E/S 公式和版本化配置生成静态分、分项解释与 confidence；
- 将首次/版本变更后的静态快照写入独立表；读取 history/latest；
- 用输入哈希、breakdown 和版本号审计“当时为什么是这个分”；
- 为 P5 提供最新静态快照输入，而不会产生假的每日完整快照。

### 不能做什么

- 不计算 F/H/U/D/P 或每日 priority/tier；不写完整 `score_snapshot`；
- 不做引用领域/年龄百分位的数据获取；citation percentile 由调用方提供，缺失按中性；
- 不做 venue/author 白名单治理；输入等级/作者分由调用方/配置提供；
- 不自动重算静态分；版本未变时仓储拒绝；
- 未调用真实 LLM；无真实模型/质量验收。

### P4-S8 输入与开工约束

- 输入：`StaticScoreResult`/`StaticScoreSnapshot`、latest static snapshot、R/A/E、
  `ClassificationResult`、`SummaryResult`、special nominations、`EnrichmentSignals`；
- P4-S8 负责把 screened 文献处理到 classified/scored 或 needs_review，写入审核队列；
- 完整 `score_snapshot` 留给 P5；P4-S8 不得伪造动态分/priority；
- 新字段/表继续新增迁移（当前 0007），测试保持 FakeLLM/fixtures。

### 回滚

- `alembic downgrade 0006` 删除 `static_score_snapshot`；先备份静态快照；
- 撤销 `scoring/static.py`、0007、测试/文档即可；`score_snapshot`/paper/run/event
  不受影响。

### 规模说明

P4-S7 超过 8 文件指引：A/E/S 公式、置信度/中性值、独立快照决策、迁移、行/映射/
仓储、golden 与文档共同构成本次竖切；未提前实现 P5 动态分或 P4-S8 编排。

## P4-S8 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s8-make-check'`：
  ruff format/check（189 文件）、strict mypy（**94 源文件**）通过；
  **827 passed / 4 e2e excluded**（P4-S7 后基线 822，新增 5 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s8-integration'`：
  **248 passed**（P4-S7 后基线 243；新增 2 项 process/review + 3 项 review API）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s8-e2e'`：**4 passed**；
  其中 onboarding e2e 已扩展为“G1–G4 → ingest → process → review list → API approve
  → classified”，使用真实 Uvicorn 与子进程 CLI。
- `TMPDIR=/dev/shm make migrate-check`：base → **0008** → base → 0008 通过。
- `make demo DEMO_PORT=8063`：自动升级到 0008，`/health`、`/papers` 正常。
- `make secret-scan`、`git diff --check` 通过；未调用真实 LLM/真实来源。
- process fixture：relevance/summary/classification/special_nomination 四段离线回放；
  低置信度分类进入 review，批准后转为 classified。

### 实现

- 迁移 `0008` / `review_item`：每篇一条，保存开放/已处理、原因、payload、处理人/
  时间/方式/备注；只追加历史由状态与时间保留。
- `pipeline/process.py`：enrichment → R → summary → classification → special →
  static；补齐缺失主题桶（`ensure_topic_buckets`），同版本静态快照复用；
  已有 enrichment 直接复用，缺摘要时 summary 置空并转 review；A/E 的
  author/citation/repro/rigor 为可选输入；汇总 needs_review 并打开审核项。
- `pipeline/review.py` + `storage/repositories.py`：
  - 列出/批准/重新打开；批准后 `needs_review → classified`；
  - relabel 用已确认分类法校验主/副标签，替换 `paper_tag` 与唯一 FILING 桶；
  - 用户标签 `assigned_by=user`、confidence=1，不伪造 Agent 溯源。
- API：`GET /review`、`POST /review/{paper_id}/approve`、
  `POST /review/{paper_id}/relabel`；统一错误信封。
- CLI：`papersteward process`（`--llm-fixture` 离线 / `--live` 显式真实）与
  `papersteward review list|approve|relabel`。
- 操作文档：[process-review-cli.md](../process-review-cli.md)。

### 能做什么

- 对 screened 文献一次完成 P4 富化/摘要/分类/特殊提名/静态分，并写标签/桶/快照；
- 低置信度、边界、摘要证据问题、特殊提名进入可审计审核队列；
- 人工批准或改主/副标签后，论文进入 classified，标签/归属桶原子替换；
- 离线 fixture 回放与真实 Uvicorn e2e 均可复现。

### 不能做什么

- 不计算动态分/优先级、不写完整 `score_snapshot`，不排序/入队/推送；
- 不处理日报、阅读反馈、归档（P6）；不实现特殊桶配额/展示（P7）；
- 无鉴权/多用户；只允许本机单用户；
- 审核队列每篇一条，不保留多次审核的完整事件史（仅当前状态+时间）；
- 未调用真实 LLM/真实来源，准确率未做真实模型验收。

### P5 交接

- 输入：`PaperStatus.CLASSIFIED` 文献、`static_score_snapshot.latest`、R/A/E/S、
  latest summary/classification/tags、review 已处理状态；
- P5 负责 F/H/U/D/P、queue/priority/tier 与完整 `score_snapshot`；不得修改静态快照
  历史，不得把 P4 review item 当作优先级；
- 复用 `review_item` 仅用于 P4 质量门，不扩展为 P6 阅读反馈。

### 回滚

- `alembic downgrade 0007` 删除 `review_item`；先备份审核记录；
- 撤销 process/review API/CLI/服务/测试/文档即可；paper/run/static snapshot 不受影响。

### 规模说明

P4-S8 超过 8 文件指引：审核表/仓储/服务/API/CLI、process 编排、桶补齐、真实 Uvicorn
e2e、操作文档共同构成本次竖切；未提前实现 P5 动态分或 P6 推送。

## P4 验收返工（F1–F5）

独立验收报告见 [P4-acceptance.md](P4-acceptance.md)，返工记录见
[P4-acceptance-rework.md](P4-acceptance-rework.md)。

- **F1**：process 不再无条件写中性 enrichment；已有行原样复用，仅缺失时创建。
- **F2**：缺摘要/摘要无法生成不再抛整批；summary 置空、记录原因、继续其余分项并打开
  review item。
- **F3**：process CLI 捕获 `LLMError`（可操作提示、无 traceback）；`--live` 在
  provider=fake 时明确拒绝。
- **F4**：A/E 输入边界明确；已有 enrichment 的 venue/code/data/OA 被使用，
  author/citation/repro/rigor 通过 `process_paper` 可选参数提供；CLI 人读输出含 R/A/E/S。
- **F5**：单一 `--llm-fixture` 只允许 `--paper`，批量逐篇或 `--live`。
- 返工门：`make check` **833 passed / 4 e2e excluded**；integration **254 passed**；
  e2e **4 passed**；migrate 0008；demo 8065；Docker 本地重建 healthy（UID 10001，
  head 0008）；CI 待 push 后重跑。
- 结论：**返工完成，待独立复验；P5 不开始**。

## 与 P5 的边界

- P4 只产出可审阅条目、摘要/分类/静态分；P5 才计算动态分、排序与解释；
- 任何真实 LLM 调用都必须显式授权；备注默认外发仍需 P6/P8 的单独授权流程；
- **P4 当前未通过独立验收（F1/F2 阻断）；P5 不得在 process 返工复验前开始。**
  详见 [P4 验收报告](P4-acceptance.md)。
