# P7 本地阶段验收（特殊桶、自定义排序与权重治理）

> 状态：**P7 独立验收 Not Accepted（2026-10-22）；F1–F3/N1–N4 两轮返工已完成，
> 待再次独立复验；P7 tag/Beta 未发布。**
> 证据日期：2026-10-22。范围：S1–S6 + 返工，head `0015`，无新增 Python 依赖。
> 独立结论见 [P7-acceptance-independent.md](P7-acceptance-independent.md)；
> 返工证据见 [P7-acceptance-rework.md](P7-acceptance-rework.md)。

## 结论

P7 的六张小阶段已完成本地实现：特殊桶提名/档位/配额/轮换、只读特殊桶视图、
自定义排序视图、全局权重预览/确认/版本/按日激活，以及最终真实 CLI/Uvicorn e2e 和
特殊桶提升回主题桶。

**2026-10-22 独立验收结论为 Not Accepted**：本地门全部通过，但独立验收发现
权重治理可激活非权重字段、确认后至生效日之间处理的文献在生效日被旧静态快照排除、
拒绝提名后无法重新提名三个阻断问题，另有多项非阻断问题。详见
[P7-acceptance-independent.md](P7-acceptance-independent.md)。

**返工后独立复验仍为 Not Accepted**：F1–F3 已闭环，N1/N2 已修复；但 N4 的
`_build_impact` 层权重重复计算使 preview `new_priority/new_rank_score` 为 43.8，
生效日实际队列为 71.7；N3 的 `config preview` 缺省日期仍用 UTC。详见
[P7-acceptance-rework.md §10](P7-acceptance-rework.md)。**这是本地验收，不是
独立验收，也不是 Beta 发布**；再次复验前不得创建 P7 tag。

## 本地门证据（独立复跑）

| 门 | 结果 |
|---|---|
| `make check` | **1015 passed / 4 deselected**；ruff **270** 文件、strict mypy **132** 源文件 |
| `make test-integration` | **336 passed** |
| `make migrate-check` | 0014 ↔ 0015 upgrade/downgrade/upgrade 通过 |
| `make e2e` | **4 passed**（G1→G4→P4→P5→P6→P7 真实链路） |
| `make demo` | 迁移/seed/API health/papers 通过 |
| `make secret-scan` | OK |
| 外部网络/真实 LLM | 未使用；全部 FakeLLM/cassette/临时 SQLite |
| 独立 fresh clone（Python 3.14.7） | 返工后 check **1015** / integration **336** / e2e **4** / migrate / demo / secret 通过，工作树干净 |
| CI（PR #10，原始 `6a877e9`） | run 39800002203 的 py3.11、py3.12、docker compose smoke 全绿；**不含返工 `91d9255`** |

## P7 验收清单

> 下列是本地实现检查项，不代表独立验收通过；独立验收阻断项以独立报告和
> 返工复验 §10 为准。

- [x] 三个特殊桶可浏览，均有档位、理由、证据；空桶不占位。
- [x] 远邻领域必须有四字段桥接假设；经典/奠基必须有经典依据；跨层综述覆盖层级。
- [x] 特殊桶文献不出现在主队列，不返回 V/P/rank；待审提名不阻塞/污染 P。
- [x] 特殊桶可提升回主标签主题桶并重新纳入 P；历史特殊档位/P 快照不改写。
- [x] 自定义视图支持 R/A/E/F/H/U 组件权重、内置预设、保存/预览/删除，只改当前排序。
- [x] 全局权重变更先预览 Top-N 影响，显式确认后提升版本并按生效日激活。
- [x] 预览不写配置/静态快照；未确认不激活；视图不写全局配置/激活版本。
- [x] 失败路径：无效权重/证据/档位、重复确认、过期 preview hash、重复提升、低 R 提升、
      非法日期/版本均返回可操作错误。
- [x] 迁移可 downgrade；回滚边界记录在 `docs/shelves.md`、`docs/views.md`、
      `docs/config-governance.md` 与本文件。

## e2e 覆盖

真实 Uvicorn + 真实 `papersteward` 子进程、固定时钟、临时 SQLite：

1. G1–G4、ingest、process、P5 队列/P6 日报/归档；
2. P7 五篇合成论文：2 篇主题、3 篇特殊；9 条特殊提名，接受 review/distant/classic；
3. `/shelves`、`/ui/shelves`、桥接假设/经典依据、档位/轮换计划；
4. 日快照只含主题候选；`view preview --weights R:100` 重排、`view save`、`/ui/views`；
5. `shelf promote` 提升 review 文献回 T02 主题桶；
6. `config preview/confirm`，旧日队列用旧配置，生效日快照（独立时钟）用新配置并包含
   提升后的文献。

## 已知限制 / 不宣称

- P7 **独立验收 Not Accepted**，需再次返工复验；未创建 P7 tag，不宣称 Alpha/Beta；P8 不得开始。
- 原始阻断 F1–F3 已闭环；N1/N2 已修复。返工后剩余：N4 层权重重复计算（阻断回归）、
  N3 config preview 缺省 UTC 日期（非阻断残留）、N2 API/页面审核人展示（非阻断）。
- 主题桶→特殊桶的独立 demote 命令未提供；当前仅能在 classified 阶段通过
  `shelf accept` 进入特殊桶。已入队主题文献的强制降级留待后续卡。
- 提升只有 CLI，无独立 API/页面；低 R 特殊文献不能提升回主队列；提升不重新调用 LLM，
  只按当前静态权重复用/重算 R/A/E。
- 特殊桶轮换配额/用量仍由调用方传入已用配额；没有后台定时器自动发送。
- 全局权重没有自动调参/自动确认/权重滑块；生效日默认次日，同日快照不可改写。

## 回滚

- 代码/数据：`alembic downgrade 0013` 删除 `scoring_change` 与权重变更记录；特殊桶/
  视图表分别可降到 `0010`/`0012`。已保存配置版本/静态快照不自动回退。
- 提升：不删除历史；如需撤回，重新走人工审核/后续降级流程。
- 发布：P7 tag 前必须完成 F1–F3 返工、复跑完整阶段门并再次独立验收；不得以本文件替代独立验收。
