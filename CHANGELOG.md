# Changelog

本文件记录 PaperSteward 的重要变更。当前版本为开发版，尚未发布 Alpha。

## [Unreleased]

### Added

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
- **依赖**：新增运行时 `PyYAML` 与开发依赖 `types-PyYAML`，见 ADR 0001。

### Fixed

- F1：未处理异常 500 响应缺少 `X-Request-ID` 响应头；统一错误响应现在回写
  `X-Request-ID`，并增加合同测试与“未迁移数据库触发 500”的集成回归测试。

### Changed

- `docs/phases/P0-baseline.md` 记录了 P0 各小阶段的契约、验收证据与后续交接。
- 当前阶段为 P1；`docs/phases/P1-domain-documents-llm.md` 记录 S1–S3 契约、
  验收证据与 S4（配置/JSON schema）交接。
- `GET /papers` 的 `status` 从 stub 值 `new` 变为正式枚举 `discovered`；响应信封不变。
- `demo seed` 与 API 改用正式 `paper` 表；`doctor` 迁移版本显示 `0002`。
- 新增文档命令：`profile/taxonomy validate|import|confirm|archive`、`doc diff`；
  确认新版本会自动归档同一文档的旧 confirmed 版本。

### Security

- `.env` 不纳入版本控制；`make secret-scan` 扫描常见密钥模式；
- API 默认只监听 `127.0.0.1`；日志与错误响应不输出密钥/堆栈。

### Notes

- P0 的 `papers` / `runs` 已由迁移 `0002` 替换；升级会迁移最小字段，降级会丢弃
  正式表数据并恢复空的 stub 表；
- P1-S3 尚无评分 YAML 校验、JSON schema、LLM 调用或真实文献源；
- taxonomy 的“边界/常见误分”说明仅保留在 Markdown 正文，不进入机器字段；
- 当前没有真实文献源、LLM 摘要/分类、评分、日报或 Web UI；
- Docker 配置已加入并纳入 CI smoke；本地无 daemon 时以 CI 结果为准。
