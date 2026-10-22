# P6 返工验证记录

> **对象**：P6 独立验收 F1/F2 阻断缺陷与 N1–N4 非阻断问题
> **基线**：独立验收 commit `57e6db8`；返工起点 `5684be0`（已合并 `4650e25` + 验收文档）
> **日期**：2026-10-22
> **性质**：实现方返工与自检记录，附独立复验结论（§9）。独立复验已通过；
> P6 可以关闭，P7 可以开始；P6 tag 仍待发布流程。

## 1. 结论摘要

| 编号 | 级别 | 处理 | 回归 |
|---|---|---|---|
| F1 周任务漏 PUSHED | 阻断 | 已修复：周候选集合加入 PUSHED，报告查询同步；到期 snooze 同时恢复 | `test_weekly_includes_delivered_unread_candidates`、`test_weekly_resumes_due_snoozed_candidate` |
| F2 二次 preflight 假 uncertain | 阻断 | 已修复：复核与 I/O 分开捕获；复核失败记 `failed` 且说明未尝试投递 | `test_second_preflight_failure_is_not_uncertain_and_never_sends` |
| N1 同日快照语义 | 中 | 已明确并测试：同日快照不重写；按设计在 20:00、当日日报投递后运行 | `test_weekly_same_day_snapshot_is_not_rewritten`；delivery-cli/ADR0016 |
| N2 日期表单 | 低 | 已修复：生成表单携带所选日期并回到该日期 | `test_today_generate_form_uses_selected_day` |
| N3 user_read 命名 | 低 | 已澄清文档：`queued→pushed` 时写 `paper.pushed` + `payload.trigger=user_read`，已 pushed 不重复写，无独立事件 | delivery-cli/ADR0016 |
| N4 userinfo URL | 低（安全） | 已修复：拒绝任何用户名或密码 | `test_untrusted_content_and_unsafe_urls`（新增空用户名/空密码用例） |

## 2. F1：周任务覆盖已投递未读

- `scoring/queue.py` 新增 `WEEKLY_ELIGIBLE = ELIGIBLE | {PUSHED}`；`_eligibility` 与
  `create_snapshots` 接受候选集合，默认行为仍是原 `ELIGIBLE`。
- `jobs/preference.py` 使用 `WEEKLY_ELIGIBLE` 生成快照并过滤报告；PUSHED 不重新入队，
  不影响投递历史。`_resume_due_snoozed` 复用 `delivery_history`，把 snooze 已到期的
  SNOOZED 按日报相同规则恢复为 QUEUED，报告新增 `resumed`。
- 回归流程：seed → 生成日报 → 本地投递（候选变 PUSHED）→ 阅读归档其中一篇 →
  `refresh_weekly`。断言未读 PUSHED 出现在报告并得到周日快照、状态保持 PUSHED；
  已归档文献不出现且既有快照不变。另断言到期 snooze 被恢复。
- 已知非阻断限制：周任务会为全部历史 PUSHED 未读追加当日快照；长期积压时快照量随
  未读存量和周数增长。当前符合“给未读候选更新U”的 P6 范围，窗口化留给 P7/P8 治理。

## 3. F2：复核失败不再伪造 uncertain

- `delivery/service.py` 把第二次 `_preflight` 单独放在一个 try 中；失败时在独立事务把
  预约写成 `failed`，错误信息包含“未尝试投递”，审计事件带 `stage=preflight`。
- 只有实际 `write_file`/`send_smtp` 抛错才进入原失败分支：文件 `failed`，SMTP
  `uncertain`。SMTP 复核失败后，再次 deliver 会把该预约重置为 `reserved` 并重试；
  本地文件仍要求 `--recover-file`。
- 回归：monkeypatch 第一次复核通过、第二次抛 `InvariantViolation`；断言
  `send_smtp` 从未调用、状态为 `failed` 且不是 `uncertain`、无 `finished_at`；
  恢复真实复核后同一预约成功发送且只发送一次。

## 4. N1–N4

- N1：同日快照保持不可变。按设计在 update_weekday 20:00、当日日报投递后运行周任务：
  当日 PUSHED 进入候选，已有同日快照不被重写，报告复用该快照，下一自然日快照继续使用
  同一周截止。若提前到投递前运行，当日稍后才变 PUSHED 的条目不进入本次 Run（同 run_id
  幂等，不会事后补录），但仍由日报创建当日快照。`test_weekly_same_day_snapshot_is_not_rewritten`
  断言 `created=0`、`existing>=1`、历史快照逐条相等。
