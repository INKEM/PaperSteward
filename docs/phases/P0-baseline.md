# P0 工程基线与 Walking Skeleton

> **阶段**：P0｜**里程碑**：Alpha 基础｜**状态**：进行中（P0-S1–P0-S4 完成）
> **依据**：[大阶段开发计划](../PaperSteward_大阶段开发计划.md) §3 P0
> **最后更新**：2026-10-21

## 1. 阶段目标

新机器 clone 后 10 分钟内跑起 API、CLI、数据库、迁移和测试；建立后续所有阶段的
工程约束（包结构、配置、日志、错误契约、测试分层）。

## 2. 小阶段拆分

| 小阶段 | 目标 | 状态 | 依赖 | 主要验收 |
|--------|------|------|------|----------|
| P0-S1 | 包结构、配置、日志、错误响应 | **完成** | — | ruff/mypy/pytest；配置错误可定位 |
| P0-S2 | DB 连接、Alembic、临时测试库 | **完成** | S1 | 迁移 up/down；仓储 CRUD |
| P0-S3 | API 骨架、health、papers stub | **完成** | S1/S2 | `curl /health`；`GET /papers` |
| P0-S4 | CLI 骨架、demo seed | **完成** | S1–S3 | `papersteward doctor`；`demo seed` |
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

## 4. P0-S2 执行记录

### 4.1 目标

建立 SQLAlchemy 2.x 引擎/会话生命周期、Alembic 迁移基线与完全隔离的临时测试库，
并创建 P0 所需的最小 `papers` / `runs` 表，为 S3 的 API 与 S4 的 demo seed 提供
可查询的数据层。本阶段不引入 P1 的领域模型和业务约束。

### 4.2 交付物

| 文件 | 作用 |
|------|------|
| `src/papersteward/storage/db.py` | Engine/会话工厂、`session_scope`、SQLite 外键、健康检查 |
| `src/papersteward/storage/models.py` | 最小 `Paper` / `Run` ORM 模型 |
| `src/papersteward/storage/repositories.py` | `PaperRepository` 最小 CRUD |
| `src/papersteward/storage/__init__.py` | 稳定导出面，供 API/CLI/任务复用 |
| `alembic.ini` | Alembic 配置；URL 由 `migrations/env.py` 从 Settings 读取 |
| `migrations/env.py` | 在线/离线迁移、连接注入、`render_as_batch` |
| `migrations/versions/0001_initial_schema.py` | 初始 `papers` / `runs` 迁移 |
| `migrations/script.py.mako`、`migrations/README` | 迁移模板与操作说明 |
| `tests/integration/conftest.py` | 临时 SQLite 引擎、建表、会话工厂、db_session fixture |
| `tests/integration/test_database.py` | 引擎/会话/事务/健康检查测试 |
| `tests/integration/test_repositories.py` | 仓储 CRUD、排序分页、重复 ID、运行表测试 |
| `tests/integration/test_migrations.py` | upgrade/downgrade、幂等、模型一致性、迁移后仓储测试 |

依赖新增：`sqlalchemy>=2.0,<3.0`、`alembic>=1.13,<2.0`
（本地验证版本：SQLAlchemy 2.1.5、Alembic 1.20.0）。

### 4.3 数据模型

| 表 | 字段 | 说明 |
|----|------|------|
| `papers` | `id TEXT(64) PK`、`title TEXT NOT NULL`、`status TEXT(32) NOT NULL DEFAULT 'new'`、`created_at DATETIME NOT NULL` | 最小文献记录 |
| `runs` | `id TEXT(64) PK`、`kind TEXT(32) NOT NULL`、`status TEXT(32) NOT NULL DEFAULT 'running'`、`started_at DATETIME NOT NULL`、`finished_at DATETIME NULL`、`error TEXT NULL` | 最小运行记录 |

