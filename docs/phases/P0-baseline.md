# P0 工程基线与 Walking Skeleton

> **阶段**：P0｜**里程碑**：Alpha 基础｜**状态**：进行中（P0-S1 完成）
> **依据**：[大阶段开发计划](../PaperSteward_大阶段开发计划.md) §3 P0
> **最后更新**：2026-10-21

## 1. 阶段目标

新机器 clone 后 10 分钟内跑起 API、CLI、数据库、迁移和测试；建立后续所有阶段的
工程约束（包结构、配置、日志、错误契约、测试分层）。

## 2. 小阶段拆分

| 小阶段 | 目标 | 状态 | 依赖 | 主要验收 |
|--------|------|------|------|----------|
| P0-S1 | 包结构、配置、日志、错误响应 | **完成** | — | ruff/mypy/pytest；配置错误可定位 |
| P0-S2 | DB 连接、Alembic、临时测试库 | 待开始 | S1 | 迁移 up/down；仓储 CRUD |
| P0-S3 | API 骨架、health、papers stub | 待开始 | S1/S2 | `curl /health`；`GET /papers` |
| P0-S4 | CLI 骨架、demo seed | 待开始 | S1–S3 | `papersteward doctor`；`demo seed` |
| P0-S5 | Makefile、Docker、CI、AGENTS/README | 待开始 | S1–S4 | `make check` / `make demo` |

> **顺序说明**：`make check` 属于 P0-S5，因此 S1–S4 先使用等价的分项命令验收；
> S5 再把它们固化为 Makefile 与 CI，不改变各分项检查的语义。

## 3. P0-S1 执行记录

### 3.1 目标

建立可安装的 `src` 包结构与本地配置入口；提供结构化日志与统一 API 错误响应契约，
为 S2 的数据库、S3 的 API、S4 的 CLI 提供共同基础。

### 3.2 交付物

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | setuptools 构建、依赖、ruff/mypy/pytest 配置 |
| `src/papersteward/config.py` | `Settings`、环境变量/.env 加载、校验与错误提示 |
| `src/papersteward/observability/logging.py` | JSON/console 日志、request_id 上下文、敏感字段脱敏 |
| `src/papersteward/errors.py` | 错误码、`AppError` 层级、错误响应模型 |
| `src/papersteward/api/errors.py` | FastAPI 异常处理器，统一错误信封 |
| `src/papersteward/{core,storage,documents,llm,sources,pipeline,scoring,delivery,api,cli,jobs}/` | 后续阶段模块占位包 |
| `tests/unit/`、`tests/contract/` | 配置、日志、错误与 API 错误契约测试 |
| `.env.example`、`.gitignore` | 本地配置模板与忽略规则 |

### 3.3 配置契约