- N2：`/ui/today?day=...` 无日报页面的表单带 hidden `day`；`POST /ui/digests` 使用该
  日期，非今日时跳回 `/ui/today?day=...`。保留大小限制与非法日期拒绝。
- N3：文档统一事件命名为 `queued→pushed` 时写 `paper.pushed` 并标记
  `payload.trigger=user_read`；已 pushed 不重复写，代码未新增独立事件类型。
- N4：`safe_url` 用 `username is not None or password is not None` 拒绝全部 userinfo，
  覆盖 `https://u:pw@host`、`https://:pw@host`、`https://user:@host`。

## 5. 阶段门证据

| 检查 | 结果 |
|---|---|
| `make check` | **944 passed / 4 deselected**；ruff format/lint 通过；strict mypy **120** 源文件 |
| `make test-integration` | **302 passed** |
| `make e2e` | **4 passed**（真实 Uvicorn + CLI，含日报/UI/归档） |
| `make migrate-check` | **通过**：base→0010→base→0010 |
| `make demo`（独立临时库 8791） | **通过**：迁移、seed、`/health`、`/papers` 正常，服务已停止 |
| `make secret-scan` / `git diff --check` | **通过** |
| Docker build/run（`p6rework`, `papersteward:dev`） | **通过**：构建成功；UID 10001；head 0010；`/health` 200；doctor OK；`/ui/today` 200 且带 CSP；本地 HTMX 200；G4 前 `POST /digests` 409 |

复现命令：

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-rework-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-rework-int'
make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-rework-e2e'
TMPDIR=/dev/shm make migrate-check
PAPERSTEWARD_DATABASE_URL=sqlite+pysqlite:////dev/shm/p6-rework-demo.db \
  make demo DEMO_PORT=8791