- 两表都是 **P0 stub**；P1 用正式领域迁移替换，不在此基础上扩展业务字段。
- 应用写入时间统一使用 `utc_now()`（带 UTC 时区）；SQLite 读取回来会是 naive
  datetime，这是 SQLite 的限制，P1 决定是否引入 `UTCDateTime` 类型装饰器。
- 目前只提供 `PaperRepository`；`runs` 的仓储与报告字段在 P3 引入。

### 4.4 连接与事务契约

- `create_db_engine(database_url=None)`：为空时读 `Settings.db_url`；默认 SQLite
  路径会自动创建 `data_dir`，显式 URL 只创建数据库文件父目录。
- SQLite：`check_same_thread=False`；内存库使用 `StaticPool` 共享连接；每个连接
  执行 `PRAGMA foreign_keys=ON`。
- `create_session_factory(engine)`：`autoflush=False`、`expire_on_commit=False`。
- `session_scope(factory)`：正常退出提交，异常回滚，最后关闭；CLI/任务优先使用它。
- `check_database(engine)`：执行 `SELECT 1`，异常时返回 `False`，供 S3 `/health` 使用。
- `PaperRepository`：只 `add`/`flush`，**不自行 commit**；事务边界由调用方控制。
  提供 `create`、`get`、`list`、`count`、`update_status`、`delete`。
- 排序契约：`list()` 按 `created_at ASC, id ASC`，支持 `limit`/`offset`。

### 4.5 迁移契约

- 迁移版本：`0001`（`initial papers and runs`），无前置 revision。
- `alembic.ini` 不含实际 URL；`env.py` 从 Settings 读取，支持
  `PAPERSTEWARD_DATABASE_URL` 或默认 `data_dir/papersteward.db`。
- 测试/脚本可通过 `config.attributes["connection"]` 注入现成连接，避免触碰本地库；
  此时不重新配置全局日志。
- SQLite 开启 `render_as_batch=True`，为后续 ALTER TABLE 迁移做准备。
- `downgrade base` 删除 `papers` / `runs`，按 Alembic 规范保留 `alembic_version`。
- `alembic check` 已确认迁移与 ORM metadata 无差异。

### 4.6 验收命令与结果

```bash
.venv/bin/ruff format --check src tests migrations
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q
```

2026-10-21 本地执行结果：

- `ruff format --check`：31 个文件已格式化；
- `ruff check`：通过；
- `mypy`（strict）：20 个源文件无错误；
- `pytest`：**58 passed**，0 failed；
- 覆盖率：`storage/db.py` 97%，S1+S2 模块整体 95%；
- 唯一警告仍是上游 `starlette.testclient` 的 `httpx2` 迁移提示。

临时数据库 CLI 演练（不触碰 `var/papersteward.db`）：

```bash
export PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:////tmp/papersteward-s2.db"
.venv/bin/alembic upgrade head
.venv/bin/alembic current
.venv/bin/alembic downgrade base
rm -f /tmp/papersteward-s2.db
```

观察结果：

- `upgrade head` 后表为 `alembic_version`、`papers`、`runs`，`current` 为 `0001 (head)`；
- `papers` 列为 `id/title/status/created_at`，`runs` 列为
  `id/kind/status/started_at/finished_at/error`；
- `downgrade base` 后仅保留 `alembic_version`；
- 再次 upgrade 可重复执行，迁移幂等。

### 4.7 已知限制与偏差

- 迁移基础设施首次接入需要固定模板文件，本小阶段文件数超过计划的“≤8 个文件”指引；
  没有引入额外业务模块，后续小阶段恢复小步提交。
- `papers` / `runs` 是 stub，不含主标签、桶、评分等不变量；P1 会替换。
- SQLite 读取的 datetime 为 naive；写入约定为 UTC，P1 再决定是否做类型级规范化。
- 非 SQLite 的 Engine 分支已保留但未测试（P1 若启用 Postgres 再补契约测试）。
- `make check` / `migrate-check` 属于 S5；本阶段用等价分项命令验收。

## 5. P0-S3 执行记录

### 5.1 目标

