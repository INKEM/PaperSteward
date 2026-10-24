# AGENTS.md — 代理开发须知

给在本仓库工作的编码代理/协作者的高信号要点汇总。产品语义详见 `docs/queue-first-plan.md`，用户文档在 `docs/`，本文只收录散落各处的开发约定与本机环境坑。

## 项目一句话

PaperSteward：本地优先的单用户论文阅读工作区。浏览器 UI（FastAPI + htmx）为主入口；手动挖掘（采集→去重→初筛→深处理→评分入队）→ 长期阅读队列 → 阅读反馈。**没有**每日定时任务、日报、推送调度；`docs/` 里的日报/投递 CLI 是遗留高级功能。

## ⚠️ 本机坑：测试必须用内存盘临时目录

本机磁盘同步慢，pytest 默认把临时 SQLite 库落在磁盘上会导致集成测试像挂起一样（实际是 fsync 等待）。**跑测试一律加 `--basetemp=<内存临时目录>`**（同时接管 `tmp_path` fixture 的位置，迁移和事务校验保持完整）：

```bash
# 直接跑（推荐）
.venv/bin/pytest --basetemp=/dev/shm/ps-basetemp -m "not e2e and not live" -q

# 经 make（覆盖 PYTEST 变量）
make test PYTEST=".venv/bin/pytest --basetemp=/dev/shm/ps-basetemp"
```

- 全量非 e2e 约 55–65s；单元测试单独跑约 1–2s（`tests/unit` 不落库，不加 basetemp 也快）。
- `/dev/shm` 重启即清空，无所谓；pytest 启动时会清空 basetemp，重复用同一目录没问题。
- 判据：如果测试"卡住"先怀疑磁盘同步，不要怀疑死循环。

## 常用命令

```bash
make bootstrap            # .venv + 依赖 + .env + alembic upgrade head
make check                # ruff format --check + ruff check + mypy + 非 e2e 测试（提交前必跑）
make test                 # 仅测试；make e2e 为 3 项真实 Uvicorn 冒烟
make migrate-check        # 临时库 upgrade → downgrade → upgrade
make secret-scan          # 疑似密钥扫描（sk-…/AKIA…/私钥块）
newgrp docker             # 开放docker权限
```

- Python 3.11+，虚拟环境在 `.venv`，一律用 `.venv/bin/…` 的工具，不要用系统 Python。
- `make check` 包含 **mypy**（约 142 个源文件，必须零报错）。
- Docker：当前用户无权访问 `/var/run/docker.sock` 且 `no new privileges` 禁止 sudo，容器验收做不了；涉及 Docker 的改动只能静态验证并如实说明。

## 测试布局与契约冻结（改接口必读）

```
tests/unit/         纯逻辑，~1s 跑完
tests/contract/     冻结契约：P1 状态/表/迁移版本/schema/prompt 注册表、P5 评分快照 golden
tests/integration/  真实 SQLite（conftest 提供 client / migrated_engine / sqlite_engine fixtures）
tests/e2e/          3 项，启动真实 Uvicorn
tests/fixtures/     profiles/ taxonomies/ llm/ process/ configs/
```

改了下面任何东西，都要**显式**同步对应冻结测试（这是设计意图：注册表变更必须过目）：

- 加 prompt（`src/papersteward/llm/prompts.py`）→ `tests/contract/test_p1_freeze.py`（prompt 注册表冻结）、`tests/unit/test_llm_prompts.py`。
- 加 schema（`src/papersteward/schemas/models.py` 的 `SCHEMA_MODELS`）→ `test_p1_freeze.py` + `tests/contract/test_json_schemas.py`。schema 一律 `extra="forbid"`。
- 加迁移（`migrations/versions/00XX_*.py`）→ 版本号断言散布在 `tests/integration/test_migrations.py`、`test_start.py`、`test_workspace_reset.py`、`test_cli.py`（doctor 输出 `current=/head=`）。
- 改 `src/papersteward/resources/starter/scoring.yaml` → `tests/fixtures/configs/scoring.yaml` 必须同步（`test_ranking_config.py::test_legacy_config_json_is_unchanged`）。
- P5 冻结契约：`scoring/contracts.py` 的 `QueueItem`/`ScoreExplanation`/`Exclusion` 不得随意加字段（`test_queue_contracts.py`）；P5 golden 快照在 `test_p5_acceptance.py` / `test_p5_golden.py`。

## 迁移

