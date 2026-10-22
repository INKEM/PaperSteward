# P7 返工验证记录

> **对象**：P7 独立验收 F1/F2/F3 阻断缺陷与 N1–N4 非阻断问题
> **基线**：独立验收 `docs/phases/P7-acceptance-independent.md`（对象 `6a877e9`，
> 文档提交 `f70e0a0`）；返工分支 `fix/p7-acceptance-rework`
> **日期**：2026-10-22（Asia/Shanghai）
> **性质**：实现方返工与自检记录。**独立复验尚未执行**，P7 仍为 Not Accepted；
> P7 tag/Beta/P8 继续阻塞。

## 1. 结论摘要

| 编号 | 级别 | 处理 | 回归 |
|---|---|---|---|
| F1 非权重字段可激活 | 阻断 | 已修复：治理 allowlist 仅 8 个线性权重；完整 config 也做非权重字段 diff；400 拒绝 | `test_preview_rejects_invalid_overrides_dates_and_versions`、`test_apply_overrides_*` |
| F2 确认后窗口文献丢失 | 阻断 | 已修复：process/promote 为 pending activation 补写生效日新版本静态快照；不重跑 LLM | `test_late_process_after_confirmation_gets_effective_snapshot`、`test_late_promotion_after_confirmation_gets_effective_snapshot` |
| F3 拒绝后不能重提名 | 阻断 | 已修复：`shelf reopen` 把 rejected 受控回到 pending，追加审计事件；不重跑 LLM | `test_rejected_nomination_can_be_reopened_and_accepted`、CLI reopen 回归 |
| N1 changes total 是页大小 | 中 | 已修复：仓储 count + API/CLI 全量 total | `test_changes_total_is_not_page_size`、CLI `--limit 1` |
| N2 tier reviewer 未持久化 | 中 | 已修复：迁移 `0015` 增加 `reviewed_by`；override 写入，CLI/详情展示 | 既有 tier 测试新增断言 |
| N3 UTC 日期不一致 | 中 | 已修复：process/explain/promote/confirm 用 runtime timezone 本地日；local_day 不放入 binding hash（避免周任务 ID 漂移） | `test_bound_context_local_day_uses_runtime_timezone`、全量周任务回归 |
| N4 预览名次用 priority | 中 | 已修复：用 `compute_priority(reset=True)` 的 `rank_score` 排序，并输出 `new_rank_score` | 治理集成预览断言 |

## 2. F1：只允许线性权重字段

- `scoring/governance.py` 新增 `WEIGHT_PATHS`：
  `static.weight/relevance/authority/extra_value`、`dynamic.weight/freshness/hunger`、
  `quasi_dynamic.weight`。
- `apply_overrides` 对任何其他路径直接返回 400；`preview_weight_change` /
  `confirm_weight_change` 再对完整 `ScoringConfig` 做非权重字段 diff（忽略 version），
  防止用 `--file` 或完整 config 绕过。
- `gates`、`stability`、`dynamic.half_life_days`、`quasi_dynamic` 子权重等不再能被
  preview/activate；错误信息明确列出允许字段。
- 回归：4 个非权重 override（gates/max_daily_push、stability.rank_ema、
  dynamic.half_life_days.fast、quasi_dynamic.tag_weight）均 400；完整 config 改
  `gates.max_daily_push=99` 也 400。

## 3. F2：确认−处理/提升−生效窗口

确认时只对当时已存在的静态快照追加生效日快照；返工新增
`scoring/activation.py::append_future_activation_snapshots`：

- 查询 `effective_on > 当前静态快照日期` 的 confirmed changes；
- 对每个 pending activation，按新配置静态子权重从最新 R/A/E 确定性重算 S；
- 追加 `effective_on` 静态快照，`input_hash` 绑定 activation/date，不调用 LLM；
- 跳过已存在同日或更新静态快照的情况。

`process_paper` 在写完当日静态快照后调用；`promote_to_topic` 在复用/追加静态快照后
调用。生效日 `create_snapshots`/`read_queue` 因而能读到新版本快照，不再
`static_version_mismatch`。

回归：

1. 确认（effective=DAY+1）后，用 topic-only fixture 处理一篇新文献；断言其当日
   快照仍是 1.0.0，同时存在 DAY+1 的 1.0.1 快照；DAY+1 日快照包含该文献。
2. 确认后新增一个 shelved 特殊文献（仅 DAY 旧快照），提升回 T02；断言自动补出
   DAY+1 的 1.0.1 快照，状态 classified。

## 4. F3：受控重新打开被拒绝提名

- 新增 `EventType.SPECIAL_NOMINATION_REOPENED`。
- `SpecialNominationRepository.reopen` 仅允许 rejected → pending，清空本次
  `decided_at/decided_by/decision_note`。