建立可启动的 FastAPI 应用、健康检查与只读 papers stub 接口，并把 S1/S2 的
错误响应、请求上下文、数据库会话真正接入 API 层。

### 5.2 交付物

| 文件 | 作用 |
|------|------|
| `src/papersteward/api/app.py` | `create_app()`、lifespan、注入/释放 Engine、挂载中间件与路由 |
| `src/papersteward/api/routes.py` | `GET /health`、`GET /papers` |
| `src/papersteward/api/deps.py` | 每请求一个 Session，关闭交给依赖清理 |
| `src/papersteward/api/schemas.py` | `HealthResponse`、`PaperRead`、`PaperListResponse` |
| `src/papersteward/api/middleware.py` | `X-Request-ID` 生成/透传与日志上下文 |
| `src/papersteward/api/errors.py` | 错误处理器优先读取 `request.state.request_id` |
| `src/papersteward/main.py` | ASGI 入口与进程日志配置 |
| `src/papersteward/api/__init__.py` | 导出 `create_app` |
| `tests/integration/test_api.py` | 健康检查、papers、请求 ID、错误信封、OpenAPI、main 测试 |
| `tests/unit/test_schemas.py` | 时间字段 UTC 规范化正反例 |

### 5.3 API 契约

**`GET /health`**

```json
{
  "status": "ok",
  "version": "0.1.0.dev0",
  "environment": "dev",
  "database": "ok"
}
```

- 200：应用可用且 `SELECT 1` 成功；
- 503：数据库不可用，`status="degraded"`、`database="unavailable"`；
- 只检查连接，不检查迁移版本或表是否存在；schema 由 Alembic 负责；
- 目前只注册 GET（HEAD 返回 405）。

**`GET /papers`**

```json
{
  "items": [
    {
      "id": "p001",
      "title": "Retrieval-Augmented Generation",
      "status": "new",
      "created_at": "2026-10-21T08:23:54.959778Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

- Query：`limit` 默认 50，范围 1–200；`offset` 默认 0，最小 0；
- 排序：`created_at ASC, id ASC`；
- 空库返回 `items: []`、`total: 0`，不返回 404；
- `created_at` 在 API 边界统一为 UTC；非法参数返回 S1 的 422 错误信封；
- 只读接口，不提交事务；详情/创建接口留到后续阶段。

**请求 ID**

- 合法 `X-Request-ID`（`[A-Za-z0-9._:-]{1,128}`）透传；否则生成 UUID4 hex；
- 同时写入 `request.state`、日志 contextvar、响应头与错误响应体；
- 未处理异常由最外层 ServerErrorMiddleware 处理时，仍从 `request.state` 关联 ID。

**生命周期**

- `create_app(engine=...)` 注入临时 Engine 时由调用方 dispose，测试不复用生产库；
- 不注入时在 lifespan 中按 `Settings.db_url` 创建，并在关闭时 dispose；
- 启动不自动建表/迁移；需先执行 `alembic upgrade head`，否则 `/papers` 会因缺表返回 500。

### 5.4 验收命令与结果

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q
.venv/bin/pytest tests/integration/test_api.py -q
```

2026-10-21 本地结果：

- `ruff format --check`：39 个文件已格式化；
- `ruff check`：通过；
- `mypy`（strict）：26 个源文件无错误；
- `pytest`：**69 passed**，0 failed；S3 新增 9 个 API 测试 + 2 个 schema 单元测试；
- 覆盖率：整体 95%；`api/app.py` 94%、`api/errors.py` 83%、`main.py` / `api/schemas.py` 100%；
- 唯一警告仍是上游 `starlette.testclient` 的 `httpx2` 迁移提示。

实际 Uvicorn 演练（临时库 + 迁移 + 种子数据）：

```bash
export PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:////tmp/papersteward-s3.db"
.venv/bin/alembic upgrade head
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/health
curl -s "http://127.0.0.1:8000/papers?limit=10"
```

观察结果：

