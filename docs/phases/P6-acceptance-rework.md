# P6 返工验证记录

> **对象**：P6 独立验收 F1/F2 阻断缺陷与 N1–N4 非阻断问题
> **基线**：独立验收 commit `57e6db8`；返工起点 `5684be0`（已合并 `4650e25` + 验收文档）
> **日期**：2026-10-22
> **性质**：实现方返工与自检记录，**不是独立验收结论**。独立复验仍须由未参与实现的
> 验收者执行；在复验通过前 P6 不关闭、P7 不开始、P6 tag 不创建。

## 1. 结论摘要

| 编号 | 级别 | 处理 | 回归 |
|---|---|---|---|
| F1 周任务漏 PUSHED | 阻断 | 已修复：周候选集合加入 PUSHED，报告查询同步；到期 snooze 同时恢复 | `test_weekly_includes_delivered_unread_candidates`、`test_weekly_resumes_due_snoozed_candidate` |
| F2 二次 preflight 假 uncertain | 阻断 | 已修复：复核与 I/O 分开捕获；复核失败记 `failed` 且说明未尝试投递 | `test_second_preflight_failure_is_not_uncertain_and_never_sends` |
| N1 同日快照语义 | 中 | 已明确并测试：同日快照不重写；建议周任务在截止后、日报前运行 | `test_weekly_same_day_snapshot_is_not_rewritten`；delivery-cli/ADR0016 |
| N2 日期表单 | 低 | 已修复：生成表单携带所选日期并回到该日期 | `test_today_generate_form_uses_selected_day` |
| N3 user_read 命名 | 低 | 已澄清文档：写 `paper.pushed` + `payload.trigger=user_read`，无独立事件 | delivery-cli/ADR0016 |
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

- N1：同日快照保持不可变。周任务在日报之后运行时复用该快照并在报告中列出；文档建议
  在 update_weekday 00:00 截止后、当日日报前运行，否则下一自然日快照生效。
  `test_weekly_same_day_snapshot_is_not_rewritten` 断言 `created=0`、`existing>=1`、
  历史快照逐条相等。
- N2：`/ui/today?day=...` 无日报页面的表单带 hidden `day`；`POST /ui/digests` 使用该
  日期，非今日时跳回 `/ui/today?day=...`。保留大小限制与非法日期拒绝。
- N3：文档统一事件命名，代码未新增事件类型。
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
最终提交只在本记录追加 clone 证据，不改变代码或测试。

## 8. 回滚

返工不新增迁移、依赖或状态值。回退代码即可恢复旧行为；已产生的 PUSHED 状态、日报、
评级、归档与投递记录不回滚。F2 只改变失败预约的状态记录；若已产生 `failed` 预约，
回旧代码前先核对是否实际发送过，SMTP `uncertain` 仍必须人工核对。
