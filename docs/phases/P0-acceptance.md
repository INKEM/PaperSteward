# P0 独立验收报告

> **验收对象**：P0 工程基线（P0-S1–P0-S5）
> **验收 commit**：`7dbc5d7`（`docs(p0-s5): record fresh clone verification`）
> **验收日期**：2026-10-21
> **验收方式**：不修改实现代码；独立运行阶段门、手工契约检查、全新 clone 验收、静态审查
> **结论**：**本地可执行验收通过；Docker 手动检查通过；F1 已修复；CI 运行证据待补**——可进入 P1，但 `p0-done` tag 仍待 CI 运行记录。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| 代码质量门（ruff/mypy/pytest） | **通过**：48 文件格式正确，30 源文件 mypy 无错误，83 passed / 1 deselected |
| 集成测试 | **通过**：44 passed |
| 迁移 up/down/up | **通过**：`0001` 可升降级；`alembic check` 无差异 |
| 真实 Uvicorn e2e | **通过**：1 passed |
| `make demo` | **通过**：health ok，papers 返回 p001 |
| 密钥扫描 | **通过**；注入假密钥可被检出 |
| 全新 clone（Python 3.12） | **通过**：bootstrap/check/integration/migrate/e2e/demo/secret 全绿 |
| 全新 clone（默认 Python 3.14） | **通过**：bootstrap + check |
| API/CLI/日志/配置手工契约检查 | **通过**（22/22 API 检查 + CLI/迁移/日志检查） |
| F1：500 响应 `X-Request-ID` | **已修复**：错误响应自身回写响应头；合同/集成回归测试 |
| Docker Compose 运行时 | **通过（手动）**：用户 2026-10-21 确认 Compose 构建/启动、迁移、`/health`、`/papers` 通过 |
| GitHub Actions | **无运行证据**：截至验收时 Actions API 返回 0 次 workflow run |
| `p0-done` tag | **未创建**（待 CI 运行证据） |

> **2026-10-21 复核更新**：用户确认 Docker 手动检查通过；F1 已修复并加入回归测试。
> 上表测试计数是验收 commit `7dbc5d7` 时的值；修复后的计数与证据见 §9。

**判定**：P0 的代码、文档、本地可执行验收均达到交接标准；但 P0 计划中的 Docker 容器验收仍未关闭，因此**不能宣称 P0 完全通过，也不应打 `p0-done`**。P1 可以并行启动，但 P0 应保持“本地通过、Docker 待验”的状态。

---

## 2. 阶段门证据

在验收 commit 上执行：

```text
make check               → 83 passed, 1 deselected；ruff/mypy 全通过
make test-integration    → 44 passed
make migrate-check       → upgrade/downgrade/upgrade OK
make e2e                 → 1 passed
make demo                → health=ok；papers 返回 p001；API 自动停止
make secret-scan         → OK
pytest -q                → 84 passed
coverage                 → 总计 94%
```

补充证据：

- `alembic upgrade head` 后表为 `alembic_version/papers/runs`；`papers` 列为
  `id/title/status/created_at`，`runs` 列为 `id/kind/status/started_at/finished_at/error`；
- `alembic downgrade base` 后仅保留 `alembic_version`；再次 upgrade 成功；
- `alembic check` 输出 `No new upgrade operations detected.`；
- `make demo DEMO_PORT=8017` 使用默认数据库成功；重复执行幂等（p001 已存在）；
- 全量测试仅有一个上游 `StarletteDeprecationWarning`（`httpx` → `httpx2`），不影响 P0 行为。

---

## 3. 全新 clone 验收

### 3.1 Python 3.12（README 推荐路径）

在独立临时 clone 中执行 `make bootstrap PYTHON=python3.12`，随后：

```text
make check               → 83 passed, 1 deselected
make test-integration    → 44 passed
make migrate-check       → upgrade/downgrade/upgrade OK
make e2e                 → 1 passed
make secret-scan         → OK
make demo DEMO_PORT=8018 → health ok；papers p001；API 停止
git status --short       → 空（工作区干净）
```