所有配置使用 `PAPERSTEWARD_` 前缀，环境变量优先于 `.env`：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PAPERSTEWARD_APP_ENV` | `dev` | `dev` / `test` / `prod` |
| `PAPERSTEWARD_DATA_DIR` | `<cwd>/var` | 数据目录；相对路径按启动目录解析 |
| `PAPERSTEWARD_DATABASE_URL` | 空 | 留空则使用 `<DATA_DIR>/papersteward.db` |
| `PAPERSTEWARD_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`，大小写不敏感 |
| `PAPERSTEWARD_LOG_FORMAT` | `json` | `json`（结构化）/ `console`（本地阅读） |
| `PAPERSTEWARD_API_HOST` | `127.0.0.1` | 默认只绑定本机 |
| `PAPERSTEWARD_API_PORT` | `8000` | 1–65535 |
| `PAPERSTEWARD_TIMEZONE` | `Asia/Shanghai` | IANA 时区名 |

- `Settings.db_url` 是 S2 使用的 SQLAlchemy 连接串；`ensure_data_dir()` 负责创建目录。
- 时区通过 `zoneinfo` 校验；依赖 `tzdata` 作为系统时区库缺失时（如 slim 容器）的回退。
- 配置错误由 `format_settings_error()` 转成带环境变量名的中文提示，供 S4 `doctor` 使用。
- 非法端口、时区、日志级别、枚举值在进程启动时直接失败，不做静默降级。

### 3.4 日志契约

- 默认 JSON Lines，字段：`timestamp`（UTC，ISO 8601，`Z` 结尾）、`level`、`logger`、
  `message`；绑定请求后增加 `request_id`。
- 业务上下文通过 `logger.info(..., extra={...})` 输出为顶层字段；异常堆栈写入
  `exception` 字段。
- `request_id` 通过 contextvar 绑定；API 中间件应同时写入 `request.state.request_id`，
  以便最外层未处理异常处理器在中间件退出后仍能关联日志与响应。
- 键名命中 `api_key/token/secret/password/authorization/cookie` 的值（含嵌套 dict/list）
  与 `SecretStr` 一律输出 `***`；日志中不输出完整配置对象。
- `configure_logging()` 幂等，可重复调用；只替换自己的 handler，不干扰 pytest/uvicorn。

### 3.5 错误响应契约

```json
{
  "error": {
    "code": "not_found",
    "message": "文献 p001 不存在",
    "details": {"paper_id": "p001"},
    "request_id": "req-123"
  }
}
```

| 来源 | HTTP | `code` |
|------|------|--------|
| `InvalidRequestError` | 400 | `invalid_request` |
| `NotFoundError` | 404 | `not_found` |
| `ConflictError` | 409 | `conflict` |
| `ConfigurationError` | 500 | `configuration_error` |
| FastAPI 参数校验失败 | 422 | `validation_error` |
| 404 / 405 路由错误 | 404 / 405 | `not_found` / `method_not_allowed` |
| 其它 `HTTPException` | 原状态码 | `http_error` |
| 未处理异常 | 500 | `internal_error` |

- `details` 为结构化上下文，禁止放密钥或完整请求体。
- 未处理异常只返回通用文案；完整堆栈只进日志。
- 参数校验详情只保留 `field/message/type`，不回显用户输入值。

### 3.6 验收命令与结果

```bash
.venv/bin/ruff format --check src tests
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q
```

2026-10-21 在本地执行结果：

- `ruff format --check`：22 个文件已格式化；
- `ruff check`：通过；
- `mypy`（strict，`src/papersteward`）：17 个源文件无错误；
- `pytest`：**37 passed**，0 failed；S1 三个模块的行覆盖率 94%（防御性 fallback 分支未覆盖）；
- 唯一警告来自上游 `starlette.testclient`：提示未来从 `httpx` 迁移到 `httpx2`，
  不影响当前行为，留给 P0-S5 固定测试依赖时处理。

配置演示：

```bash
.venv/bin/python -c "from papersteward.config import Settings; print(Settings(_env_file='.env.example').model_dump_json(indent=2))"
```

### 3.7 与计划的偏差

- 本小阶段是**一次性工程脚手架**，提交文件数超过计划建议的“≤8 个文件”；其中 12 个
  是后续模块的单行 `__init__.py`，没有业务行为。后续每个小阶段仍按 ≤8 个文件控制。
- P0-S1 尚未提供 `make check` / README / CI，因为对应工作包是 P0-S5；本阶段用等效的
  分项命令验收，避免提前扩大范围。
- 依赖选择遵循计划 §2.2 的技术基线，未引入计划外框架；`uvicorn` 为 S3 启动 API 预留。

## 4. 交接给 P0-S2

**已就绪**

- `Settings`、`get_settings()`、`Settings.db_url`、`Settings.ensure_data_dir()`；
- `PAPERSTEWARD_DATABASE_URL` 覆盖与 SQLite 默认路径；
- 统一错误模型与 FastAPI 处理器安装函数 `install_error_handlers(app)`；
- 结构化日志配置入口 `configure_logging()`；
- 测试分层目录与 `clean_papersteward_env` fixture。

**P0-S2 需要完成**

- SQLAlchemy 2.x engine/session、连接生命周期；
- Alembic 初始化与空库 `upgrade head` / `downgrade base`；
- 临时 SQLite 测试库 fixture（不复用 `var/papersteward.db`）；
- 最小 `papers` / `runs` 迁移，为 S3/S4 提供可查询数据；
- 迁移/仓储测试；不引入 P1 领域模型。

**不能做什么**

- 还没有 HTTP 应用和 `/health`（S3）；
- 还没有 CLI 入口（S4）；
- 还没有 Docker/CI/Makefile（S5）；
- 配置中尚无 LLM/来源/评分参数（P1 起按需增加）。
