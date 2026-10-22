# Changelog

本文件记录 PaperSteward 的重要变更。当前版本为开发版，尚未发布 Alpha。

## [Unreleased]

### Fixed

- **P4 验收返工（F1–F5）**：process 复用已有 enrichment，不再用中性值覆盖来源数据；
  缺摘要/无法摘要转为 `needs_review` 并打开 review item，不使批次回滚；process CLI
  捕获 LLMError、`--live` 在 provider=fake 时拒绝、单一 fixture 仅允许 `--paper`；
  A/E 可选 author/citation/repro/rigor 输入并在 CLI 输出 R/A/E/S。返工门
  833 passed / 254 integration / 4 e2e；Docker 本地重建通过；CI 待 push 后重跑。

### Added

- **P4-S8**：`process` 离线编排（enrichment → R → 摘要 → 分类 → 特殊桶 → 静态分），
  低置信度/边界/特殊提名写入 `review_item`；新增 review API/CLI（列表、批准、
  人工改标签）、迁移 `0008` 与真实 Uvicorn process→review e2e。无新依赖，
  测试仅用 FakeLLM/fixtures。
- **P4 验收**：独立验收发现 F1–F5；F1/F2 阻断已返工（见 Fixed），P4 待独立复验，
  P5 在复验通过前不开始。
  详见 `docs/phases/P4-acceptance.md`。
- **P4-S7**：确定性静态分 A/E/S（venue/作者/引用、code/data/repro/rigor/open），
  confidence/中性值与分项 breakdown；迁移 `0007` 新增独立
  `static_score_snapshot`，同版本冻结、版本变化后按日期追加。完整每日
  `score_snapshot` 留给 P5。无新依赖、无真实 LLM。
- **P4-S6**：special_nomination 三类特殊桶提名：review 层覆盖、classic 依据、
  distant 四段 bridge memo、逐字证据与置信度；只提名不改归属，按
  review > classic > distant 选择默认 kind，低置信度/证据不足进入 review。
  无迁移，测试只用 FakeLLM。
- **P4-S5**：classification/v2 主/副标签、runner-up/置信度、逐字 evidence 与
  boundary_note；置信度/边界/证据不足进入 `needs_review`，标签与主题桶幂等写入
  `paper_tag`/`paper_bucket`，已有不同分类拒绝静默覆盖。无迁移，测试只用 FakeLLM。
- **P4-S4**：100 字概括 `summary/v2` 与 `pipeline/summarize.py`：四槽生成、逐字证据
  覆盖、数字 claim 对齐证据、空泛词检查与 `needs_review`；复用 `summary` 表持久化，
  保留 `summary/v1`。无迁移，测试只用 FakeLLM。
- **P4-S3**：相关度 R 管线：离线哈希 embedding、profile facet 提取、rubric prompt/
  `relevance` schema、逐字标题/摘要证据校验、55/65 band 与 `needs_review`。缺失/伪造
  证据用中性 2.5 降级；默认未校准（`calibrated=False`），不持久化/不改变状态。
- **P4-S2**：新增迁移 `0006` 与 `enrichment` 表，持久化每篇文献的富化信号、
  confidence/source 与可选 provenance；提供幂等 upsert、中性值回填和稳定读取。
  缺失字段保持 None/()/unknown，重复保存不推进 `updated_at`；无新依赖、无真实 LLM 调用。
- **P4-S1**：真实 LLM 入口：`PAPERSTEWARD_LLM_*` 配置、OpenAI-compatible
  `chat/completions` provider、显式 `llm ping --live` 授权、`SecretStr` 密钥、
  HTTPS/超时/响应上限/脱敏错误与网关按 `retryable` 重试；新增中性
  `EnrichmentSignals`/`EnrichmentProvider` 契约。无迁移、无新依赖，测试离线。
- **P3-S7**：G4 约束下的采集、去重/硬过滤、幂等入库与运行报告 CLI；来源失败隔离，
  暂停/安静时段及精确配置证据复核。迁移 `0005` 保存来源原始元数据版本；保护归档/
  已评分及校准数据，支持只读 dry-run、事务回滚与脱敏失败报告，真实 CLI 离线 e2e。
- **P3 验收**：独立复核本地阶段门、全新 clone、Docker 与 CI 均通过；P3-S1–S7
  可以关闭，阶段 tag 待发布流程。
