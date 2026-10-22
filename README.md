# PaperSteward

本地优先的文献自动挖掘、优先级排序与每日推送系统。

当前处于 **P7 特殊桶、自定义排序与权重治理（S1 提名审核、S2 档位/配额/轮换完成；S3–S6 待开始）**。
P0–P6已有初始化G1–G4、采集/去重/过滤、摘要/分类/静态与动态评分、审核和解释队列、
日报/投递/阅读反馈/归档。P7新增特殊桶提名持久化/人工决策、五维档位 rubric 与人工覆盖、
周配额与每日两槽轮换计划，迁移head为0012，无新增Python依赖；特殊桶视图和权重治理尚未实现。
P6 已关闭，P6 tag 与用户首份真实日报审阅仍待发布/验收流程，不宣称 Alpha/Beta 已发布。

```bash
.venv/bin/papersteward shelf review
.venv/bin/papersteward shelf accept p001 --by local-user --note "确认值得读"
.venv/bin/papersteward shelf assess p001 --json
.venv/bin/papersteward shelf tier p001 --tier high --by local-user --note "人工确认"
.venv/bin/papersteward shelf plan --date 2026-10-22 --json
.venv/bin/papersteward digest --date 2026-10-22 --out out/digest.md --json
.venv/bin/papersteward done p001 --mode deep --score 4 --note "阅读反馈" --request-id read-p001
.venv/bin/papersteward archive export --format bibtex --out out/archive.bib
.venv/bin/papersteward jobs preference-weekly --json
```

先完成初始化、process与审核。特殊归属桶排除P/rank，待审提名同样不会进入P快照；
主队列只包含合格主题候选。页面从 `/ui/today` 开始，提供今日/队列/详情/归档。
SMTP必须G4明确选择且CLI显式发送；阅读备注默认仅本地保存。
操作见[日报与归档指南](docs/delivery-cli.md)、[P7阶段卡片](docs/phases/P7-shelves.md)、
[P6阶段交接](docs/phases/P6-delivery.md)、[P6本地验证记录](docs/phases/P6-acceptance.md)、
[P6独立验收报告](docs/phases/P6-acceptance-independent.md)和
[P6返工验证记录](docs/phases/P6-acceptance-rework.md)。
公式边界见[优先级队列指南](docs/priority-queue.md)；上阶段独立验收证据见
[P5独立验收](docs/phases/P5-acceptance.md)。P4复验记录仍见
[P4返工复验](docs/phases/P4-acceptance-rework.md)。

## 当前能力

- `papersteward shelf review|accept|reject`：列出待审特殊桶提名，按人工决策归入
  跨层综述/远邻领域/经典奠基或保留主题归属；待审提名不进入P快照；
- `papersteward shelf assess|tier|plan`：五维 rubric 生成 high/medium/low 档位、
  人工覆盖并保留历史；按 review 3 / distant 2 / classic 1 的周上限和每日两槽轮换
  生成计划，低档/空桶/配额用尽不凑数；
- `GET/POST /digests`、阅读操作 `/papers/{id}/actions`、`GET /archive`及导出；
- `/ui/today|queue|papers/{id}|archive`：本地HTMX阅读闭环，`POST /jobs/preference/weekly`：显式周任务；
- `GET /queue`、`GET /scores/{id}/explain`、`POST /scores/snapshot`：日队列、分项解释与快照；
- `GET /health`：返回版本、运行环境与数据库连通性；
- `GET /papers`：从正式 `paper` 表返回文献列表（只读）；
- `GET /review`、`POST /review/{id}/approve|relabel`：列出并处理低置信度/边界审核项；
- `GET /onboarding`、`POST /onboarding/start`：查看/开始初始化（幂等）；
- `POST /onboarding/profile/drafts`：接收 Agent/用户 Markdown 草案；编辑时指定
  base_version 并提升版本，返回全文、diff、内容哈希、G1 缺项与协商问题；
- `GET /onboarding/profile/{id}/{version}`：审阅档案，可用 base_version 查询参数比较版本；
- `POST /onboarding/profile/{id}/{version}/confirm`：携带 confirmed=true、确认者、
  变更摘要、审阅返回的 content_hash 与 expected_revision，原子确认档案并通过 G1；
