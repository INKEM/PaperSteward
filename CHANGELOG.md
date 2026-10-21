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
- **P1-S4**：`scoring.yaml` 加载/校验（层级权重和、wR/wS/wA 策略、gates、
  stability）；配置版本追加写与 `config.updated` 事件；分类/摘要/评分/事件
  Pydantic JSON schema 与运行时校验；`config validate/import`、
  `schema show/validate` CLI；正反例与字段级错误测试。
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
- 仓储标签/桶写入改为聚合校验：首次写入必须是主标签 / filing；事件时间必须非递减。

### Security

- `.env` 不纳入版本控制；`make secret-scan` 扫描常见密钥模式；
- API 默认只监听 `127.0.0.1`；日志与错误响应不输出密钥/堆栈。

### Notes

- P0 的 `papers` / `runs` 已由迁移 `0002` 替换；升级会迁移最小字段，降级会丢弃
  正式表数据并恢复空的 stub 表；
- P1-S4 尚无 LLM 网关；分类/摘要/评分算法与真实文献源在 P3–P5；
- 独立验收 F6（httpx/Starlette 弃用警告）与 F7（Docker/CI 运行证据）仍保留为
  依赖/发布债务；
- taxonomy 的“边界/常见误分”说明仅保留在 Markdown 正文，不进入机器字段；
- 当前没有真实文献源、LLM 摘要/分类、评分、日报或 Web UI；
- Docker 配置已加入并纳入 CI smoke；本地无 daemon 时以 CI 结果为准。
