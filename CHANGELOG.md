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

### Changed

- `docs/phases/P0-baseline.md` 记录了 P0 各小阶段的契约、验收证据与后续交接。

### Security

- `.env` 不纳入版本控制；`make secret-scan` 扫描常见密钥模式；
- API 默认只监听 `127.0.0.1`；日志与错误响应不输出密钥/堆栈。

### Notes

- P0 的 `papers` / `runs` 是 stub 表，P1 将替换；
- 当前没有真实文献源、LLM 摘要/分类、评分、日报或 Web UI；
- Docker 配置已加入并纳入 CI smoke；本地无 daemon 时以 CI 结果为准。