- Alembic 编号迁移（`migrations/versions/`，当前 head 见最新文件）；**只有新表是零成本的，改旧表列必须写迁移**。测试 fixture 用 `Base.metadata.create_all`，正式库走 alembic——两者都要照顾。
- 涉及 schema 的改动跑 `make migrate-check` 验证可回滚。

## 架构速览（改挖掘/队列前先读）

- 分层：`api/`（路由与页面渲染）· `cli/` · `core/`（实体/枚举/状态机）· `pipeline/`（ingest、screen、dedupe、process、relevance/summarize/classify/special）· `scoring/`（config、queue、static/dynamic/preference/priority）· `sources/`（arxiv/openalex/semantic_scholar 适配器 + queries/planner）· `storage/`（models/repositories/mappers）· `jobs/ui_flow.py`（单 worker 后台任务编排）· `llm/`（gateway/prompts/runtime/cache）· `onboarding/` · `delivery/` · `documents/`。
- 挖掘漏斗（`jobs/ui_flow.py::_run_sync`）：Agent 检索规划（`sources/planner.py`，失败退回 `generate_queries` 档案展开）→ 采集 → 并行初筛（仅相关度）→ 按 R 降序逐波深处理 → 刷新队列 → 不足配额则扩查/降门槛补足（`quota_fill` 标记，下限 `quota_fill_relevance_floor=40`）。
- 入队门槛唯一硬条件：`static.relevance < max(55, gates.min_relevance_for_push)`；保底填充快照带 `quota_fill` reasons 标记，读取侧（`_eligibility`）识别后豁免——改门槛逻辑时两边要一起动。
- LLM：所有调用走 `LLMGateway.complete_json`（版本化 prompt + JSON schema 校验 + 一次 repair + 溯源 + input_hash 缓存键）。缓存表 `llm_cache`（迁移 0019），`llm_cache_enabled` 可关。
- 相关度 R = 100·(0.40·语义 + 0.60·rubric/5)，语义分量目前是**未校准的词面哈希 embedding**（`HashingEmbeddingProvider`）——已知短板，改评分相关逻辑时记住这一点。

## SQLite 并发约定（并行 worker 必守）

- 文件库已启用 WAL（`storage/db.py`）；`session_scope(factory, immediate=True)` 以 BEGIN IMMEDIATE 开写事务。
- 规矩：**worker 任务 = 只读阶段（绝不写库，包括 `ensure_topic_buckets(persist=False)`）→ 事务外模型计算 → BEGIN IMMEDIATE 短写落库**。读→写同事务升级会撞 BUSY_SNAPSHOT 立即失败（busy timeout 不生效），别回退成"一个事务包全程"。
- 内存库（`:memory:`）用 StaticPool 单连接，不支持 WAL，仅限测试。

## 产品不变量（违反即走偏）

- 挖掘完全手动触发；不做 cron/APScheduler/每日任务（`api/jobs.py` 只有周度偏好刷新）。
- 每个工作区同一时刻只允许一个 `ui.*` 任务（409/303 语义见 `workflow_pages.py`；多工作区见 ADR 0020，`app.state.workspaces` 按请求经 cookie 解析）；同日重复挖掘幂等，只追加新论文。
- 队列持久：合格论文进长期队列，不按日期切分、不按容量隐藏；`snooze/skip` 已移除。
- 配置治理（`docs/advanced/config-governance.md`）：治理入口只允许改线性权重字段；`gates`/`stability`/half_life 等非权重字段 400 拒绝。
- 档案激活后改研究方向只能走设置页重新生成或 documents CLI；材料/意见仅 DRAFT 阶段可写。

## 文档与提交约定

- 用户可见变更进 `CHANGELOG.md` 的「未发布」段（中文，一条一个行为变化）；影响操作方式的同步 `docs/quickstart.md`、`docs/faq.md`、`docs/troubleshooting.md`；挖掘/队列设计决策追加到 `docs/queue-first-plan.md` 对应章节（它是设计+验收记录，不重复进 roadmap）。
- 提交信息用 conventional 前缀（`feat:`/`fix:`/`docs:`），正文中文说明动机与要点；分支 `feat/<topic>` / `docs/<topic>`。
- 大改动先在分支上做检查点提交再继续，避免多批工作纠缠在同一工作树。
- 文案面向浏览器用户：错误提示说清"发生了什么 + 下一步做什么"，不泄漏技术后缀、prompt、密钥。