- `/health` 返回 200、版本 `0.1.0.dev0`、`environment=dev`、`database=ok`；
- 种子 `p001` 出现在 `/papers` 中，`total=1`，`created_at` 带 UTC `Z`；
- 合法 `X-Request-ID` 原样回写响应头；
- 空库 `/papers` 返回 `{"items":[],"total":0,"limit":50,"offset":0}`；
- 数据库不可用时 `/health` 的 503 路径由 TestClient 集成测试覆盖。

### 5.5 已知限制与偏差

- 只实现只读列表；没有 `GET /papers/{id}`、创建、更新接口。
- 应用不自动迁移；S5 的 bootstrap/Makefile 必须显式串联 `alembic upgrade head`。
- `/health` 是连接级检查，不校验 schema 版本；迁移状态由 S4 `doctor` 补充。
- 只支持 GET；若后续 Docker healthcheck 需要 HEAD，可再加路由。
- 本地单用户、无鉴权、无 CORS 配置，符合 P0 范围。
- 仍未引入 CLI/Makefile/Docker/CI，这些属于 S4/S5。

## 6. P0-S4 执行记录

### 6.1 目标

提供可直接运行的 `papersteward` CLI：`doctor` 检查配置/数据库/迁移状态，`demo seed`
幂等写入 P0 演示文献，使 `GET /papers` 能看到演示数据；CLI 失败时给出可操作提示，
不静默建表或执行迁移。

### 6.2 交付物

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | 新增 `[project.scripts] papersteward = "papersteward.cli.main:main"` |
| `src/papersteward/cli/main.py` | argparse 子命令、doctor/demo seed 输出与退出码 |
| `src/papersteward/cli/doctor.py` | 配置、数据库、迁移三项检查与 `DoctorReport` |
| `src/papersteward/cli/demo.py` | `p001` 演示数据与幂等 `seed_demo_data()` |
| `src/papersteward/storage/migrations.py` | 查找 `alembic.ini`、读取 current/head |
| `src/papersteward/cli/__init__.py` | CLI 包说明 |
| `tests/integration/conftest.py` | 新增 `migrated_engine` 临时迁移库 fixture |
| `tests/integration/test_cli.py` | 14 个 doctor / demo seed / CLI 失败路径测试 |

### 6.3 CLI 契约

```bash
papersteward --version
papersteward doctor
papersteward demo seed
```

**`doctor`**

- 检查项：`配置`、`数据库连接`、`迁移版本`；
- 配置错误展示具体 `PAPERSTEWARD_*` 变量，不打印配置对象或密钥；
- 数据库检查执行 `SELECT 1`；失败时迁移项标记为“无法检查”；
- 迁移检查比较 Alembic current 与脚本 head，**只读不升级**；
- 全部通过退出 0，否则退出 1；不自动修复、不自动迁移；
- 典型输出：

```text
PaperSteward 0.1.0.dev0
[OK] 配置: env=dev, data_dir=/.../var
[OK] 数据库连接: ok
[OK] 迁移版本: current=0001 head=0001
结果：通过
```

**`demo seed`**

- 固定写入 `p001` / “Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks”；
- 已存在时不更新、不重复插入，输出“已存在”；
- 使用 `session_scope` + `PaperRepository`，单次事务提交；
- 数据库未迁移/不可用时返回 1，并提示运行 `alembic upgrade head`；
- 配置非法时返回 1，并输出环境变量级错误；
- 不自动建表、不自动迁移。

**命令骨架**

- `demo` 是父命令，`seed` 是子命令；后续可在 `demo` 下扩展；
- 暂不提供 `serve`；启动 API 仍使用
  `uvicorn papersteward.main:app --host 127.0.0.1 --port 8000`；
- `argparse` 实现，未引入新的 CLI 依赖。