- **P3 验收后优化**：跟进四项非阻断观察：早期 discovered/deduped 存量状态重跑收敛到
  screened/rejected；HTTP 缓存增加 32 MiB 总字节上限与 FIFO 淘汰；OpenAlex 身份消歧
  改为最多 3 页保守分页并拒绝歧义/未完整扫描；CLI fixture 支持按请求顺序回放多页
  `responses` 信封且耗尽不重复。无迁移、无新依赖，新增回归/契约测试。

- **P3-S6**：规范化 DOI/arXiv/文献类型，五类保守去重、强标识冲突保护和跨源版本合并；
  保留全部来源与最早公开日。摘要近似使用离线词法策略，未调用 embedding。

- **P3-S5**：Semantic Scholar bulk 搜索/分页适配器，保留引用/TLDR与原始记录，
  缺失元数据降级；可选密钥只放绑定来源的请求头。无真实来源/密钥调用。

- **P3-S4**：OpenAlex 适配器，支持搜索/基础分页、倒排摘要重建、缺字段降级及
  关注作者/venue/机构的精确名称解析；保留原始来源记录，测试完全离线。

- **P3-S3**：arXiv Atom 查询/解析/分页适配器，保留来源原始元数据；公共 HTTP 边界
  支持超时、限流、重试、预算和有界缓存。现有 httpx 提升 runtime；默认离线测试。

- **P3-S2**：从 G1 精确选定档案生成稳定关键词/布尔/关注列表查询；支持预算、
  字面排除短语与字段语义，拒绝未确认档案、缓存漂移和空检索输入。无网络/模型调用。

- **P3-S1**：来源适配器协议、查询与统一候选元数据、独立来源溯源/错误契约；
  版本化合成 cassette 与 FakeSourceAdapter 离线重放，覆盖失败/空结果及 API 兼容演示。
  建立 P3 七张小阶段卡与 ADR；无真实来源、生产 ingest、新依赖或迁移。
- **P2-S6**：最小 onboarding CLI，覆盖草案/版本审阅、种子导入、独立校准、日报反馈与
  G1–G4 显式确认。保存带校验和的私有审阅快照，确认只使用已审 hash/revision，
  拒绝文件覆盖、跨服务器或意外编辑；本地 HTTP 禁用代理/重定向，不自动重试变更。
  新增 CLI 真实子进程 e2e、操作指南与 P2 本地验收/P3 交接；无依赖/迁移。
- **P2 验收**：独立复核本地阶段门、Docker 与后续 CI（run 39643102708）通过；
  P2 通过，`p2-done` tag 留待发布流程。
- **P2-S5**：版本化试运行日报与运行参数 API，展示固定合成条目/Markdown，不发送；
  支持“太多/太少/不准/分类不对”的追加反馈。G4 要求最新日报、最新认可反馈、
  审阅双 hash 与显式确认，绑定 G3 及前序文档；原子保存所选配置、审计并进入 ACTIVE。
  新增真实 Uvicorn G1–G4 门禁 e2e；无迁移/依赖，不启动真实采集、评分或调度。
- **P2-S4**：G2 种子幂等导入、独立 15–20 篇校准集与双方不可覆盖标注 API；
  迁移 `0004` 持久化最多两轮校准。报告包含平均并列秩 Spearman、确定性 Top-5、
  标签/阅读方式一致率、边界争议与档案/rubric 建议；G3 绑定前序文档和完整报告，
  低指标需书面接受已知偏差，确认后进入 dry_run，G4 门禁继续关闭。
  合成 fixture 标记“仅流程验证”，无新依赖/真实调用，不生成生产评分或阅读反馈。
- **P2-S3**：分类法草案/版本编辑/审阅与 G2 确认 API；校验 5–8 主标签、种子覆盖率
  ≥90%、单标签占比 ≤50%、正例与边界判定；预览种子/队列/规则变化。草案绑定
  G1 档案并追加不可变证据；确认重算报告并保存快照，拒绝过期审阅。无新迁移/依赖，
  不导入论文、不执行重分类或真实调用；合成标注明确标为“仅流程验证”。