### 3.2 默认 `python3`（本机 Python 3.14.7）

在同一仓库的另一个全新 clone 中直接执行 `make bootstrap`：

```text
Python 3.14.7；bootstrap 成功（含 alembic upgrade head）
make check → 83 passed, 1 deselected；ruff/mypy 全通过
```

说明 README 的“Python 3.11+ / 默认 python3”路径在本机可用。

---

## 4. 手工契约检查

### 4.1 API（22/22 通过）

独立脚本覆盖：

- `/health` 200，版本/environment/database 字段正确；
- `X-Request-ID` 合法透传、非法值替换为 32 位 hex；
- `GET /papers` 空库信封 `items/total/limit/offset`；
- 分页顺序 `created_at ASC, id ASC`，`limit/offset` 正确；
- `created_at` 在 API 边界为 UTC `Z`；
- `limit=0` → 422 `validation_error`，details 含 `query.limit`，不回显输入；
- 未知路由 404、`POST /papers` 405 均为统一错误信封；
- 未处理异常 500 只返回通用文案，不泄露堆栈/异常文本；
- 空数据库 `/papers` 返回 500 错误信封，且**不自动建表**；
- 数据库不可用时 `/health` 返回 503 `degraded/unavailable`；
- OpenAPI 暴露 `/health`、`/papers`。

真实 Uvicorn + curl 复核了 `/health`、`/papers`、404、422、405 与 `X-Request-ID`。

### 4.2 CLI

```text
papersteward --version          → 0.1.0.dev0，exit 0
doctor（已迁移库）              → current=0001 head=0001，exit 0
doctor（未迁移库）              → 提示 alembic upgrade head，exit 1
demo seed（迁移前）             → exit 1；未创建 papers/runs 表
demo seed（迁移后，两次）       → 第一次 inserted，第二次 already exists；总数 1
非法 PAPERSTEWARD_API_PORT      → exit 1，错误信息含变量名
无子命令                        → exit 2
```

### 4.3 迁移与日志

- 临时库 `upgrade → downgrade → upgrade` 通过；列名与迁移文件一致；
- 日志 JSON 字段、`request_id` 上下文、嵌套敏感字段脱敏（`api_key/token/password/SecretStr`）均通过独立检查；
- 配置环境变量优先于 `.env`；非法时区/端口错误信息包含 `PAPERSTEWARD_*` 变量名。

### 4.4 密钥扫描

- 基线 `make secret-scan` 通过；
- 在临时 clone 注入 `sk-...` 假密钥后，`make secret-scan` 退出非 0 并打印命中文件，说明扫描不是空转。

---

## 5. 发现项

