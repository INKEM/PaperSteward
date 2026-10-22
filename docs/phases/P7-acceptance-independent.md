# P7 独立验收报告（特殊桶、自定义排序与权重治理）

> 状态：**Not Accepted（2026-10-22）**。本地 S1–S6 实现与阶段门通过，但独立验收发现
> 3 个阻断问题和 4 个非阻断问题，需返工后复验；P7 tag、Beta 发布和 P8 开始均保持阻塞。
> 验收对象：`6a877e9`（`feat/p7-s6-e2e`，PR #10，base `origin/main` `3026696`）；
> head `0014`；验收日期 2026-10-22（Asia/Shanghai）。验收只读取/运行，不修改实现代码；
> 临时探针在取证后删除。

## 1. 结论

P7 的本地实现覆盖了 S1–S6：特殊桶提名/人工决定、档位/配额/轮换、只读特殊桶视图、
自定义排序视图、全局权重预览/确认/按日激活，以及最终真实 Uvicorn/CLI e2e 和特殊桶
提升回主题桶。官方本地门和独立复跑均通过，未发现迁移、CI、fresh-clone 或 Docker smoke
层面的失败。

但独立验收确认了三个契约级缺陷：

1. **F1（阻断）** 权重治理可预览并激活 `gates`、`stability`、`dynamic`/`quasi_dynamic`
   内部参数等非权重字段；预览只重排六组件，未覆盖这些字段的候选集、EMA、日报和 U 计算
   影响，违反 `docs/config-governance.md` “R 门槛/特殊桶/EMA/日快照不由本流程修改”。
2. **F2（阻断）** 在“确认成功”到“生效日”之间处理的文献只得到旧配置版本的静态快照；
   生效日队列以 `static_version_mismatch` 排除它们。默认生效日为次日，因此整个确认后
   窗口内处理的文献会从生效日队列消失，且 `classified` 状态不能通过重跑 process 修复。
3. **F3（阻断）** 拒绝特殊桶提名后无法重新提名。S1 契约写明“被拒绝后可重新 process
   产生新提名”，但 process 只接受 `screened/enriched/summarized`，且
   `SpecialNominationRepository.upsert` 对已拒绝的 `paper_id+kind` 永远返回旧拒绝行，
   不会产生新的 `pending`。

因此 **P7 不通过独立验收**，不能创建 P7 tag，不能宣称 Beta，也不能开始 P8。返工后
必须重新跑完整阶段门和本报告第 7 节复验清单。

## 2. 验收范围与方法

- 只读审查 `6a877e9` 的 S1–S6 源码、迁移 `0011`–`0014`、CLI/API/UI、文档和测试；
- 复跑完整本地门、独立 fresh clone（Python 3.14.7）、迁移 up/down/up、demo、
  `make secret-scan`、Docker smoke 和 CI；
- 针对特殊桶提名/决定、档位/轮换、只读视图、视图重排、权重预览/确认、生效日队列、
  静态快照和审计字段编写临时探针；探针不修改实现，取证后删除；
- 未使用真实 LLM、真实网络源或生产数据库。

## 3. 阶段门与发布证据

| 检查 | 结果 |
|---|---|
| 本地 `make check` | **1010 passed / 4 deselected**；ruff **268** 文件；strict mypy **131** 源文件 |
| 本地 `make test-integration` | **331 passed** |
| 本地 `make e2e` | **4 passed**（G1→G4→P4→P5→P6→P7 真实 Uvicorn/CLI） |
| 本地 `make migrate-check` | 0010 ↔ 0014 upgrade/downgrade/upgrade 通过 |
| 本地 `make demo` | 临时库 migration/seed/API `/health`、`/papers` 通过 |
| `make secret-scan` / `git diff --check` | 通过 |
| 全新 clone（Python **3.14.7**） | `make check` **1010 passed / 4 deselected**；integration **331**；e2e **4**；migrate/demo/secret 通过；工作树干净 |
| CI（PR #10，`6a877e9`） | run [39800002203](https://github.com/INKEM/PaperSteward/actions/runs/39800002203) 的 py3.11、py3.12、docker compose smoke 全部 success |
| Docker smoke | CI docker compose smoke 成功（run 39800002203）；独立未另起本地镜像验收 |
| 真实浏览器 | 未做独立 Chrome 点击；P7 e2e 已用真实 Uvicorn/CLI 验证 `/ui/shelves`、`/ui/views` 的 HTML/CSP/HTMX 断言 |