### 6.4 验收命令与结果

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest -q
.venv/bin/pytest tests/integration/test_cli.py -q
```

2026-10-21 本地结果：

- `ruff format --check`：44 个 Python 文件已格式化；
- `ruff check`：通过；
- `mypy`（strict）：30 个源文件无错误；
- `pytest`：**83 passed**，0 failed；其中 CLI 集成测试 14 个；
- 覆盖率：整体 94%；`cli/main.py` 89%、`cli/doctor.py` 94%、
  `storage/migrations.py` 79%（未覆盖多 head / 无脚本分支）；
- 唯一警告仍是上游 `starlette.testclient` 的 `httpx2` 迁移提示。

实际验收链路（临时数据库）：

```bash
export PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:////tmp/papersteward-s4.db"
.venv/bin/alembic upgrade head
.venv/bin/papersteward doctor
.venv/bin/papersteward demo seed
.venv/bin/papersteward demo seed
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8125
curl -s http://127.0.0.1:8125/papers
```

观察结果：

- `doctor` 退出 0，显示 `current=0001 head=0001`；
- 第一次 `demo seed`：`已插入演示文献 p001（当前共 1 篇）`；
- 第二次：`演示文献 p001 已存在（当前共 1 篇）`，总数仍为 1；
- `/papers` 返回 `p001`，`total=1`，`created_at=2026-10-21T08:00:00Z`；
- 未迁移数据库时 `doctor` / `demo seed` 均退出 1，后者提示
  `alembic upgrade head`；
- 非法配置（如 `PAPERSTEWARD_API_PORT=70000`）退出 1 并指出对应变量。

### 6.5 已知限制与偏差

- `alembic.ini` 与 `migrations/` 位于仓库根目录；CLI 先查当前目录，再查包所在
  仓库根目录。仅安装 wheel、未带迁移文件的部署会无法完成 `doctor` 迁移检查；
  S5 的 Docker 镜像必须复制 `alembic.ini` 与 `migrations/`。
- `doctor` 只诊断，不自动执行迁移；这是有意的安全边界。
- 演示数据固定为 `p001`，不提供自定义 fixture 参数；后续 `demo` 子命令可扩展。
- 没有 `serve` 子命令；Uvicorn 启动命令见上。
- CLI 输出为中文人类可读文本，暂无 `--json` / `--quiet`。
- Makefile/Docker/CI/README 仍属于 S5。

## 7. 交接给 P0-S5

**已就绪**

- 控制台命令 `papersteward`，以及 `doctor` / `demo seed` 子命令；
- `alembic upgrade head`、`alembic downgrade base` 与迁移 current/head 查询；
- `uvicorn papersteward.main:app` 可启动的 API、`/health`、`GET /papers`；
- 临时 SQLite 测试 fixture：`sqlite_engine`、`schema_engine`、`migrated_engine`、
  `db_session`；
- 可直接串联的验收链：migrate → doctor → demo seed → API 验证。

**P0-S5 需要完成**

- `Makefile`：`bootstrap`、`check`、`test`、`test-integration`、`migrate-check`、
  `demo`、`e2e`；
- `make bootstrap`：创建环境、安装依赖、准备 `.env`、执行 `alembic upgrade head`；
- `make check`：ruff format check、ruff check、mypy、pytest；
- `make demo`：迁移 → `papersteward demo seed` → 启动 API → 验证 `/health` 与
  `/papers`；
- `make migrate-check`：在临时库执行 upgrade head / downgrade base，不碰生产库；
- Docker Compose：单机、绑定 `127.0.0.1`、数据卷持久化、启动前迁移、
  healthcheck 使用 GET `/health`；
- CI：Python 3.11/3.12 矩阵、无真实网络/LLM、ruff/mypy/pytest、secret scan；
- README、AGENTS.md、CHANGELOG、`.env.example` 完整化；
- 阶段门清单、`docs/phases/P0-*.md` handoff、tag `p0-done`。

**不能做什么**

- 不引入 P1 领域模型、真实文献源、LLM、评分或 UI；
- 不实现多用户/鉴权/Postgres；
- 不在 S5 修复业务语义，只固化工程门禁与运行方式。
