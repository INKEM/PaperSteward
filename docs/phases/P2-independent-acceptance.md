# P2 独立验收报告

> **验收对象**：P2 初始化协商与确认门（P2-S1–S6）  
> **验收 commit**：`f3b4a1b`（`feat(p2-s6): add onboarding CLI and close local phase acceptance`）  
> **验收日期**：2026-10-22  
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、Docker build/run、G1–G4 边界与门禁复核、GitHub Actions 查询  
> **结论**：**有条件通过（Conditional Pass）**。本地阶段门、全新 clone 与当前代码 Docker 运行均通过；CI 尚无可归属于 P2 的运行证据，因此 P2 发布门未完全闭合，不打 `p2-done` tag。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：129 文件格式化；67 源文件 strict mypy；**567 passed / 3 e2e excluded** |
| `make test-integration` | **通过**：**209 passed** |
| `make e2e` | **通过**：**3 passed**（walking skeleton、API G1–G4/门禁、CLI G1–G4） |
| `make migrate-check` | **通过**：base → 0004 → base → 0004 |
| `make demo` | **通过**：临时库迁移、seed、`/health`、`/papers` |
| `make secret-scan` | **通过** |
| 全新 clone（默认 Python 3.14） | **通过**：bootstrap / check / integration / migrate / e2e / demo / secret / compose config |
| Docker build/run（当前代码） | **通过**：镜像构建成功；UID 10001；容器 healthy；`/health`、`/papers`、`/onboarding` 正常 |
| G1–G4 顺序/陈旧/重复确认 | **通过**：集成与 e2e 覆盖 |
| G4 前 enqueue/push/archive 门禁 | **通过**：仓储层统一门禁测试；G4 后放行 |
| CI | **未取得 P2 证据**：验收时 Actions 仅有 P1 运行；P1 证据不能替代当前提交 |
| P2 tag | **未创建**（与 CI 未闭合一致） |

**总体判定**：**P2 本地实现与可执行验收通过，Docker 已闭环；唯一未闭合的是 CI 发布门。** P3 可以按 P2 handoff 开始，但 P2 阶段 tag 必须等 CI 在 `f3b4a1b`（或其后继提交）上通过后再补。

---

## 2. 阶段门证据

### 2.1 当前工作区

```text
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-accept-check'
  → 567 passed, 3 deselected；ruff/mypy 通过

make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-accept-int'
  → 209 passed

TMPDIR=/dev/shm make migrate-check
  → base → 0001 → 0002 → 0003 → 0004 → base → 0004

make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-accept-e2e'
  → 3 passed

make demo DEMO_PORT=8047
  → /health ok；/papers 返回 p001（discovered）；API 自动停止

make secret-scan
  → OK
```

SQLite 使用 `/dev/shm` 临时目录，避免当前工作区 fsync 阻塞；没有修改 SQLite 同步设置，也没有写入 `var/papersteward.db`。

### 2.2 全新 clone

默认 Python 3.14.7，执行 `make bootstrap` 后：

- `make check`：567 passed（3 e2e 排除）；
- `make test-integration`：209 passed；
- `make migrate-check`：0004 往返通过；
- `make e2e`：3 passed；
- `make demo`：health/papers 正常；
- `make secret-scan`：通过；
- `docker compose config`：通过；
- `git status --short`：空。

---

## 3. Docker 独立复核

使用当前 `f3b4a1b` 代码重新执行 `docker compose build`，构建成功；以独立 Compose project 启动：

```text
docker compose -p p2accept up -d --no-build
container health → healthy
id → uid=10001(papersteward) gid=10001(papersteward)
alembic current → 0004 (head)
/health → 200 {"status":"ok","database":"ok"}
/papers → 200 {"items":[],"total":0}
/onboarding → 200 {"stage":"seeds","revision":0,"next_gate":null}
POST /onboarding/start → 200 {"stage":"profile","revision":1,"next_gate":"g1"}
docker compose -p p2accept down -v → 正常清理
```

结论：**Docker build/run 已闭环**；P2 新增的 `0004` 迁移和 onboarding API 在容器内可用。

---

## 4. G1–G4 与执行门禁复核

### 4.1 顺序与证据绑定