- **P2-S2**：档案 Markdown 草案/版本编辑/全文审阅 API，返回结构化与 Markdown diff、
  G1 缺项及最多 5 个协商问题；可信 G1 完整性校验、版本与内容哈希绑定的显式确认，
  同事务确认文档/追加事件/推进至分类法阶段。保留旧版本，拒绝陈旧/重复确认；
  UTF-8 文档限长并拒绝 YAML 引用展开，无新增依赖、迁移或真实模型调用。
- **P2-S1**：独立 onboarding 状态与可逆迁移 `0003`；G1–G4 顺序确认、产物版本/
  用户/变更摘要审计、revision 条件更新；`GET /onboarding` 与幂等
  `POST /onboarding/start`；统一门禁阻止 G4 前入队/推送/归档。内部确认接口必须
  注入可信业务证据校验器，具体校验与外部确认入口留给 S2–S5；无真实调用。
- **P0-S1**：`src` 包结构；`Settings` 环境变量/`.env` 配置；JSON/console 结构化日志、
  request_id 上下文与敏感字段脱敏；统一 `AppError` / `ErrorResponse` 与 FastAPI
  错误处理器。
- **P0-S2**：SQLAlchemy 2.x Engine/Session、`session_scope`、SQLite 外键与健康检查；
  最小 `papers` / `runs` 模型；Alembic 初始迁移 `0001`；临时 SQLite 测试夹具。
- **P0-S3**：FastAPI app factory 与 lifespan；`GET /health`、`GET /papers`；
  `X-Request-ID` 中间件；每请求 Session；API/schema 测试。
- **P0-S4**：`papersteward` 控制台命令；`doctor` 配置/DB/迁移检查；
  幂等 `demo seed`（`p001`）；Alembic current/head 查询与 CLI 集成测试。
- **P0-S5**：Makefile（bootstrap/check/test/test-integration/e2e/migrate-check/demo/
  secret-scan）；Dockerfile、Compose、非 root 运行与健康检查；
  GitHub Actions（Python 3.11/3.12 + Docker smoke）；README、AGENTS、CHANGELOG；
  真实 Uvicorn walking-skeleton e2e。
- **P1-S1**：纯领域层 `core/`：稳定枚举、15 个不可变实体/值对象、领域错误字段定位、
  文献/文档/运行状态机，以及主标签、归属桶、评分冻结、追加写、归档不可变、
  低置信度与推送门槛等不变量。新增 60+ 单元测试，`core/` 覆盖率 99%。
- **P1-S2**：正式领域表与迁移 `0002`（14 张表、部分唯一索引、CHECK、外键）；
  ORM `*Row` 与领域实体映射；13 个仓储；状态迁移同事务追加事件；事件/评分快照/
  归档快照只追加；P0 `papers`/`runs` 数据迁移；集成测试覆盖 up/down、约束、
  JSON/UTC 往返。`storage/` 覆盖率 96%。
- **P1-S3**：Markdown + YAML front matter 解析与字段级错误；profile/taxonomy
  Pydantic 契约与语义校验；基于实体的可读 diff；draft/confirm/archive 服务与
  `profile.*`/`taxonomy.*` 事件；`profile`/`taxonomy`/`doc diff` CLI；
  `tests/fixtures/profiles`/`taxonomies` 与测试。文档模块覆盖率 97%。
- **P1-S4**：`scoring.yaml` 加载/校验（层级权重和、wR/wS/wA 策略、gates、
  stability）；配置版本追加写与 `config.updated` 事件；分类/摘要/评分/事件
  Pydantic JSON schema 与运行时校验；`config validate/import`、
  `schema show/validate` CLI；正反例与字段级错误测试。
- **P1-S5**：可替换 `LLMProvider` 协议、确定性 `FakeLLM`、版本化 prompt registry、
  provider 重试 + schema 修复重试 1 次、`Provenance` 溯源、usage/成本累计；
  `llm ping --fake` CLI；合法/非法 JSON、重试、修复与不落库的测试。
- **P1-S6**：P1 contract freeze（枚举/14 张表/迁移/schema/prompt/CLI）、端到端
  P1 验收测试（profile/taxonomy/config + FakeLLM 分类/摘要）、完整阶段门证据与
  P2 handoff；未新增业务功能或迁移。
- **依赖**：新增运行时 `PyYAML` 与开发依赖 `types-PyYAML`，见 ADR 0001。

### Fixed