- `POST /onboarding/taxonomy/drafts`：提交分类法与完整种子标注，保存版本化审阅证据；
- `GET /onboarding/taxonomy/{id}/{version}`：全文/diff、覆盖率/均衡/边界报告和影响预览；
- `POST /onboarding/taxonomy/{id}/{version}/confirm`：显式确认文档及审阅哈希，原子通过 G2；
- `POST /onboarding/seeds/import`：导入已确认 G2 清单，只补充 discovered 元数据；
- `POST /onboarding/calibration/rounds`：建立与种子分离的 15–20 篇校准集，最多两轮；
- `POST /onboarding/calibration/{round}/agent-labels`、`/user-labels`：分别完整提交，
  不可覆盖；双方完成前隐藏参考答案与另一方标注；
- `GET /onboarding/calibration/{round}`：完整后返回 Spearman、Top-5、主标签/
  阅读方式一致率、争议及档案/rubric 建议；合成数据标记“仅流程验证”；
- `POST /onboarding/calibration/{round}/confirm`：confirmed=true、确认者、摘要、
  expected_revision=3、report_hash；低指标需 accept_deviations=true 和 known_biases；
- `POST /onboarding/dry-runs`：提交运行参数、完整 scoring 配置与预选合成条目，
  生成只展示的版本化日报（version 从 1 依次追加）；
- `GET /onboarding/dry-runs/{version}`：查看 Markdown、参数、报告 hash 与反馈历史；
- `POST /onboarding/dry-runs/{version}/feedback`：以 report_hash 绑定反馈，支持
  too_many/too_few/inaccurate/wrong_category；accepted=true 须无未解决问题；
- `POST /onboarding/dry-runs/{version}/confirm`：显式确认最新已认可日报，携带
  confirmed=true、确认者、摘要、expected_revision=4、report_hash 和 feedback_hash；
  原子保存选定配置/审计并通过 G4，预览和负面反馈不会激活流程；
- `papersteward onboarding`：start/status、Markdown/JSON 提交、全文/版本差异审阅、
  分角色标注、日报反馈与显式确认；`--out` 保存私有审阅快照，确认不会刷新依据；
- `papersteward ingest`：在 ACTIVE/G4 后从 arXiv/OpenAlex/Semantic Scholar fixture
  或显式 `--live` 采集，去重、硬过滤并幂等写入 screened/rejected 文献与来源版本；
  `papersteward runs show` 查看可审计运行报告（partial/失败/过滤原因）；
- `papersteward process`：在 G4 后离线处理 screened 文献（enrichment/R/摘要/分类/特殊桶/
  静态分），低置信度进入 review；`--live` 才允许真实 LLM；
- `papersteward review list|approve|relabel`：审核队列的列出、批准与人工改标签；
- `papersteward doctor`：检查配置、数据库连接与 Alembic current/head；
- `papersteward demo seed`：幂等写入演示文献 `p001`；
- SQLite + SQLAlchemy 2.x + Alembic；
- 结构化日志、`X-Request-ID`、统一错误响应；
- 纯领域层 `core/`：稳定枚举、不可变实体、文献/文档/运行状态机、分类/桶/评分/
  追加写等不变量；
- 正式领域表与仓储：P1 的 14 张表 + onboarding/calibration/paper_source/enrichment 表
  （head `0012`）；部分唯一索引/CHECK/FK、事件/评分快照/归档追加写；G4 前拒绝入队/推送/归档；
- profile/taxonomy：Markdown + YAML front matter 校验、版本确认/归档、实体 diff；
- scoring.yaml：权重和、策略下限/上限、gates 与 stability 校验；配置版本追加写；
- Agent 输出 JSON schema：分类、相关度、摘要、特殊桶提名、评分快照、事件（正反例校验）；
- LLM：provider 协议、FakeLLM、版本化 prompt、schema 修复重试、usage/成本；
  P4-S1 新增 OpenAI-compatible 真实 provider（显式 `--live`、HTTPS/超时/脱敏）；
