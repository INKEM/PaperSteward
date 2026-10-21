# Changelog

本文件记录 PaperSteward 的重要变更。当前版本为开发版，尚未发布 Alpha。

## [Unreleased]

### Added

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