- Docker：宿主 umask 077 时源文件/迁移可能以 `600` 进入镜像，非 root 的
  `papersteward` 用户无法读取 `/app/src`，Alembic 启动报 `PermissionError`。
  Dockerfile 在切换用户前统一补齐 `a+rX`，并规范化工作区文件权限。
- P0 F1：未处理异常 500 响应缺少 `X-Request-ID` 响应头；统一错误响应现在回写
  `X-Request-ID`，并增加合同测试与“未迁移数据库触发 500”的集成回归测试。
- P1-S1–S3 验收返工 F1：taxonomy 确认前重新校验 5–9 主标签结构，非法 draft 不能
  被确认或写入状态/事件。
- 验收返工 F2：`## T06` 等缺少名称的主标签标题不再被静默忽略，报错并定位行号。
- 验收返工 F3：事件追加要求时间非递减；`PaperRepository.transition()` 先校验/追加
  事件再更新状态，回填事件不会留下半更新状态。
- 验收返工 F4：文档 DB 子命令捕获配置校验错误，输出 `PAPERSTEWARD_*` 字段提示，
  不再打印 traceback。
- 验收返工 F5：标签/桶仓储写入执行聚合校验（恰好一个主标签 / filing、副标签
  ≤5、批量必须同一文献），非法中间态需回滚。
- P1 整体验收返工 F1：`save_scoring_config()` 写入前重新执行跨字段校验，非法配置
  不写 `Config`、不写 `config.updated` 事件。
- P1 整体验收返工 F2：新增 `special_scoring` schema（tier，无 P），主题桶
  `scoring` 继续要求 priority；freeze/契约测试同步。
- P1 整体验收返工 F3：最终 provider 失败时 `LLMProviderError`/`LLMTimeoutError`
  携带 usage（calls/retries），可审计失败调用。

### Changed

- `docs/phases/P0-baseline.md` 记录了 P0 各小阶段的契约、验收证据与后续交接。
- 当前阶段为 P1；`docs/phases/P1-domain-documents-llm.md` 记录 S1–S3 契约、
  验收证据与 S4（配置/JSON schema）交接。
- `GET /papers` 的 `status` 从 stub 值 `new` 变为正式枚举 `discovered`；响应信封不变。
- `demo seed` 与 API 改用正式 `paper` 表；`doctor` 迁移版本显示 `0002`。
- 新增文档命令：`profile/taxonomy validate|import|confirm|archive`、`doc diff`；
  确认新版本会自动归档同一文档的旧 confirmed 版本。
- 新增配置/schema 命令：`config validate|import`、`schema show|validate`；配置版本
  只追加，schema 使用 `extra="forbid"`。
- 新增 `llm ping --fake`；P1 不提供真实 provider，超时由 provider 实现。
- P1 实现与本地验收完成（返工后 `340 passed`）；Docker 容器 build/run 与 CI
  run [39610871808](https://github.com/INKEM/PaperSteward/actions/runs/39610871808)
  均通过，F4/F6 已闭环；F5（httpx 弃用警告）保留为非阻断依赖债务，P1 整体验收通过。
- 仓储标签/桶写入改为聚合校验：首次写入必须是主标签 / filing；事件时间必须非递减。

### Security

- `.env` 不纳入版本控制；`make secret-scan` 扫描常见密钥模式；
- API 默认只监听 `127.0.0.1`；日志与错误响应不输出密钥/堆栈。

### Notes

- P0 的 `papers` / `runs` 已由迁移 `0002` 替换；升级会迁移最小字段，降级会丢弃
  正式表数据并恢复空的 stub 表；
- P1-S5/S6 只提供协议与 FakeLLM；真实 provider、分类/摘要/评分算法、真实文献源与
  P2 onboarding 在后续阶段；
- Docker post-fix 镜像 build/run 与 CI run
  [39610871808](https://github.com/INKEM/PaperSteward/actions/runs/39610871808)
  均已验证；F4/F6 闭环；
- F5（httpx/Starlette 弃用警告）保留为非阻断依赖债务；
- taxonomy 的“边界/常见误分”说明仅保留在 Markdown 正文，不进入机器字段；
- 当前没有真实文献源、LLM 摘要/分类、评分、日报或 Web UI；
- Docker 配置已加入并纳入 CI smoke。
