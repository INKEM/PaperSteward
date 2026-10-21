# PaperSteward

本地优先的文献自动挖掘、优先级排序与每日推送系统。

当前处于 **P1 领域模型、版本化文档与 LLM 网关**：P0 的配置、API、CLI 与演示链路
已跑通；P1-S1 提供不可变领域实体、状态机与不变量，P1-S2 接入正式领域表、仓储与
事件日志，P1-S3 完成 profile/taxonomy 解析、版本确认/归档与 diff，P1-S4 完成
评分配置校验、JSON schema 与配置版本事件，P1-S5 完成 LLM 网关、FakeLLM、prompt
registry 与修复重试；P1-S6 完成契约冻结、端到端验收与阶段门。P1 实现与本地验收
完成；Docker/CI 运行证据与依赖弃用警告仍是发布债务，P2 尚未开始。

## 当前能力

- `GET /health`：返回版本、运行环境与数据库连通性；
- `GET /papers`：从正式 `paper` 表返回文献列表（只读）；
- `papersteward doctor`：检查配置、数据库连接与 Alembic current/head；
- `papersteward demo seed`：幂等写入演示文献 `p001`；
- SQLite + SQLAlchemy 2.x + Alembic；
- 结构化日志、`X-Request-ID`、统一错误响应；
- 纯领域层 `core/`：稳定枚举、不可变实体、文献/文档/运行状态机、分类/桶/评分/
  追加写等不变量；
- 正式领域表与仓储：14 张表、部分唯一索引/CHECK/FK、事件与评分快照追加写；
- profile/taxonomy：Markdown + YAML front matter 校验、版本确认/归档、实体 diff；
- scoring.yaml：权重和、策略下限/上限、gates 与 stability 校验；配置版本追加写；
- Agent 输出 JSON schema：分类、摘要、评分快照、事件（正反例校验）；
- LLM 网关：provider 协议、FakeLLM、版本化 prompt、schema 修复重试、usage/成本；
- Makefile、Docker Compose、GitHub Actions CI。

## 环境要求

- Python **3.11+**（推荐 3.12）
- `make`
- `curl`（仅 `make demo` 使用）
- Docker + Docker Compose（可选，用于容器运行）

## 快速开始

```bash
git clone https://github.com/INKEM/PaperSteward.git
cd PaperSteward

make bootstrap
make check
make demo
```

`make bootstrap` 会创建 `.venv`、安装开发依赖、复制 `.env.example` 为 `.env`
（若不存在）并执行 `alembic upgrade head`。

如果系统的 `python3` 不是 3.11+，显式指定：

```bash
make bootstrap PYTHON=python3.12
```

`make demo` 会迁移数据库、写入 `p001`、启动 API、打印 `/health` 与 `/papers`，
然后自动停止服务。修改端口：

```bash
DEMO_PORT=8010 make demo
```

### 手动启动 API

```bash
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
curl http://127.0.0.1:8000/health
curl "http://127.0.0.1:8000/papers?limit=10"
```

首次启动前需要：

```bash
.venv/bin/alembic upgrade head
.venv/bin/papersteward demo seed
```

## 配置