- P3：确定性查询生成、三来源离线适配器、带预算/限流/大小上限的 HTTP 边界、
  canonicalization/五类保守去重、硬过滤、来源版本与 Run 报告；
- P4-S1–S4：中性富化信号、enrichment 持久化、相关度 R/rubric 证据；
- 摘要：summary/v2 prompt、四槽/逐字证据/数字校验、needs_review 与 summary 表持久化；
- 分类：classification/v2 主/副标签、runner-up/置信度、边界/证据校验与 paper_tag/paper_bucket 写入；
- 特殊桶：special_nomination 三类提名、review 层级/classic 依据/distant bridge memo 校验；
- 静态分：A/E/S 确定性公式、置信度/中性值、版本冻结与 `static_score_snapshot` 快照；
- 审核：`process` 编排、`review_item` 队列、批准/改标签 API/CLI，真实 Uvicorn e2e；
- Makefile、Docker Compose、GitHub Actions CI。

## 初始化链路演示

```bash
.venv/bin/pytest tests/e2e/test_onboarding_gates.py -m e2e -q
```

该命令在临时 SQLite 和真实 localhost Uvicorn 上跑完 G1–G4、负面反馈与参数新版，
验证 G4 前真实仓储入队/推送/归档均拒绝，确认后允许。没有正式发送动作。
完整 CLI 演示（真实 CLI 子进程、独立临时库）：

```bash
.venv/bin/pytest tests/e2e/test_onboarding_cli_e2e.py -m e2e -q
.venv/bin/papersteward onboarding --help
.venv/bin/papersteward onboarding status
```

CLI 需要先启动 API；操作步骤、确认语义和恢复方法见
[初始化 CLI 指南](docs/onboarding-cli.md)、
[process/review 指南](docs/process-review-cli.md)。本轮选择 CLI，浏览器可使用 API 的 `/docs`。

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
| `PAPERSTEWARD_LLM_PROVIDER` | `fake` | `fake` / `openai_compatible` |
| `PAPERSTEWARD_LLM_BASE_URL` | `https://api.openai.com/v1` | API 根地址；非本机必须 HTTPS |
| `PAPERSTEWARD_LLM_API_KEY` | 空 | 只从环境/.env 读取，不打印 |
| `PAPERSTEWARD_LLM_MODEL` | 空 | `openai_compatible` 时必填 |
| `PAPERSTEWARD_LLM_TIMEOUT_S` | `30` | 单请求硬超时（1–600 秒） |
| `PAPERSTEWARD_LLM_MAX_ATTEMPTS` | `3` | provider 重试上限（1–5） |
| `PAPERSTEWARD_LLM_PROMPT_COST_PER_1K_USD` | `0` | 成本估算单价 |
| `PAPERSTEWARD_LLM_COMPLETION_COST_PER_1K_USD` | `0` | 成本估算单价 |

`.env` 已被 `.gitignore` 忽略，**不要提交真实密钥**。真实 LLM 配置、端点示例与
故障排查见 [LLM provider 配置指南](docs/llm-provider.md)。

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
papersteward schema show relevance
papersteward schema show special_nomination
papersteward schema validate classification /tmp/classification.json