本地门全绿只能证明实现与其现有测试一致，不能覆盖下面三个契约缺口。

## 4. 阻断问题

### F1（阻断）权重治理可以激活非权重字段，预览无法覆盖其影响

**契约依据**：`docs/config-governance.md` 的激活语义明确写：

> 预览使用当前合格队列的六组件分数（R/A/E/F/H/U）和新展开权重重排，不扩大候选集；
> R 门槛、特殊桶排除、EMA/日快照规则不由本流程修改。

**实际行为**：

- `scoring/governance.py::apply_overrides` 接受任意 JSON 点路径；
- `preview_from_overrides` / `preview_from_config` 对完整 `ScoringConfig` 只做整体校验；
- `preview_weight_change` 计算 impact 时只使用 `component_weights` 和当前队列的六组件，
  不计算 `gates`、`stability`、`dynamic.half_life_days`、`quasi_dynamic.tag_weight/
  author_weight/venue_weight` 等字段的未来影响；
- `confirm_weight_change` 随后把完整 proposed 配置写入新版本并激活。

独立探针（临时测试，已删除）：

```text
preview_from_overrides(session, {"gates.max_daily_push": 99}, ...)
→ status=previewed（未拒绝）
confirm_weight_change(...)
→ status=confirmed, static_recomputed=3
ConfigRepository.get("scoring", "1.0.1").value_json["gates"]["max_daily_push"] == 99
```

`max_daily_push` 只是最容易演示的字段；同样的路径可改 `gates.max_per_primary_tag`、
`gates.deep_cap`/`skim_cap`、`stability.rank_ema`/`max_daily_delta`、
`dynamic.half_life_days`、`quasi_dynamic.update_weekday`/`shrinkage_lambda` 等。
这些会影响 P6 日报容量、P5 EMA/日快照或 U 的计算，但都不会出现在预览的六组件重排
中；用户会被诱导以为只改变了权重。

**影响**：全局治理可绕过“预览 Top-N 影响”的安全边界，改变候选集/digest/EMA 等行为；
preview hash 只覆盖六组件排序，无法证明实际生效结果。

**修复方向**：为 P7-S5 建立显式 allowlist，只接受可以线性作用于当前六组件的权重
（至少 `static.weight`、`static.relevance/authority/extra_value`、`dynamic.weight`、
`dynamic.freshness/hunger`、`quasi_dynamic.weight`）；对 `gates`、`stability`、
`dynamic`/`quasi_dynamic` 内部参数在 `preview` 入口返回 400/422。若确实要治理完整配置，
必须把候选集、EMA、日报和 U 的影响全部纳入 preview/impact，而不是只重排六组件。

### F2（阻断）确认后、生效日前处理的文献在生效日被旧静态快照挡住

**契约依据**：`docs/config-governance.md` 写“生效日之前的队列/静态快照继续用旧配置；
生效日及之后用新配置”，且确认时只对**已有**静态快照追加生效日新版本。

**实际行为**：

- `confirm_weight_change` 对确认时已存在的每篇非归档文献追加 `effective_on` 的新版本
  静态快照；
- 此后、`effective_on` 之前处理的文献，`process_paper` 通过
  `bound_context(day=now.date())` 仍选择旧配置，静态快照日期是当天、`config_version`
  仍是旧版本；
- 生效日 `create_snapshots`/`read_queue` 使用新 `scoring_version`，
  `_eligibility` 以 `static_version_mismatch` 排除该文献；