make secret-scan
git diff --check
```

所有 SQLite 使用 pytest `tmp_path`/`/dev/shm` 或独立临时库，未触碰 `var/papersteward.db`。
未调用真实来源、LLM 或 SMTP；浏览器/邮件质量不在本轮返工范围。

## 6. 尚未执行 / 限制

- 本轮返工 diff（`5684be0..HEAD`）为 18 文件 / 521 行（467 新增 + 54 删除，含文档），
  超过单卡 8 文件/400 行指引；原因是 F1/F2 两项跨 scoring/jobs/delivery/API 的阻断修复、
  N1–N4、5 项回归与返工/交接文档必须同交，未夹带 P7 或其他重构。
- 全新 clone 验证见下节；远程 CI 需要推送返工提交后由 CI 执行，独立复验也需要由
  未参与实现的验收者执行。本记录不声称独立复验已通过。
- 首次真实日报审阅、真实 SMTP 厂商 TLS/送达质量仍未验收。
- P6 tag 未创建；P2–P5 tag 与 P1 F5/F3 债务保持原状。
- 返工不改变 P7 范围：特殊桶完整规则、自定义视图、权重治理仍不开始。

## 7. 全新 clone

在 `a6fbe58` 执行 `git clone --no-hardlinks . /dev/shm/p6-rework-clone`，用系统
Python **3.14.7** 走 `make bootstrap`（全新 `.venv`、安装运行/开发依赖、迁移临时库），
再执行阶段门：

| 检查 | 结果 |
|---|---|
| `make bootstrap` | **通过**：新 venv、依赖安装、Alembic 升级到 0010 |
| `make check` | **944 passed / 4 deselected**；ruff、strict mypy **120** 源文件通过 |
| `make test-integration` | **302 passed** |
| `make migrate-check` | **通过**：base→0010→base→0010 |
| `make e2e` | **4 passed**（真实 Uvicorn + 子进程 CLI） |
| `make secret-scan` | **通过** |
| `git status --short` | 空 |

克隆位于临时 tmpfs，验证后未保留；未使用工作区的 `.venv`、`.env` 或 `var` 数据库。
clone 之后仅有本文档的补充提交，不改变代码或测试。**正式独立复验应固定在包含本记录的
最终提交**；如验收方坚持复验 clone 时的代码，可 checkout `a6fbe58`（代码树相同）。

## 8. 回滚

返工不新增迁移、依赖或状态值。回退代码即可恢复旧行为；已产生的 PUSHED 状态、日报、
评级、归档与投递记录不回滚。F2 只改变失败预约的状态记录；若已产生 `failed` 预约，
回旧代码前先核对是否实际发送过，SMTP `uncertain` 仍必须人工核对。

## 9. 独立复验结论（2026-10-22）

> 复验 commit：`cabd0af`；远程 `main` 合并提交 `3026696`，与本 commit 树一致。
> 独立执行，不修改实现代码；临时探针在复验后删除。

**结论：通过（Accepted）。** F1、F2 两项阻断缺陷已闭环；N1–N4 已处理或澄清；未发现新的阻断缺陷。P6 可以关闭，P7 可以开始；P6 tag 尚未创建。

### 9.1 阶段门与发布证据

```text
make check            → 944 passed, 4 deselected；ruff 243 文件；strict mypy 120 源文件
make test-integration → 302 passed
make migrate-check    → base → 0010 → base → 0010
make e2e              → 4 passed
make demo DEMO_PORT=8791（临时库）→ /health、/papers 正常
make secret-scan / git diff --check → OK
全新 clone（Python 3.14.7）→ bootstrap/check/integration/migrate/e2e/demo/secret 全通过，git status 空
Docker build/run → UID 10001；head 0010；/health 200；doctor OK；/ui/today CSP；HTMX 200；G4 前 digest 409
```

- PR #9：`cabd0af`，[run 39782896364](https://github.com/INKEM/PaperSteward/actions/runs/39782896364)
  `quality (py3.11)`、`quality (py3.12)`、`docker compose smoke` 全绿；
- main：`3026696`，[run 39783913436](https://github.com/INKEM/PaperSteward/actions/runs/39783913436)
  三个 job 全绿；
- PR #9 已于 2026-10-22T13:06:05Z 合并；`origin/main = 3026696`；无 P6 tag。

### 9.2 F1 复验：已投递未读进入周任务

复现真实顺序：生成日报 → 本地投递（候选变 `PUSHED`）→ 阅读归档其中一篇 → `refresh_weekly`：

- 未读 `PUSHED` 出现在 `stats.scores`，`U > 50`；
- 候选仍保持 `PUSHED`，没有新增 `paper.queued` 事件（未重新入队）；
- 获得周日快照；已归档文献不进入报告，既有快照逐条不变；
- 到期 snooze 恢复为 `QUEUED`，`resumed` 计数；官方回归
  `test_weekly_includes_delivered_unread_candidates` 与
  `test_weekly_resumes_due_snoozed_candidate` 通过。

### 9.3 F2 复验：复核失败不再伪造 uncertain

Monkeypatch 第一次 preflight 通过、第二次（I/O 前）抛 `InvariantViolation`：

- `send_smtp` 从未调用（`sends == []`）；
- `DeliveryRow.status == "failed"`，不是 `uncertain`；
- `row.error` 包含“未尝试投递”，`finished_at is None`；
- 恢复真实 preflight 后，同一预约重试成功且只发送一次；
- 官方回归 `test_second_preflight_failure_is_not_uncertain_and_never_sends` 通过。

### 9.4 N1–N4 复验

- **N1**：日报已生成同日快照后运行周任务，`created=0`、`existing>=1`，历史快照逐条不变，报告复用该快照；文档明确 20:00/投递后调度与下一自然日语义。
- **N2**：真实 Chrome 访问 `/ui/today?day=2026-10-21`，页面隐藏 `day=2026-10-21`，点击“生成该日日报”后 URL 保持 `?day=2026-10-21`；今日页面仍可完成 HTMX 阅读闭环并归档。官方回归通过。
- **N3**：文档统一为 `queued→pushed` 时写 `paper.pushed` + `payload.trigger=user_read`；已 pushed 不重复写，无独立 `user_read` 事件，与代码一致。
- **N4**：`safe_url` 拒绝 `https://u:pw@host`、`https://:pw@host`、`https://user:@host`，普通 http(s) URL 不受影响；新增测试覆盖空用户名/空密码。

### 9.5 非阻断限制

- 周任务会为全部历史 `PUSHED` 未读追加当日快照；长期积压时快照量随未读存量增长，窗口化留给 P7/P8。
- 真实 SMTP 厂商 TLS/送达质量、真实用户首日报内容审阅仍未验收；SMTP I/O 失败仍按设计记 `uncertain`。
- P1 F5/F3 债务、P2–P5 tag 与 P6 tag 仍待发布/维护流程；P6 未发布 Alpha。
- 返工 diff 18 文件/521 行（含文档），超单卡指引；原因为 F1/F2 跨模块修复、N1–N4、回归与文档同交，未夹带 P7。

### 9.6 最终判定

**F1/F2 返工与 N1–N4 处理通过独立复验；P6 可以关闭，P7 可以开始，P6 tag 仍待发布流程。**