- `test_ordered_flow_persists_across_sessions_with_audit`：按 G1→G4 推进，revision 1→5，事件持久化；重复 `start` 不重复推进。
- `test_reject_skip_stale_duplicate_and_invalid_evidence`：跳步、陈旧 revision、重复确认、证据校验失败均不推进；失败后状态与事件数不变。
- G1 校验档案内容哈希、版本/正文缓存一致、完整性与旧版本归档。
- G2 校验 5–8 主标签、种子覆盖率 ≥90%、单标签 ≤50%、正例/边界案例、档案绑定与队列影响预览。
- G3 校验 15–20 篇独立 cohort、双方完整盲标、Spearman/Top-5、低指标书面接受偏差、最新轮与报告 hash。
- G4 校验最新日报、最新认可反馈、双 hash、revision、参数/配置漂移，并原子保存评分/运行配置后进入 ACTIVE。

### 4.2 G4 前执行门禁

`test_every_pre_g4_stage_blocks_all_execution_paths` 与真实 Uvicorn e2e 覆盖：

- `PaperRepository.add` 直接写入 `queued/pushed/archived` → 拒绝；
- `PaperRepository.transition` 到 `queued/pushed/archived` → 拒绝；
- `ArchiveSnapshotRepository.add` → 拒绝；
- 被拒绝时 paper 状态与事件不写；
- G4 完成后同一组操作放行；测试结束回滚临时 DB。

结论：**P2 计划要求的“G4 前禁止入队/推送/归档”在仓储边界成立。**

### 4.3 CLI 与最小体验

- `make e2e` 包含 CLI 真实子进程 G1–G4 流程；
- `onboarding` CLI 覆盖 start/status、profile/taxonomy 提交、审阅/差异、种子导入、校准建轮与双方标注、试运行日报、四类反馈、G1–G4 显式确认；
- 审阅快照包含服务器/阶段/revision/完整响应/校验和；确认只使用已审快照，要求 `--gate/--by/--summary/--yes`；陈旧快照、跨服务器、缺 hash/revision、无 `--yes` 均拒绝；
- HTTP 客户端只允许 loopback，禁用代理/重定向，不自动重试变更。

---

## 5. 未闭合项与风险

| ID | 级别 | 内容 | 处理 |
|----|------|------|------|
| F1 | **发布阻断（仅 CI）** | 验收时 GitHub Actions 无 P2 head 的运行；现有 3 个 run 均为 P1。P1 CI 不能替代 P2 当前提交 | 推送 P2 分支/PR 触发 CI，确认 quality 3.11/3.12 与 docker smoke 全绿后再打 `p2-done` |
| F2 | 非阻断依赖债 | P1 的 `StarletteDeprecationWarning`（httpx → httpx2）仍存在 | 依赖升级窗口处理 |
| F3 | 非阻断测试债 | P1 F3 的 provider 失败 usage 仍缺独立回归断言 | 下一维护窗口补断言；不影响 P2 功能 |
| F4 | 环境说明 | 当前工作区 fsync 可能阻塞默认 pytest 临时目录，P2 文档要求 `/dev/shm` | 属环境问题；不改变产品配置，CI 使用常规临时目录 |

**未发现 P2 新增的 P0/P1 功能性阻断缺陷。** 未发现 G1–G4 顺序、版本绑定、盲标展示、原子回滚或执行门禁的绕过路径。

---

## 6. 能力、边界与 P3 交接

**现在能做什么**

- 通过 API/CLI 完成 start → G1 档案 → G2 分类法 → G3 校准 → G4 试运行与参数确认；
- G4 前统一阻止入队/推送/归档；G4 后放行；
- 保存追加写事件、配置版本、校准轮次与审阅快照；
- 使用 FakeLLM/合成 fixture 做本地流程验证。

**不能做什么**

- 真实文献采集、摘要/分类/评分、排序、推送/调度、特殊桶实现（P3–P6）；
- 多用户/鉴权、网页 UI、重置/再协商；
- 用合成校准结果宣称真实模型质量；
- 在 CI 未验证时声称 P2 发布门通过或创建 P2 tag。

**P3 可直接使用**

- head `0004`；`onboarding` + `onboarding_calibration` 表；
- `P2-onboarding.md` 的 P3 handoff；
- 所有入队/推送/归档必须继续经过 `ensure_active` 或等价统一门禁；
- `calibration/basic.json`、`dry_run/basic.json` 与 CLI 可重放确认链路；
- P1 的 F5/F3 债务继续记录。

---

## 7. 最终判定

**P2-S1–S6 本地验收与 Docker 验收通过；P2 阶段发布门因 CI 未验证而保持开放。**  
建议在 P3 启动的同时触发 P2 CI；CI 全绿后补 `p2-done` tag，即可把 P2 标记为完全关闭。

---

## 8. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`docker compose config`、`docker compose build/up`
- 额外检查：全新 clone（默认 Python 3.14）、G1–G4 门禁测试、CLI e2e、Docker 内 `/onboarding` start、GitHub Actions run 查询
- 验收产物：本报告