- `pipeline/shelf.py::reopen_nomination` 要求文献仍 classified；可选择 kind
  （缺省按 review>classic>distant 选一个 rejected）；追加事件记录原决定人/时间/备注；
  不重跑 process/LLM，不改主标签、Filing 或状态。
- CLI：`papersteward shelf reopen <paper> [--kind] --by --note [--json]`。
- 回归：process → reject review → reopen → pending 列表出现 → accept；无 rejected
  可重开时 409；CLI JSON 输出 `pending`。S1 文档已从“重新 process”改为
  `shelf reopen`。

## 5. N1–N4

- **N1**：`ScoringChangeRepository.count()`；API 列表 `total` 与 CLI
  `config changes --limit 1` 均返回全量计数，`items` 仍按页。
- **N2**：迁移 `0015_special_tier_reviewer` 为 `special_tier_snapshot` 增加
  `reviewed_by`（nullable）；`override_special_tier` 写入审核人；`shelf tier --json`
  输出 `reviewed_by`，`shelf show --json` 输出 `tier_reviewed_by`。历史/自动档位为
  null，避免回填伪造审核人。
- **N3**：新增 `pipeline/context.py::runtime_local_day`，按绑定 runtime timezone 取
  本地日；process CLI 用该日作为 `score_date`；`explain_score`、`promote_to_topic`、
  `confirm_weight_change` 同步。**不把 `local_day` 放进 binding dict**，否则周任务
  run_id 会随日变化（返工中发现并修复的回归）。
- **N4**：`_build_impact` 用新 S/D/U 与 `compute_priority(reset=True)` 计算
  `new_rank_score`，按 `rank_key(rank_score=...)` 排序；保留 `new_priority` 只是 P
  解释值。新静态 input_hash 触发 reset，符合生效日实际快照。

## 6. 阶段门证据

| 检查 | 结果 |
|---|---|
| `make check` | **1015 passed / 4 deselected**；ruff **270** 文件；strict mypy **132** 源文件 |
| `make test-integration` | **336 passed** |
| `make migrate-check` | 0014 ↔ 0015 upgrade/downgrade/upgrade 通过 |
| `make e2e` | **4 passed**（返工后复跑） |
| `make demo` | 临时库 migration/seed/API health/papers 通过 |
| `make secret-scan` / `git diff --check` | 通过 |

本记录生成时点尚未执行：全新 clone、Docker build/run、远程 CI、独立复验。它们必须在
独立复验/发布前补做，不能用本轮本地门替代。

## 7. 文件与规模

- 实现：`scoring/governance.py`、`scoring/activation.py`（新）、`pipeline/context.py`、
  `pipeline/process.py`、`pipeline/shelf.py`、`scoring/queue.py`、`cli/processing.py`、
  `cli/shelf.py`、`cli/config.py`、`api/governance.py`、`storage/repositories.py`、
  实体/模型/mapper、迁移 0015、核心枚举。
- 测试：治理/特殊桶/档位/提升/周任务回归，合计 +5 项。
- 文档：P7 执行卡/验收/返工记录、shelves/config-governance、README、CHANGELOG、
  AGENTS、delivery-cli。
- 规模超过单卡 8 文件/400 行指引；原因是三项阻断跨治理/处理/提升/审核/日期与迁移，
  必须与回归同交，未夹带 P8 或其他重构。

## 8. 回滚

- 迁移 `0015` 可单独 downgrade 删除 `reviewed_by`；已写入的审核人丢失，但档位/理由
  保留。
- F1/F3/N1/N3/N4 只改代码，回滚代码即可；已产生的 pending activation、future
  static snapshots 与 reopen 事件不回滚，回旧代码前应导出/备份事件。
- F2 回滚后，确认后窗口文献可能再次在生效日缺失；不要只回滚代码而保留新配置激活。
- 返工不创建 P7 tag；不改变 P6/P4/P5 验收结论。

## 9. 独立复验清单

复验必须至少：

1. 复现 F1：`gates.max_daily_push`/`stability`/half-life/quasi 子权重预览均 400；
   完整 config 同样拒绝。
2. 复现 F2：确认→处理→生效、确认→提升→生效均进入生效日队列；检查当日旧快照
   与生效日新快照并存。
3. 复现 F3：reject→reopen→accept，pending 列表/审计事件正确；重复/越权 reopen 拒绝。
4. N1：两条变更、`limit=1` 时 total=2；N2：tier override 的 reviewer 持久化；
   N3：Asia/Shanghai 00:00–08:00 边界本地日；N4：preview 新名次基于 rank_score。
5. 复跑完整阶段门、fresh clone、Docker smoke、CI；更新本记录 §10 后才能
   Accepted、创建 P7 tag 或开始 P8。

## 10. 独立复验结论

**待执行。** 当前 P7 仍为 **Not Accepted**；P7 tag/Beta/P8 阻塞。
