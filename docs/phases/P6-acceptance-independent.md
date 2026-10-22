# P6 独立验收报告

> **验收对象**：P6 日报推送、阅读反馈与归档（P6-S1–S7）  
> **验收 commit**：`57e6db8`（PR #8 head）；远程 `main` 合并提交 `4650e25`，与 `57e6db8` 树完全一致  
> **验收日期**：2026-10-22  
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、Docker、CI 查询、真实 headless Chrome/HTMX 闭环，并做周任务与投递状态机专项探针  
> **结论**：**未通过（Not Accepted）**。阶段门、全新 clone、Docker、CI 与浏览器闭环均通过，但 **P6-S7 周任务漏掉已投递未读文献（F1）**，且 **投递第二次 preflight 失败会被错误记录为 SMTP `uncertain`（F2）**。两项均为 P6 契约内的必修缺陷；P6 不能关闭，P7 暂不开始，P6 tag 不创建。
>
> **后续状态**：F1/F2 与 N1–N4 已返工；独立复验通过（`cabd0af` / main `3026696`），见 [P6 返工验证记录](P6-acceptance-rework.md) §9。本报告保留 `57e6db8` 时点的历史结论。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：243 文件格式检查；ruff lint 通过；strict mypy **120** 源文件；**939 passed / 4 deselected** |
| `make test-integration` | **通过**：**297 passed** |
| `make e2e` | **通过**：**4 passed**（真实 Uvicorn + 子进程 CLI，覆盖 digest/UI/archive） |
| `make migrate-check` | **通过**：base → **0010** → base → 0010 |
| `make demo`（临时库） | **通过**：迁移、seed、`/health`、`/papers` 正常 |
| `make secret-scan` / `git diff --check` | **通过** |
| 全新 clone（Python **3.14.7**） | **通过**：bootstrap / check / integration / migrate / e2e / demo / secret / wheel 静态资源，`git status` 为空 |
| Docker build/run | **通过**：镜像构建成功；UID 10001；head 0010；`/health` 200；`doctor` OK；UI/CSP/静态 HTMX 正常；G4 前 digest 409 |
| CI | **通过**：PR run [39766917474](https://github.com/INKEM/PaperSteward/actions/runs/39766917474)（`57e6db8`）与 main push run [39767303502](https://github.com/INKEM/PaperSteward/actions/runs/39767303502)（`4650e25`）三个 job 全绿 |
| 真实浏览器 | **通过**：Chrome 156 headless/HTMX，完成评分+备注+归档，归档页可见备注 |
| 远程发布状态 | PR #8 已于 2026-10-22T11:00:14Z 合并；`origin/main = 4650e25`；无 P6 tag |
| P6 阻断缺陷 | **2 项**：F1 周任务漏 PUSHED；F2 假 `uncertain` |

**总体判定**：**工程门与发布证据通过，但两项 P6 功能契约不满足；P6 未通过独立验收。**

---

## 2. 阶段门证据

### 2.1 当前工作区（`57e6db8`）

```text
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-accept-check'
  → 939 passed, 4 deselected；ruff/mypy 120 源文件通过

make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-accept-int'
  → 297 passed

TMPDIR=/dev/shm make migrate-check
  → base → 0010 → base → 0010

make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-accept-e2e'
  → 4 passed

PAPERSTEWARD_DATABASE_URL=sqlite+pysqlite:////dev/shm/p6-accept-demo.db \
  make demo DEMO_PORT=8789
  → /health {"status":"ok","database":"ok"}；/papers total=1

make secret-scan / git diff --check
  → OK
```

### 2.2 全新 clone

`git clone --no-hardlinks .` 到临时 tmpfs，默认 Python 3.14.7，执行 `make bootstrap` 后：

```text
make check            → 939 passed, 4 deselected
make test-integration → 297 passed
make migrate-check    → upgrade/downgrade/upgrade OK（0010）
make e2e              → 4 passed
make demo             → /health ok；/papers total=1
make secret-scan      → OK
wheel                 → 含 htmx-2.0.11.min.js / HTMX-LICENSE / style.css
git status --short    → 空
```

与工作区结果一致；未发现 Python 3.14 或全新安装依赖导致的 P6 回归。

### 2.3 远程状态与 CI

```text
origin/main = 4650e25fdd7867cff1afe80ba339cd74d8cb7893
HEAD        = 57e6db8b1fb59a734ba0270745ccae7725877b07
git diff HEAD origin/main → 空（树一致）
git tag --list / git ls-remote --tags origin → 无 tag
```

| 工作流 | commit | 结果 |
|---|---|---|
| [PR #8 checks](https://github.com/INKEM/PaperSteward/actions/runs/39766917474) | `57e6db8` | `quality (py3.11)`、`quality (py3.12)`、`docker compose smoke` 全部 success |
| [main push](https://github.com/INKEM/PaperSteward/actions/runs/39767303502) | `4650e25` | 三个 job 全部 success |

远程 CI 的 Docker smoke 已通过；本轮也独立执行了本地 Docker。验收开始时 P6 文档仍写“Docker 未验证/CI 未执行”；本报告所在提交已同步状态（见 N5）。

---

## 3. Docker 与真实浏览器复核

### 3.1 Docker

使用当前代码执行 `docker compose -p p6accept build`，独立容器启动：

```text
docker run -d --rm --name p6accept-8078 -p 127.0.0.1:8078:8000 papersteward:dev
GET /health → 200 {"status":"ok","database":"ok"}
docker exec id → uid=10001(papersteward) gid=10001(papersteward)
docker exec alembic current → 0010 (head)
docker exec papersteward doctor → 全部 OK
GET /ui/today → 200，含 CSP：default-src 'self'; script-src 'self'; ...
GET /ui/static/htmx.js → 200
POST /digests（G4 前）→ 409 conflict
```

容器只使用自身临时文件系统，未触碰工作区数据库；验收后已停止。

### 3.2 真实浏览器

用真实 Uvicorn + 临时 G4/日报数据库，启动 headless Chrome 156（CDP）：

- `/ui/today` 加载本地 HTMX（`typeof htmx === "object"`），4 个表单；
- 在浏览器内选择实际阅读方式「精读」、评分 5、填写备注并提交；
- HTMX 返回「已保存阅读记录：archived」；
- `/ui/archive` 可见该备注；`/archive/p` 返回冻结内容；
- 无头/跨站/缺同源头 POST 被 UI guard 拒绝（409）；同源普通表单返回 303。

浏览器使用合成 fixture，不代表真实用户日报内容质量。

---

## 4. 独立契约与边界复现

### 4.1 日报编排与渲染

- 配额/多样性/H 饥饿槽/清库存/冷却/延期：单元与独立探针通过；默认配置下主队列 7、每标签 2、精读 2/粗读 5；
- 精读溢出默认延期；显式 `allow_downgrade` 才降级并注明原因；
- Markdown/HTML golden；标题/摘要/理由/完整解释/特殊占位/待审数齐全；
- 恶意标题/摘要/脚本 URL 被转义或丢弃；`javascript:`、`data:`、带用户名/密码的 URL 被拒绝（但见 N4）。

### 4.2 投递与幂等

- 同日期 digest 唯一，重复生成返回原内容；
- 同日期/渠道投递唯一；文件 0600、原子不覆盖、相同内容恢复、不同目标拒绝；
- SMTP 仅 G4 明确选择时可用；预约先提交，外部 I/O 后确认；
- SMTP 失败留 `uncertain`，不自动重试；文件仍可独立成功；跨日 reserved/uncertain 不重推；
- 暂停/安静生成拒绝；发送前检查 G4、本地今日、push_time、条目状态和冷却；
- 历史日报发送拒绝；已送达回执在完成后仍可幂等读取。

### 4.3 阅读动作、归档与导出

- done/score/note/snooze/skip/reject；严格评分、请求 ID 幂等/冲突、修订历史、冷却；
- done 与 Rating、完整内容、`archived`、审计事件同事务；失败回滚；
- 归档冻结元数据/摘要/标签桶/徽章/建议与实际方式/完整 S/D/U/P/版本；
- 修订只追加 Rating/事件，不回写初始归档；P1 空内容标记不可用；hash 不匹配拒绝；
- BibTeX/CSV/Markdown/JSON 导出含标签/评分/备注/链接/修订；CSV 防公式注入；文件 0600 不覆盖；
- 归档迁移 `0010↔0009` 保留初始评分/备注/状态，删除完整内容后明确不可用。

### 4.4 周任务

- 周截止 00:00 排他、同周幂等、失败报告可显式新 run_id、覆盖率 <30% 弱信号、null 不当低分；
- 修订只使用截止前最后一条；归档 P 不被回写；备注/归档正文不被 SQL 读取；
- 特殊桶/已归档不产生 P；读取历史优先归档时元数据/标签；
- **发现 F1：PUSHED 未读候选不进入任务。**

---

## 5. 阻断缺陷

### F1（高，阻断）：周任务漏掉 `PUSHED` 未读候选

**复现**：

```text
G4 + seed(p) + create_digest(DAY) + deliver(local file)
  → p.status = pushed
refresh_weekly(now=DAY+3 Sunday)
  → status=succeeded
  → stats: created=0, existing=0, excluded=21, scores=[]
  → p 仍为 pushed；只有 DAY 的快照，没有周日快照
```

原因：

- `src/papersteward/scoring/queue.py:56` 的 `ELIGIBLE` 不含 `PaperStatus.PUSHED`；
- `src/papersteward/jobs/preference.py:119` 直接调用 `create_snapshots`，没有像 `create_digest` 那样先把 PUSHED 重新入队（对比 `delivery/service.py:94-109`）；
- `src/papersteward/jobs/preference.py:131` 的报告查询同样只用 `ELIGIBLE`。

影响：

- 日报正常投递后，未读文献全部是 `PUSHED`；周任务在文档约定的周日 20:00（投递后）运行时，**不会为这些主队列文献生成/报告快照**；
- 若次日没有日报任务，周 U 更新不会落到这些候选；
- 与 [P6 S7 卡片](P6-delivery.md) “只写未读当日新快照”、[delivery-cli.md](../delivery-cli.md) “只处理未读合格主题候选”冲突。

**建议修复**：在 `refresh_weekly` 中先对 `PUSHED`（以及需要恢复的 due snoozed）建立与 `create_digest` 等价的重新入队，或定义专门的周任务候选集合；报告查询同步；补“deliver→refresh_weekly”回归。

### F2（高，阻断）：第二次 preflight 失败被记录为 SMTP `uncertain`，但从未尝试发送

**复现**：

```text
G4(channel=smtp) + create_digest(DAY)
monkeypatch _preflight：第1次（预约事务）通过，第2次（外部 I/O 前复核）抛 InvariantViolation
send_smtp 从未被调用（sent calls = []）
→ deliver 抛“邮件送达结果未知，请人工核对；未自动重试”
→ DeliveryRow.status = uncertain
```

原因：

- `src/papersteward/delivery/service.py:260-284` 将第二次 `_preflight` 与外部 I/O 包在同一个 `try/except Exception` 中；
- 任何异常（包括纯校验失败、pause/quiet 变化、条目状态变化）都按“可能已发送”的 SMTP `uncertain` 处理。

影响：

- 没有发生任何邮件网络请求，却被标记为“送达结果未知”，用户会被告知需要人工核对；
- `uncertain` 按设计禁止自动重试，造成不必要的永久阻塞；
- 与 [ADR0016](../adr/0016-digest-actions-archive.md) “SMTP 异常或进程崩溃留下 uncertain”语义不符；preflight 失败属于“未发送”。

**建议修复**：把第二次 preflight 与外部 I/O 分开捕获；preflight 失败应保留 `reserved`/显式删除预约或标记为可重试的 `failed_preflight`，只有实际尝试过 SMTP（或不确定是否已进入网络发送）才允许 `uncertain`；补并发/二次 preflight 回归。

---

## 6. 非阻断问题与限制

| ID | 级别 | 内容 | 建议 |
|----|------|------|------|
| N1 | 中 | 周日日报已生成同日快照后，周任务 `create_snapshots` 不会改写已有快照；报告中的 U 可能仍是旧快照值，新 U 实际要到下一日快照才应用 | 明确“周任务安排在日报前/次日应用”的语义，或在文档中声明 next-day 生效；补调度说明 |
| N2 | 低 | `/ui/today?day=...` 展示指定日期；但无日报时的生成表单 POST `/ui/digests` 忽略查询日期，只生成今天 | 让生成表单携带日期，或移除未文档化的 day 查询 |
| N3 | 低 | `delivery-cli.md` 称用户主动完成队列文献会写 `user_read` 触发；代码实际写 `paper.pushed` 事件并带 `payload.trigger=user_read`，没有独立 `user_read` 事件 | 统一文档与事件命名，或在 ADR 中说明该 payload 即触发标记 |
| N4 | 低（安全） | `safe_url` 拒绝 `https://u:pw@host`，但仍放行 `https://:pw@host`（空用户名+密码） | 拒绝任何 `username`/`password` userinfo；补 URL 测试 |
| N5 | 文档 | 验收开始时 `AGENTS.md`、`P6-delivery.md`、`P6-acceptance.md`、README 仍写“Docker 未验证/远程 CI 未执行/独立验收待执行”，但 PR #8 已合并、CI 和 Docker smoke 已绿 | 本报告所在提交已同步状态；不要把远程 CI 误记为未执行 |
| N6 | 质量 | digest 的 `reason` 主要是通用 R/E 描述，未引用具体证据；“精读溢出”只记录 deferred，没有次日精读保留/老化字段 | P7/P8 规则治理时增强；不阻断 P6 主流程 |
| N7 | 继承 | P1 F5 Starlette/httpx 弃用警告仍存在；P2–P5 tag 尚未创建；无真实 SMTP 厂商/送达质量验收 | 保持记录，发布流程处理 |

---

## 7. 已通过的范围

- P6-S1 编排：7/2/2/5、H 饥饿槽、至少两标签（候选足够时）、清库存优先首次入队、冷却/延期/配额/重推阈值；
- P6-S2 渲染：MD/HTML golden、完整解释、特殊桶占位、转义/URL 白名单；
- P6-S3 投递：文件/SMTP、日期/渠道唯一、预约先提交、0600/不覆盖/恢复、失败隔离、G4/本地今日/push_time/暂停安静；
- P6-S4 阅读：六种动作、严格评分、request_id 幂等/冲突、事件/Rating/状态原子、冷却、归档接入；
- P6-S5 归档：完整冻结内容、hash、修订历史、P1 空内容、BibTeX/CSV/Markdown/JSON、迁移；
- P6-S6 页面：四页、CSP、同源 guard、本地 HTMX、真实浏览器闭环；
- P6-S7 周任务的大部分算术/截止/覆盖率/幂等/隐私/归档不回写（但 F1 的候选集不完整）；
- 无新增 Python 运行依赖；HTMX 本地分发、0BSD 许可证、wheel 静态资源齐全；无真实来源/LLM/邮件测试。

---

## 8. 继承债务

- P1 F5：Starlette/httpx 弃用警告；
- P1 F3：provider 失败 usage 的独立回归断言；
- P2–P5 tag 未创建；P6 tag 未创建；
- 真实 SMTP 厂商 TLS/送达质量、真实用户日报审阅未验收；
- P4 部分历史 R 原始证据缺失；P5/P6 已按 unavailable/置信度 0 处理。

---

## 9. 返工验收条件

P6 返工后至少满足：

1. **F1**：`refresh_weekly` 对 `PUSHED` 未读候选生成/报告快照（或文档明确重新定义范围）；新增回归：`deliver → refresh_weekly` 后 PUSHED 候选出现在报告且获得当日 U，已归档 P 不被回写。
2. **F2**：第二次 preflight 失败不得在未发生 SMTP I/O 时写 `uncertain`；新增回归：monkeypatch 二次 preflight 失败、断言 `send_smtp` 未调用且状态不是 `uncertain`。
3. 明确 N1 的周任务调度与同日快照语义，补对应文档/测试。
4. 修复或记录 N2/N3/N4；N4 建议直接拒绝任何 userinfo。
5. 重新运行 `make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`，并重新执行全新 clone、Docker、CI 与独立复验。

---

## 10. 最终判定

**P6-S1–S7 的工程门、Docker、CI、全新 clone 与浏览器闭环通过，但存在两项 P6 契约内阻断缺陷，P6 独立验收不通过。**  
P6 保持未关闭，不创建 P6 tag；P7 不应在 F1/F2 修复并复验前开始。远程 `main` 已包含 PR #8，但需要后续返工提交/PR。

---

## 11. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码；临时探针已删除）
- 验收 commit：`57e6db8`；远程 `main`：`4650e25`（树一致）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`git diff --check`、`docker compose build/run`
- 额外检查：Python 3.14.7 全新 clone 全部门禁、wheel 静态资源、Chrome 156 + HTMX 浏览器闭环、UI CSP/CSRF、周任务 PUSHED 探针、二次 preflight 状态探针
- 验收产物：本报告
