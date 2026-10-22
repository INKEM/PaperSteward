# P7 特殊桶、自定义排序与权重治理

> 状态：**P7-S1 实现（2026-10-22）；S2–S6 待开始。**
> 依据：开发计划 §3 P7；设计 §5.4、§8、§10.10；P6 handoff、P5 队列契约与 ADR0015。
> 前置：P6 已关闭；特殊桶不参与 P、主标签保留、Filing 唯一等 P4/P5 契约不重开。

## 执行卡

每卡执行 `make check`、相关 unit/contract/integration、`make secret-scan`；阶段结束
跑完整阶段门。前卡本地提交后下一卡从该提交建立 stacked 分支；无未通过检查不开始下一卡。

| 卡 | 目标、输入→输出与文件 | 契约、失败路径、验收/演示 | 范围外与回滚 |
|---|---|---|---|
| S1 数据模型/提名审核 | P4 提名结果→`special_nomination` 0011、`core` 实体、仓储、`pipeline/shelf.py`、`shelf` CLI | 三类桶幂等目录；提名持久化/幂等重跑；待审列表；accept 需 classified+主题归属，写 special filing→shelved；reject 保留主题桶；待审提名排除 P；重复/越权决策拒绝 | 无档位/配额/轮换/展示/API；回退代码可降 0010，已归入特殊桶的文献保留归属 |
| S2 档位/配额/轮换 | S1 提名+建桶→rubric、V 分档、配额与轮换计划、单元/表驱动测试 | 8.2–8.4 权重与阈值（高≥75/中50–74/低<50）；配额是上限不凑数；桶内不跨桶排名；无 P | 无展示/推送绑定；回退删除档位快照，不改已有归属 |
| S3 特殊桶视图/展示 | S2 档位→只读 shelf API/CLI/页面与解释 | 档位+理由+证据；远邻 bridge memo、综述覆盖层、经典奠基证据；空桶不占位；读取不写库 | 无日报推送改造；回退页面/API 保留数据 |
| S4 自定义视图/预设/保存 | P5 队列解释→自定义视图契约、预设模板、保存/加载 | 勾选 R/A/E/F/H/U、权重归一化、只影响当前排序；视图不写全局配置；无效权重拒绝 | 无全局权重激活；回退删除视图行 |
| S5 权重影响预览/版本/确认 | 现有 config import/version→preview、Top-N 影响、确认后激活 | 预览 golden；未确认不激活；版本/审计不丢；配置与视图隔离 | 无自动调参；回退保留已确认版本，不静默回滚 |
| S6 特殊桶+视图 e2e | S1–S5→真实 Uvicorn/CLI 链路 | G4→process→shelf review→accept→特殊无 P→视图重排；配额/档位展示；失败路径 | 不扩采集/评分算法；回退保留快照/归属 |

## S1 契约（2026-10-22）

- 迁移 `0011` 新增 `special_nomination`：每篇文献每个特殊类型一行，保存档位前置证据
  （逐字证据、review layers、classic basis、bridge memo）、置信度、门禁原因与 LLM 溯源。
- 状态只有 `pending / accepted / rejected`；决策记录 `decided_at/decided_by/note`。
  已决策提名重跑 process 不会被静默覆盖；待审提名重跑只刷新证据与时间，不重复建行。
- 三个特殊桶由固定 ID 幂等补齐：`b-special-review`（跨层综述）、`b-special-distant`
  （远邻领域）、`b-special-classic`（经典/奠基）。只补目录，不设置配额/档位。
- `process` 只持久化提名，不自动归入特殊桶；提名不再复制到 `review_item`，由
  `papersteward shelf review` 专门审核。证据不足的提名仍可在人工确认后 `--force` 接受，
  但必须记录决策人和备注。
- accept 只允许 `classified`、已有唯一主标签与主题归属且无待决 review 的文献：
  替换 Filing 为主题桶→特殊桶，状态转 `shelved`，追加 `paper.bucket_moved` 与
  `paper.shelved`；同一文献其他待审提名自动 rejected（`superseded`）。
- reject 不改变文献状态、主标签或主题归属；被拒绝后可重新 process 产生新提名。
- 待审提名在每日快照/队列中以 `pending_special_nomination` 排除，避免决定前进入 P。
  已归入特殊桶后沿用既有 `special_bucket` 排除；历史 P 快照保留但不参与排序。
- 本卡不实现档位 V、配额、轮换、特殊桶只读视图/API、日报实际特殊槽位、特殊↔主题
  提升/降级、自定义视图或全局权重治理。

## S1 执行记录（2026-10-22）

- 已完成：迁移 `0011`、领域实体/仓储、三类桶幂等目录、process 提名持久化、
  `shelf review|accept|reject` CLI、待审提名排除 P/explain、决策事件与回归测试。
- 新增 6 项集成回归：process 只落提名不进普通 review、幂等重跑、接受写 special
  Filing/`shelved`/双事件并作废其他待审、拒绝保留主题归属、force+note 门禁、
  CLI 列表/拒绝；P5 队列回归覆盖 `pending_special_nomination` 与
  `excluded_from_priority=false` 的解释。
- 完整本地门：`make check` **950 passed / 4 deselected**，ruff 247 文件、strict mypy
  **122** 源文件通过；`make test-integration` **308 passed**；`make migrate-check`
  0010↔0011 upgrade/downgrade/upgrade 通过；`make e2e` **4 passed**；`make demo`
  health/papers 通过；`make secret-scan` OK。测试仅用 FakeLLM/临时 SQLite，无新依赖。
- 能做：把 P4 提名落库并按人工决定归入特殊桶或保留主题桶；不能做：自动分档、配额
  限制、特殊桶页面/API、特殊桶日报槽位、主题提升/降级、自定义视图或权重治理。
- 规模说明：本卡超过 8 文件/400 行指引（源码 + 测试约 1.1k 行），原因是迁移、
  领域/映射/仓储、process/queue 接入、CLI、证据/决策失败路径与回归共同构成
  完整竖切；没有提前实现 S2–S6。
- 回滚：停止应用并备份后回退代码；`alembic downgrade 0010` 删除提名表。已接受并
  写入的 special filing/`shelved` 状态不会自动回退，需要人工改回主题桶并重算，
  或在恢复备份后重放。
- 已知限制：普通 review 不再包含特殊提名，旧库中已打开的 review item 仍按旧原因
  处理；同一文献只允许一个特殊 Filing，特殊桶间移动/提升留到后续卡。

## P7 Handoff（后续卡）

- S2 只能消费 `pending/accepted` 提名与三个特殊桶，不得为视图或日报改写 `paper_bucket`
  的历史 Filing；档位失效时保留最后一次人工覆盖记录。
- S3 读取特殊桶必须显示档位/理由/证据，且不得返回 P/rank；S4/S5 不得写全局配置或
  复用特殊桶 Filing 绕过 `excluded_from_priority`。
- S6 完成后再更新 Beta 验收与 CHANGELOG；P7 tag 待发布流程，不提前宣称 Beta。