- 该文献状态已是 `classified`，process 只接受 `screened/enriched/summarized`，不能
  重跑修复；默认生效日是次日，因此确认后的整个白天都可能落入该窗口。

独立探针（临时测试，已删除）：在确认生效日为 `DAY+1` 后，向数据库加入一篇
`config_version=1.0.0` 静态快照的 `late` 文献（等价于该窗口内 process 的写入），
生成 `DAY+1` 日快照；`/queue?date=DAY+1` 不含 `late`。同一问题也影响确认后、
生效日前 `shelf promote` 提升的文献：它按当天旧配置追加静态快照，可能在生效日之后
反而留下一个旧版本“未来快照”。

**影响**：确认后新处理/提升的文献在生效日不进入 P；由于无法重跑 process，会一直缺失到
下一次配置激活或人工数据处理。这与“生效日及之后用新配置”直接冲突。

**修复方向（至少一种）**：处理/提升在发现已确认的 pending activation 时，为
`effective_on` 写入新版本静态快照；或让生效日队列对旧版本静态快照做一次受控重算；
或在确认时登记未来窗口并阻止/重放该窗口的 process。必须新增“确认后再处理，生效日
仍入队”和“确认后再提升，生效日仍入队”的回归测试。

### F3（阻断）拒绝后不能重新提名，与 S1 契约不符

**契约依据**：`docs/phases/P7-shelves.md` S1 契约写明：

> reject 不改变文献状态、主标签或主题归属；被拒绝后可重新 process 产生新提名。

**实际行为**：

- reject 只把 `special_nomination` 行改为 `rejected`，文献仍是 `classified`；
- `process_paper` 的允许输入只有 `screened/enriched/summarized`，classified 文献直接
  返回“只处理 screened/enriched/summarized 文献”；
- 即使绕过状态检查直接把 P4 结果再次交给 `persist_nominations`，
  `SpecialNominationRepository.upsert` 对同一 `paper_id+kind` 的非 pending 行直接返回
  旧拒绝行，不会插入新的 pending；
- 没有 CLI/API/迁移路径可以 reset/reopen 该拒绝。

独立探针（临时测试，已删除）：process 产生 review+distant 提名，reject review 后调用
`persist_nominations`，review 仍是 rejected；对 classified 文献调用 process 抛冲突；
最终没有任何 pending review 行可供重新接受。

**影响**：用户拒绝一个特殊桶后无法在同一篇文献上重新考虑该桶，与 S1 契约和“可重新
process”工作流不符；这是不可恢复的产品行为，而不是测试缺口。

**修复方向**：提供受控的 re-nominate/reset（只允许 rejected 行回到 pending，或追加
新的提名 revision），并在 process/review 路径暴露；若产品决定拒绝不可逆，必须修改
S1 契约和验收清单，明确这是已知、不可恢复的限制。P7 当前实现选择了前者却未实现。

## 5. 非阻断问题

### N1 `GET /config/scoring/changes` 的 `total` 是当前页大小

当存在 2 条变更、`limit=1` 时，响应为 `items` 1 条、`total=1`，不是全量 2。CLI
`config changes` 同样只把当前页长度当 total。这违反列表信封 `items/total/limit/offset`
的语义；应返回全量计数。独立探针已复现。

### N2 `shelf tier --by` 的审核人没有持久化

`override_special_tier` 要求 `reviewed_by` 且 CLI 强制 `--by`，但
`SpecialTierSnapshot` 没有该字段，override 也不写事件或 `breakdown_json`；独立探针
确认 `asdict(snapshot)` 中不存在 `reviewed_by`/`audit-user`。`assigned_by=user` 和
`note` 只能证明“有人改过”，不能审计是谁。应增加字段/迁移或事件；若不需要审核人，
应删除 CLI 的必填 `--by` 并同步文档。

### N3 多个 P7 日期入口使用 UTC 日期，和 runtime timezone 不一致