配置使用 `PAPERSTEWARD_` 前缀，环境变量优先于 `.env`。完整模板见
[`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PAPERSTEWARD_APP_ENV` | `dev` | `dev` / `test` / `prod` |
| `PAPERSTEWARD_DATA_DIR` | `./var` | 数据目录（数据库、导出等） |
| `PAPERSTEWARD_DATABASE_URL` | 空 | 留空使用 `<DATA_DIR>/papersteward.db` |
| `PAPERSTEWARD_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `PAPERSTEWARD_LOG_FORMAT` | `json` | `json` / `console` |
| `PAPERSTEWARD_API_HOST` | `127.0.0.1` | 默认只绑定本机 |
| `PAPERSTEWARD_API_PORT` | `8000` | 1–65535 |
| `PAPERSTEWARD_TIMEZONE` | `Asia/Shanghai` | IANA 时区名 |

`.env` 已被 `.gitignore` 忽略，**不要提交真实密钥**。

## CLI

```bash
papersteward --version
papersteward doctor
papersteward demo seed

papersteward profile validate tests/fixtures/profiles/basic.md
papersteward taxonomy validate tests/fixtures/taxonomies/basic.md
papersteward doc diff profile tests/fixtures/profiles/basic.md tests/fixtures/profiles/v2.md

papersteward profile import tests/fixtures/profiles/basic.md
papersteward profile confirm 1.0 --id demo-profile --at 2026-10-21T09:00:00Z
papersteward profile archive 1.0 --id demo-profile
papersteward doc diff profile 1.0 1.1 --id demo-profile

papersteward config validate tests/fixtures/configs/scoring.yaml
papersteward config import tests/fixtures/configs/scoring.yaml --at 2026-10-21T09:00:00Z
papersteward schema show classification
papersteward schema validate classification /tmp/classification.json

papersteward llm ping --fake
papersteward llm ping --fake --json
```

- `doctor` 全部通过退出 0，否则退出 1；只诊断，不自动迁移；
- `demo seed` 幂等；数据库未迁移时提示先执行 `alembic upgrade head`；
- `profile/taxonomy validate` 只解析校验，不写库；`import` 写入 draft；
  `confirm/archive` 经状态机并写事件；
- `doc diff` 支持两个文件，或 `--id` + 两个数据库版本；`--json` 输出机器可读结果；
- `config validate` 不写库；`config import` 追加版本并写 `config.updated` 事件；
- `schema show/validate` 输出或校验分类/摘要/评分/事件 JSON schema；
- `llm ping --fake` 使用确定性 FakeLLM；P1 不发起真实网络/LLM 调用；
- P1 contract freeze 覆盖枚举/表/迁移/schema/prompt/CLI；端到端测试覆盖
  profile/taxonomy/config + FakeLLM 分类/摘要落库；
- 安装后可直接使用 `papersteward`；在仓库内也可用 `.venv/bin/papersteward`。

## API

### `GET /health`

```json
{
  "status": "ok",
  "version": "0.1.0.dev0",
  "environment": "dev",
  "database": "ok"
}
```

数据库不可用时返回 503，`status="degraded"`、`database="unavailable"`。

### `GET /papers`

Query：`limit`（默认 50，范围 1–200）、`offset`（默认 0）。
`created_at` 是 P0 兼容别名，映射正式模型中的 `first_seen_at`。

```json
{
  "items": [
    {
      "id": "p001",
      "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
      "status": "discovered",
      "created_at": "2026-10-21T08:00:00Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

错误响应统一为：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数校验失败",
    "details": [],
    "request_id": "req-123"
  }
}
```

## 数据库与迁移

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
.venv/bin/alembic downgrade base
make migrate-check
```

- 迁移文件位于 `migrations/versions/`；
- 应用启动**不会**自动建表或迁移；
- 迁移 `0002` 已将 P0 的 `papers` / `runs` 替换为正式 `paper` / `run` 及 12 张领域表；
  升级会迁移 P0 的最小字段，降级只恢复空的 stub 表（正式数据不保留）；
- 测试使用临时 SQLite，不触碰本地 `var/papersteward.db`。

## Docker

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/papers
docker compose down
```

- 容器启动时先执行 `alembic upgrade head`；
- 镜像构建时会为非 root 运行用户补齐源码/迁移的读权限，避免宿主严格 umask
  导致 Alembic `PermissionError`；修改 Dockerfile 后请用 `docker compose up --build`
  重建；
- 数据保存在 `papersteward_data` 命名卷；
- 端口只绑定 `127.0.0.1`；
- 删除数据卷：`docker compose down -v`。

## 质量门

| 命令 | 内容 |
|------|------|
| `make bootstrap` | 创建 venv、安装依赖、准备 `.env`、迁移 |
| `make check` | ruff format/check + mypy + 非 e2e 测试 |
| `make test` | 单元/契约/集成测试（不含 e2e） |
| `make test-integration` | 仅集成测试 |
| `make migrate-check` | 临时库 upgrade/downgrade/upgrade |
| `make e2e` | 启动真实 Uvicorn 的 walking skeleton 测试 |
| `make demo` | 迁移、seed、启动 API、验证 health/papers、停止 |
| `make secret-scan` | 常见密钥模式扫描 |
| `make docker-build` / `docker-up` / `docker-down` | Compose 操作 |

CI 在 Python 3.11/3.12 上执行 `make check`、`make migrate-check`、`make e2e`、
`make secret-scan`，并有 Docker Compose smoke job。

## 目录结构

```text
src/papersteward/
  api/            # FastAPI app、路由、依赖、错误、请求 ID
  cli/            # papersteward 命令、doctor、demo seed
  config.py       # Settings（PAPERSTEWARD_*）
  errors.py       # 统一错误模型
  observability/  # 结构化日志
  storage/        # Engine/Session、*Row ORM、mappers、仓储、迁移状态
  core/           # 领域实体/枚举/状态机/不变量（P1-S1）
  documents/      # profile/taxonomy 解析、diff、确认/归档服务（P1-S3）
  scoring/        # scoring.yaml 校验、配置版本持久化（P1-S4）
  schemas/        # 分类/摘要/评分/事件 JSON schema（P1-S4）
  llm/            # provider 协议、FakeLLM、prompt、网关、成本（P1-S5）
  sources/ pipeline/ delivery/ jobs/  # P1+ 占位
migrations/       # Alembic 迁移
tests/            # unit / contract / integration / e2e
docs/             # 设计与阶段文档
```

## 当前范围与限制

- 单用户、本地优先；无鉴权、无 CORS、无 Postgres；
- P1-S5/S6 完成 LLM 网关、FakeLLM 与阶段验收；真实 provider、真实文献源与 P2 onboarding 未开始；
- 不采集真实文献、不调用真实 LLM、不生成摘要/评分/日报；FakeLLM 仅用于契约/演示；
- API 只有只读 `/papers`，无详情/创建接口；`status` 使用正式枚举（如 `discovered`）；
- `doctor` 只检查，不修复；`demo seed` 只支持一个演示文献；
- Docker 健康检查使用 Python `urllib`，不依赖镜像内 curl；
- 生产部署、备份/恢复、成本治理在 P8。

## 文档

- [大阶段开发计划](docs/PaperSteward_大阶段开发计划.md)
- [系统设计方案](docs/PaperSteward_文献自动挖掘推送与优先级排序系统设计方案.md)
- [P0 阶段记录与交接](docs/phases/P0-baseline.md)
- [P1 阶段记录与交接](docs/phases/P1-domain-documents-llm.md)
- [ADR 0001：YAML front matter 解析](docs/adr/0001-yaml-front-matter.md)

## 安全

- `.env` 不入库；`make secret-scan` 扫描常见密钥模式；
- 默认只绑定 `127.0.0.1`；不要直接暴露到公网；
- 日志脱敏 token/secret/password/api_key 等字段；
- 错误响应不回显异常堆栈或用户输入值。