| ID | 严重度 | 发现 | 证据 | 建议 |
|----|--------|------|------|------|
| F1 | **P2 / 已修复** | 未处理异常 500 响应缺少 `X-Request-ID` 响应头；错误体中的 `request_id` 正确 | 未迁移库请求 `GET /papers`，带 `X-Request-ID: req-fail-500`；修复前响应体含 `request_id=req-fail-500`、响应头无 header | 已修复：`_response()` 统一回写 `X-Request-ID`；新增合同断言与真实未迁移库 500 回归测试 |
| F2 | **已关闭** | Docker Compose 健康检查运行时证据 | 用户 2026-10-21 手动执行 Compose 构建/启动，确认容器迁移、`/health`、`/papers` 通过 | 关闭；CI Docker smoke job 保留为持续回归 |
| F3 | 信息 | GitHub Actions 尚无运行记录 | [Actions 页面](https://github.com/INKEM/PaperSteward/actions) 与 `api.github.com/repos/INKEM/PaperSteward/actions/runs` 均无运行记录（`total_count: 0`） | 推送/触发 CI，保留 `quality` 与 `docker` job 证据；在 tag 前附上 run 链接 |
| F4 | 信息 | 本地 `main` 比 `origin/main` 多 2 个提交；无 tag | `git log --decorate`；`git tag` 为空 | 发布前确认提交已推送；不要仅凭本地状态打 tag |
| F5 | 信息 | `make secret-scan` 是轻量正则扫描，且排除 `docs`/`*.example` | Makefile 规则；注入测试可检出常见 key | 保持为提交前最低门槛；P8 再加专业扫描器 |
| F6 | 接受 | P0-S1/S2/S5 文件数超过“≤8 个文件”指引 | P0-baseline 已说明为一次性脚手架/工程收尾 | 不视为 P0 缺陷；P1 起恢复小步提交 |

> F1 已修复：500 响应现在同时包含 `X-Request-ID` 头与错误体 `request_id`；合同测试
> 和集成回归测试已加入，详见 §9。F2 已由用户 Docker 手动验收关闭；剩余仅为 CI
> 运行证据。

---

## 6. 范围与契约复核

- P0 只包含配置、日志、错误契约、SQLite/Alembic、`/health`、`/papers`、CLI、Makefile、Docker/CI 模板；
- `papers` / `runs` 是 stub 表，未提前引入 P1 的标签、桶、评分、事件日志等业务字段；
- 未引入真实文献源、真实 LLM、摘要/分类/评分/日报/UI；
- 应用启动不自动建表/迁移；测试使用临时 SQLite；
- `.env` 未被 git 跟踪；`.gitignore` 覆盖数据与缓存；
- README/AGENTS/CHANGELOG/P0 handoff 与实际命令基本一致；
- P0 handoff 明确标注 Docker 未验证、tag 待定，没有虚假声称容器验收通过。

---

## 7. 最终判定与建议

**判定：通过（本地 + Docker 手动验收；CI 证据待补）**。

- **通过部分**：P0 的代码、测试、迁移、API/CLI 契约、文档与本地运行链路均独立复验
  通过；全新 clone 在 Python 3.12 与默认 Python 3.14 下均可 bootstrap/check。
- **Docker**：用户 2026-10-21 手动执行 Compose 构建/启动并确认迁移、`/health`、
  `/papers` 通过，F2 关闭。
- **F1**：500 响应已同时返回错误体 `request_id` 与 `X-Request-ID` 响应头，回归测试
  通过。
- **未关闭部分**：GitHub Actions 尚无运行记录；因此仍不自动打 `p0-done`。

**建议下一步**：

1. 推送本次修复提交并触发 CI，保留 `quality` 与 `docker` job 的成功证据；
2. CI 通过后打 `p0-done` tag，并将 run 链接补到本报告；
3. P1 可以启动，但应先冻结 P1 schema/接口，且不要把 P0 stub 表当作正式领域模型。

---

## 8. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`pytest -q`
- 额外检查：全新 clone（Python 3.12 / 3.14）、API 22 项手工契约、CLI 契约、迁移 schema、日志脱敏、配置优先级、密钥注入、Docker 静态解析
- 验收产物：本报告

---

## 9. F1 修复与复核记录

**日期**：2026-10-21（P0-S5 补丁）

**根因**：未处理异常由最外层 `ServerErrorMiddleware` 处理，此时请求中间件已经退出；
`RequestIdMiddleware` 没有机会给 500 响应写 header。错误体中的 `request_id` 来自
`request.state`，所以体正确、头缺失。

**修复**：

- 新增 `api/constants.py`，集中定义 `REQUEST_ID_HEADER`；
- `_response()` 在生成错误响应时统一写入 `X-Request-ID`，覆盖 500 与普通错误；
- `RequestIdMiddleware` 继续负责成功响应与普通错误响应的 header；
- 回归测试：
  - `tests/contract/test_error_responses.py` 的 500 断言响应头；
  - `tests/integration/test_api.py` 用未迁移数据库触发真实 500，断言响应头与错误体
    的 `request_id` 一致。

**复核结果**：

```text
make check              84 passed, 1 deselected
make test-integration   45 passed
pytest -q               85 passed
make e2e                1 passed
ruff format --check .   49 个 Python 文件
mypy (strict)           31 个源文件无错误
```

**Docker**：用户手动验收通过（Compose 构建/启动、容器迁移、`/health`、`/papers`）。

**剩余项**：CI `quality` / `docker` job 运行证据；取得后打 `p0-done` tag。
