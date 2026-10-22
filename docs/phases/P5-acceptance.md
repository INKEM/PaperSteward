# P5 独立验收报告

> **验收对象**：P5 优先级排序、动态分与解释（P5-S1–S6）  
> **验收 commit**：`c9854fd`（PR #7 head）；远程 `main` 合并提交 `ed2409b`，与 `c9854fd` 树完全一致  
> **验收日期**：2026-10-22  
> **验收方式**：不修改实现代码；独立运行本地阶段门、全新 clone、Docker 容器、GitHub Actions 查询，并做队列/快照/评分边界独立探针  
> **结论**：**通过（Accepted）**。未发现 P5 新增的阻断性功能或数据缺陷；P5 可以关闭。P5 阶段 tag 尚未创建；远程 `main` 已合并且 CI 已通过，阶段文档中的“未合并/未推送/CI 未验证”表述需要在关闭提交中同步。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：209 文件格式检查；ruff lint 通过；strict mypy **101** 源文件；**876 passed / 4 deselected** |
| `make test-integration` | **通过**：**265 passed** |
| `make e2e` | **通过**：**4 passed**（真实 Uvicorn + CLI，包含完整 P5 链路） |
| `make migrate-check` | **通过**：base → **0008** → base → 0008 |
| `make demo`（临时库） | **通过**：迁移、seed、`/health`、`/papers` 正常 |
| `make secret-scan` / `git diff --check` | **通过** |
| 全新 clone（Python **3.14.7**） | **通过**：bootstrap / check / integration / migrate / e2e / demo / secret，`git status` 为空 |
| Docker build/run | **通过**：镜像构建成功；容器 UID 10001；head 0008；`/health` 200；`doctor`、`/papers` 正常；G4 前 queue 返回 409 |
| CI | **通过**：PR run [39751976792](https://github.com/INKEM/PaperSteward/actions/runs/39751976792)（`c9854fd`）与 main push run [39752309117](https://github.com/INKEM/PaperSteward/actions/runs/39752309117)（`ed2409b`）三个 job 全绿 |
| 远程发布状态 | PR #7 已于 2026-10-22T09:02:15Z 合并；`origin/main = ed2409b`；无 P5 tag |
| P5 阻断缺陷 | **无** |

**总体判定**：P5-S1–S6 的本地实现、独立边界复现、全新 clone、Docker、CI 均已闭合；P5 通过独立验收。

---

## 2. 阶段门证据

### 2.1 当前工作区（`c9854fd`）

```text
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p5-accept-check'
  → 876 passed, 4 deselected；ruff/mypy 通过

make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p5-accept-int'
  → 265 passed

TMPDIR=/dev/shm make migrate-check
  → base → 0008 → base → 0008

make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p5-accept-e2e'
  → 4 passed

PAPERSTEWARD_DATABASE_URL=sqlite+pysqlite:////dev/shm/p5-accept-demo.db \
  make demo DEMO_PORT=8075
  → /health {"status":"ok","database":"ok"}；/papers total=1；API 退出

make secret-scan
  → OK
```

SQLite 临时库均使用 pytest `tmp_path` 或显式 `/dev/shm` 临时文件，没有写入工作区 `var/papersteward.db`。

### 2.2 全新 clone

`git clone --no-hardlinks .` 到临时目录，默认 Python 3.14.7，执行 `make bootstrap` 后：

```text
make check           → 876 passed, 4 deselected
make test-integration→ 265 passed
make migrate-check   → upgrade/downgrade/upgrade OK（0008）
make e2e             → 4 passed
make demo            → /health ok；/papers total=1
make secret-scan     → OK
git status --short   → 空
```

与工作区结果一致；未发现 Python 3.14 或全新安装依赖导致的 P5 回归。

### 2.3 远程状态与 CI

本轮验收开始时，PR #7 已经合并：

```text
origin/main = ed2409b2944e8816ebe94ef6609a860ffd1bc67e
HEAD        = c9854fd27484e28b7ae2d6901741ffc3d15c924e
git diff HEAD origin/main → 空（树一致）
git tag --list / git ls-remote --tags origin → 无 tag
```

| 工作流 | commit | 结果 |
|---|---|---|
| [PR #7 checks](https://github.com/INKEM/PaperSteward/actions/runs/39751976792) | `c9854fd` | `quality (py3.11)`、`quality (py3.12)`、`docker compose smoke` 全部 success |
| [main push](https://github.com/INKEM/PaperSteward/actions/runs/39752309117) | `ed2409b` | 三个 job 全部 success |

---

## 3. Docker 独立复核

使用当前代码重新执行 `docker compose -p p5accept build`（构建成功，镜像层命中缓存），并以独立容器启动：

```text
docker run -d --rm --name p5accept-8077 -p 127.0.0.1:8077:8000 papersteward:dev
GET /health → 200 {"status":"ok","version":"0.1.0.dev0","environment":"dev","database":"ok"}
GET /papers → 200 {"items":[],"total":0,"limit":50,"offset":0}
docker exec p5accept-8077 id → uid=10001(papersteward) gid=10001(papersteward)
docker exec p5accept-8077 alembic current → 0008 (head)
docker exec p5accept-8077 papersteward doctor → 全部 OK
GET /queue?date=2026-10-22（G4 前）→ 409 conflict onboarding.g4
```

容器只使用自身临时文件系统，没有触碰工作区数据库。验收后已停止容器。

---

## 4. 独立契约与边界复现

除现有测试外，使用临时探针（不修改产品代码，探针已删除）复核以下 P5 行为：

### 4.1 算术与 golden

- 设计 §10.10 的组件权重示例复现 **P=75.1**；P4 冻结 `S=78.8` 后分层算术复现 **P=75.2**，`previous_rank=74` 得到 **rank=74.8**。中间舍入差额与 [ADR 0015](../adr/0015-priority-arithmetic.md) 一致，没有为凑 75.1 改动 P4 静态分。
- F 表值（按 180 天半衰期）：age 0/90/180/360/720 → **100 / 72.2 / 52.5 / 28.8 / 10.9**；无出版日 F=50，未来日期 age=0。
- H 示例：目标占比 0.2、10 篇全读其他标签 → **85.7**；本标签读 5 篇 → **0**；无阅读 → **50**。
- 无评分：U=50、置信度 0；10 次 5 分且 λ=5 时标签亲和度 **83.3333**、置信度 **0.5**、U **66.7**；低覆盖率再乘 `coverage/0.3`。
- 自建探针额外验证：`by_primary_tag={"T01":120}` 使 90 天前文献 F 从 72.2 变为 **61.5**；关闭 `backlog_dampener` 后，积压 21 篇时 H 从 **67.9** 恢复为 **85.7**。

### 4.2 EMA、快照与历史

- 连续两日快照：第二日 EMA 使用第一日 `rank_score`，原始 EMA 与 ±8 夹取结果可由公式精确复算；同日重复生成返回 `created=0, existing=1`，不重复平滑。
- 次日追加后，前一日的解释 payload 保持不变；归档/状态变化不改写历史快照（现有集成测试）。
- 同日静态输入 hash 或主标签依据变化会拒绝整批写入；倒序补算拒绝；注入中途失败时整批回滚（现有集成测试）。
- 静态输入 hash 变化时 `reset=True` 的算术分支由纯函数单测覆盖。当前产品在 G4 绑定不变时不会追加新的静态快照；端到端触发依赖 P7 的版本治理/重新绑定流程，P5 文档已将该流程列为范围外。

### 4.3 队列合格性

- 特殊 FILING 桶（review/classic/distant）只返回 `special_bucket`，没有 P/rank；主题 FILING 上的特殊 MEMBERSHIP 徽章不排除文献（现有单元/集成测试）。
- 低相关度（< max(55, 配置门槛)）、未通过摘要、静态/分类/标签版本漂移、开放 review、未经批准的低置信度分类均被排除并给出原因（现有集成测试；另测 `R=54.9`）。
- 没有 review 的历史条目：解释明确含 `relevance_evidence_unavailable`、R 置信度 0，同时保留摘要/静态证据，不伪造 R 证据。
- 特殊桶阅读按主标签计入 H；评分修订按周截止前最后一条生效，备注列不被 SQL 读取（现有集成测试）。
- 独立探针验证：3 篇合格文献分页 `total=3`、`limit=2/offset=0` 返回前两篇、`offset=2` 返回第三篇；读取前后 `score_snapshot` 与事件计数不变。
- 暂停时段读取队列允许，生成快照拒绝；安静时段（Asia/Shanghai 16:00 落在 15:00–17:00）读取允许、生成拒绝。

### 4.4 G4 绑定与配置护栏

- 保存新配置版本 `9.9.9` 后，队列/解释仍使用 G4 选定的 `1.0.0`：解释 `config_version=1.0.0`，组件权重与层权重保持 `0.70/0.20/0.10` 与 `0.65/0.20/0.15`。`import` 保存新版本不等于激活。
- NaN/Inf、层内/层间和不为 1、`wR<0.5`、`wS<0.6`、`wA>0.2`、`fast>default>slow` 等非法配置在保存/计算前被拒绝；非法保存不留下配置或事件（现有测试）。

### 4.5 API/CLI 边界

- G4 前 `GET /queue`、`POST /scores/snapshot` → **409**；非法日期格式、`limit=0`、`limit=201` → **422**；缺失文献 explain → **404**；无快照 explain → **404**。
- 未来日期快照 → **409**（冲突语义）；未来日期队列返回空 200；未来日期 explain → 404。倒序补算同样为 409。
- CLI `scores snapshot`、`queue`、`score explain` 均可返回 0 和可解析 JSON；非法分页返回非 0 并输出可操作错误。
- 真实 Uvicorn + 子进程 CLI 链路覆盖 G4 前拒绝 → G1–G4 → ingest → process → review → snapshot → queue/explain → 同日重复生成。

---

## 5. 未发现阻断缺陷

本轮未发现以下 P5 关键契约的违背：

- P 分层算术、EMA、±8 限制、静态重置、稳定并列排序；
- 无评分/无阅读中性，缺失 A/E 不产生负分；
- 特殊桶排除与徽章不误伤；
- 配置护栏与 G4 精确绑定；
- 日快照不可变、同日幂等、倒序拒绝、事务回滚；
- 队列只含合格候选、读取不写库、解释含分项/权重/贡献/置信度/版本/证据；
- 周度评分截止、修订、覆盖率弱化与备注隐私。

---

## 6. 非阻断限制、债务与文档同步建议

| ID | 级别 | 内容 | 处理建议 |
|----|------|------|----------|
| N1 | 文档漂移 | [AGENTS.md](../../AGENTS.md)、[README.md](../../README.md)、[P5-ranking.md](P5-ranking.md) 仍称“未合并/未推送/CI 未验证”，但 PR #7 已合并、CI 已绿 | 关闭提交中改为“独立验收通过、已合并、CI 通过、tag 待发布” |
| N2 | 发布流程 | 无 P5 tag；P2/P3/P4 tag 也仍未创建 | 按发布流程补 tag，不在本轮擅自创建 |
| N3 | 范围限制 | 静态重算 reset 的端到端触发依赖 P7 版本治理/重新绑定；当前只有纯函数单测覆盖 | 保持 P5 文档“版本治理属于 P7”的边界，不在 P5 补激活流程 |
| N4 | 测试卫生 | `priority_golden.json` 的 `cold_start` 条目没有被测试直接读取（冷启动由单元/集成/e2e 覆盖） | 可后续让 golden 测试显式断言该条目 |
| N5 | 继承债务 | P1 F5：`StarletteDeprecationWarning`（httpx → httpx2）仍在 | 依赖升级窗口处理 |
| N6 | 继承限制 | P4 部分历史条目无 R 原始证据；P5 明确标记 unavailable、R 置信度 0，不伪造 | 后续版本治理或证据回填时另行设计 |
| N7 | 测量限制 | P5 文档记录的组合覆盖率测量受 SQLAlchemy C 扩展错误影响；本轮不将其作为全 scoring 覆盖率证据 | 维护窗口排查工具兼容 |

---

## 7. P5 能力与 P6 交接

**现在能做什么**

- 离线确定性计算 F/H/D、U、P、rank_score 与并列表述；
- 按 G4 精确绑定批量追加不可变日快照、原子推进 classified→scored→queued；
- 读取分页队列、单篇当前/历史解释；特殊桶只返回排除原因；
- 无评分/无阅读时中性；不发送、不写阅读反馈、不归档。

**不能做什么（P5 范围外）**

- 日报渲染/推送/调度、阅读操作 API、备注/反馈写模型、归档/导出；
- 特殊桶分档/配额、自定义视图/权重影响预览；
- 用合成 golden 宣称真实模型或真实阅读数据质量；
- 自动激活新评分配置（P7）。

**P6 可直接使用**

- `create_snapshots` / `read_queue` / `explain_score` 与 `QueueItem`、`ScoreExplanation`、`RankConfig` 契约；
- 队列按 rank_score、R、出版日、A、标题、paper_id 稳定排序；
- 解释中的日期、版本、静态输入 hash、动态/偏好截止和证据；
- P6 编排应在此基础上做容量/多样性/冷却，不重算静态分、不扩大 R 门槛、不混入特殊桶。

---

## 8. 最终判定

**P5-S1–S6 通过独立验收。** 本地阶段门、全新 clone（Python 3.14.7）、Docker 容器、远程 CI 与独立边界探针均通过；未发现 P5 新增的 P0/P1 级阻断缺陷。P5 可以关闭，P6 可以开始。

发布状态需如实记录：**PR #7 已合并到 main，CI 已通过，P5 tag 尚未创建**。本轮未修改实现代码、未创建 tag、未推送新的提交。

---

## 9. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收 commit：`c9854fd`；远程 `main`：`ed2409b`（树一致）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`docker compose build/run`、`git diff --check`
- 额外检查：全新 clone（Python 3.14.7）全部门禁、Docker 内 doctor/health/papers/G4 门禁、GitHub Actions run/jobs、P5 算术/EMA/分页/只读/安静时段/G4 绑定独立探针
- 验收产物：本报告