papersteward llm ping --fake
papersteward llm ping --fake --json
# 配置真实 provider 后，显式允许一次网络调用（可能产生少量费用）
papersteward llm ping --live
```

- `doctor` 全部通过退出 0，否则退出 1；只诊断，不自动迁移；
- `demo seed` 幂等；数据库未迁移时提示先执行 `alembic upgrade head`；
- `profile/taxonomy validate` 只解析校验，不写库；`import` 写入 draft；
  `confirm/archive` 经状态机并写事件；
- `doc diff` 支持两个文件，或 `--id` + 两个数据库版本；`--json` 输出机器可读结果；
- `config validate` 不写库；`config import` 追加版本并写 `config.updated` 事件；
- `schema show/validate` 输出或校验分类/相关度/摘要/评分/事件 JSON schema；
- `llm ping --fake` 使用确定性 FakeLLM，离线；`--live` 必须显式指定并读取
  `PAPERSTEWARD_LLM_*`，不会隐式联网；配置见 [LLM provider 指南](docs/llm-provider.md)；
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
- 迁移 `0004` 保存两轮独立校准；`alembic downgrade 0003` 删除校准表，但保留已导入
  文献、审计事件与门禁状态。完整回滚应停应用并恢复 S4 前备份，不能据降级假定 G3 撤回；
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
- P1 完成领域/文档/LLM 网关；P2-S1–S6 完成初始化 API/CLI 至 G4，并通过本地阶段门；
- P1/P2/P3 Docker 与 CI 均已验证；P2/P3 阶段 tag 仍待发布流程；
- 不采集真实文献、不调用真实 LLM、不执行真实摘要/评分或正式日报发送；试运行展示固定 fixture；
- `/papers` 只读；导入通过 onboarding API；`status` 使用正式枚举（如 `discovered`）；
- `doctor` 只检查，不修复；`demo seed` 只支持一个演示文献；
- Docker 健康检查使用 Python `urllib`，不依赖镜像内 curl；
- 校准使用人工输入或固定 fixture/cassette，不自动生成“用户答案”，不调用真实模型；
  角色分开提交用于避免界面泄题，当前无身份鉴权；rubric_version 是输入版本标识；
- 合成校准示例为 `tests/fixtures/calibration/basic.json`：包含 cohort、agent_batch、
  user_batch，分别提交；样本元数据由策展者提供，服务校验结构，不判断论文真实相关性；
- 试运行示例：`tests/fixtures/dry_run/basic.json`；可读日报：
  [digest.md](tests/fixtures/dry_run/digest.md)。日报只校验输入配额/标签/相关度，不运行排序；
- 运行参数确认时间/时区、安静时段、暂停、主标签目标占比、渠道/隐私；配额/权重复用
  scoring 配置。渠道限本地 Markdown，备注不外发；ACTIVE 不会自动发起外部调用；
- 生产部署、备份/恢复、成本治理在 P8。

## 文档

- [大阶段开发计划](docs/PaperSteward_大阶段开发计划.md)
- [系统设计方案](docs/PaperSteward_文献自动挖掘推送与优先级排序系统设计方案.md)
- [P0 阶段记录与交接](docs/phases/P0-baseline.md)
- [P1 阶段记录与交接](docs/phases/P1-domain-documents-llm.md)
- [P2 阶段记录与 P3 交接](docs/phases/P2-onboarding.md)
- [P2 本地验收报告](docs/phases/P2-acceptance.md)
- [P2 独立验收报告](docs/phases/P2-independent-acceptance.md)
- [P3 阶段记录与 P4 交接](docs/phases/P3-ingestion.md)
- [P3 独立验收报告](docs/phases/P3-acceptance.md)
- [P4 阶段记录与 P5 交接](docs/phases/P4-enrichment.md)、
[P4 验收](docs/phases/P4-acceptance.md)、[返工记录](docs/phases/P4-acceptance-rework.md)
- [P4 独立验收报告](docs/phases/P4-acceptance.md)
- [LLM provider 配置指南](docs/llm-provider.md)
- [初始化 CLI 操作指南](docs/onboarding-cli.md)
- [采集 CLI 操作指南](docs/ingestion-cli.md)
- [处理/审核 CLI 操作指南](docs/process-review-cli.md)
- [ADR 0001：YAML front matter 解析](docs/adr/0001-yaml-front-matter.md)
- [ADR 0013：真实 LLM provider 与显式授权](docs/adr/0013-llm-provider.md)
- [ADR 0014：静态分独立快照](docs/adr/0014-static-score-snapshot.md)

## 安全

- `.env` 不入库；`make secret-scan` 扫描常见密钥模式；
- 默认只绑定 `127.0.0.1`；不要直接暴露到公网；
- LLM key 只通过 `PAPERSTEWARD_LLM_API_KEY`/环境变量；真实调用需显式 `--live`；
- 日志脱敏 token/secret/password/api_key 等字段；
- 错误响应不回显异常堆栈或用户输入值。