`process` 调用 `bound_context(..., day=now.date())`，`explain_score` 的缺省 day 用
`now.date()`，`promote_to_topic` 用 `timestamp.date()` 选择配置和静态快照日期，
`confirm_weight_change` 用 `timestamp.date()` 判断生效日是否已过。P2/P6 的本地日期
使用 runtime timezone；在 Asia/Shanghai 00:00–08:00，UTC 仍是前一天，可能选错
activation、把生效日判断错或生成旧配置静态快照。应统一使用
`now.astimezone(runtime.timezone).date()`（或显式传入目标日期），并增加边界测试。

### N4 权重预览的 `new_rank` 使用 priority，而队列实际按 rank_score/EMA 排序

`_build_impact` 用 `new_priority` 作为 `rank_key(rank_score=...)` 排序，但 P5 队列
按 `rank_score`（含 `rank_ema`、`max_daily_delta` 和 U/偏好）排序。`old_rank` 是真实
队列名次，`new_rank` 是 priority 名次，二者不是同一指标；确认后实际队列可能和预览
不一致。应使用 `compute_priority`（带 previous rank）计算实际新 rank_score，或把
字段明确命名为 priority rank 并同步文档/UI。

## 6. 已确认无问题的部分

- 三个特殊桶目录/提名持久化/幂等重跑、待审提名排除 P、accept/reject 的 Filing/状态/
  事件/越权/重复决策路径，官方 331 集成测试通过；独立抽查认可。
- 档位 rubric 五维权重/阈值、V 只进入审计接口、不进入特殊桶展示；轮换计划按
  review→classic→distant、每日最多 2 槽、配额上限/空桶/低档跳过；官方测试通过。
- 特殊桶 API/CLI/页面不返回 V/P/rank，`excluded_from_priority=true`；空桶不占位；
  archived/rejected 不展示；独立抽查认可。
- 自定义视图 R/A/E/F/H/U 权重归一化、预设、保存/加载/删除、只读预览，不写全局配置/
  日快照；官方测试和 e2e 通过。
- 权重预览/确认的版本、preview hash、确认人、`config.activated`、静态重算、生效日
  选择的 happy path；e2e 和官方测试通过。
- 迁移 0011–0014 upgrade/downgrade/upgrade、Docker smoke、CI、fresh clone 均通过。

## 7. 返工与复验要求

返工必须包含实现、迁移（如新增 reviewer 字段）和同交回归测试：

1. **F1**：限制/拒绝非权重配置字段；新增 400/422 回归；若保留完整配置激活，必须把
   candidate set、EMA、digest 和 U 的全部影响纳入 preview/impact。
2. **F2**：保证确认后、生效日前处理的文献和提升文献在生效日仍以新配置进入 P；新增
   “确认−处理−生效”“确认−提升−生效”两个回归，并覆盖 process 不能重跑的 stranded 状态。
3. **F3**：实现受控重新提名/重置或修改契约并明确不可逆；新增 reject→re-nominate→accept
   的回归（若选择不可逆，则新增文档和验收清单的显式变更）。
4. **N1–N4**：修复 total 计数、记录 tier reviewer、统一日期时区、修正 preview rank
   指标；至少为 N1/N2 添加回归，N3/N4 添加边界/一致性测试。
5. 重新跑完整阶段门、fresh clone、migrate/demo/e2e/secret、Docker smoke 和 CI；更新
   P7 执行卡、验收记录、handoff 和 CHANGELOG；再次独立复验前不得创建 P7 tag、不得宣称
   Beta、不得开始 P8。

## 8. 发布状态与边界

- P7 tag **未创建**；Beta **未发布**；P8 **不得开始**。
- P6 仍为已验收/已关闭；P4/P5/P6 验收记录不受本报告影响。
- 本报告只记录独立验收结论，不修改实现代码；临时探针在提交前删除。
- 已知且**已在 P7 范围外**的能力：特殊桶日报槽位/实际推送、特殊桶阅读/归档状态、
  主题桶→特殊桶独立 demote、提升 API/UI（仅 CLI）、低 R 提升、周回顾清理建议、
  LLM 质量评分；这些不应被误读为本报告新增的阻断项。
