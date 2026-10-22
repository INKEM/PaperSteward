# P7 特殊桶、自定义排序与权重治理

> 状态：**P7-S3 实现（2026-10-22）；S4–S6 待开始。**
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

## S2 契约（2026-10-22）

- 新增迁移 `0012` 的 `special_tier_snapshot`：追加写档位评估历史，保存 V、档位、五维分、
  权重、证据、机器原因、人类可读理由、rubric 版本与输入摘要；最新一条为当前档位。
  用户改档追加新行（`assigned_by=user`、必填备注），不覆盖旧评估，也不改变 V 的含义。
- rubric 严格按设计 §8.2–8.4 的五维权重：review 跨层深度 .30/覆盖 .25/权威 .20/
  清晰 .15/时效 .10；distant 迁移潜力 .35/桥接清晰 .20/可信度 .20/新颖性 .15/可及性 .10；
  classic 奠基性 .35/长期影响 .25/权威 .20/当前相关 .20。五维均为 0–100，V 为加权和
  保留一位小数；V≥75 为 high，50≤V<75 为 medium，V<50 为 low。
- 维度分由确定性代理信号推导（不新增 LLM 调用）：提名证据有效率/置信度/门禁原因、
  review 层级覆盖、bridge memo 完整度、classic basis、venue/引用、OA/代码数据链接、
  发表年龄与当前 R。缺失信号保持中性 50 并记录 `missing_*` 原因；代理不把缺失解释为负面。
  该启发式只用于 Beta 分档，用户可覆盖，真实质量评估留给 P8。
- 三个特殊桶的周配额写入 `bucket.quota`：review 3、distant 2、classic 1（设计区间下界，
  可按桶调整）；配额是上限，不是每日/每周 KPI。每日最多 2 个特殊槽位，三桶按
  review→classic→distant 循环轮换，起始桶由日期序号决定；空桶/低档/配额用尽时跳过，
  不跨桶比 V、不凑数。
- rotation 计划只消费 `shelved`、当前归属特殊桶、已有档位快照的文献；high/medium 才进入
  推送计划，low 仅在特殊桶视图展示。每周已用量由调用方传入（S2 不新增推送记录表）；
  同一桶每日最多占一个槽位。S2 只提供纯计划与 CLI，不改 P6 日报。
- CLI：`shelf assess <paper>` 重新评估并追加快照；`shelf tier <paper> --tier ... --by ...
  --note ...` 人工改档；`shelf plan --date ... [--used-review/…]` 输出轮换计划。
  V 只在 `shelf assess --json`/breakdown 中供审计，不进入特殊桶视图、日报或日常文本。
  `shelf accept` 在同一事务内自动评估新归入的特殊桶，失败整体回滚。
- 范围外：特殊桶视图/API、日报实际特殊槽位、推送/已读状态、周回顾清理建议、LLM 质量
  评分、跨桶排名、特殊↔主题移动、全局权重治理。

## S2 执行记录（2026-10-22）

- 已完成：迁移 `0012` 的 `special_tier_snapshot` 追加写历史、五维 rubric/V/阈值、
  确定性代理评估、人工改档、三个特殊桶默认周配额、每日两槽轮换纯计划、
  `shelf assess|tier|plan` CLI；`shelf accept` 在同一事务内自动分档并回滚失败。
- 新增 34 项测试：21 项 rubric/阈值/缺失信号/三类推导单元测试；7 项轮换/配额/跳过/
  并列/非法输入测试；6 项集成测试（accept 分档、覆盖历史、默认配额、候选加载、
  CLI assess/tier/plan、分档失败整体回滚）。
- 完整本地门：`make check` **984 passed / 4 deselected**，ruff 253 文件、strict mypy
  **124** 源文件通过；`make test-integration` **314 passed**；`make migrate-check`
  0011↔0012 upgrade/downgrade/upgrade 通过；`make e2e` **4 passed**；`make demo`
  health/papers 通过；`make secret-scan` OK。测试仅用 FakeLLM/临时 SQLite，无新依赖。
- 能做：为已接受特殊桶文献生成/覆盖档位，按周配额和日期轮换生成每日最多两篇的
  候选计划；不能做：特殊桶页面/API、日报实际特殊槽位、推送状态/周用量持久化、
  周回顾清理建议、LLM 质量评分或跨桶排名。
- 规模说明：本卡超过 8 文件/400 行指引，原因是迁移、实体/映射/仓储、rubric/代理、
  配额/轮换、accept/CLI 接入与表驱动回归共同构成完整竖切；未提前实现 S3–S6。
- 回滚：停止应用并备份后回退代码；`alembic downgrade 0011` 删除档位历史。已写入桶的
  周配额与已接受 Filing/`shelved` 状态不会自动回退；需要时人工恢复配额或在备份中重放。
- 已知限制：维度分是确定性代理而非语义质量判断，缺失信号按中性 50；周已用量由调用方
  传入，S2 不新增推送记录；低档默认不进入计划；V 仅内部审计，不作为展示优先级。

## S3 契约（2026-10-22）

- 只读 API：`GET /shelves` 概览、`GET /shelves/{kind}` 分页列表、`GET
  /shelves/{kind}/{paper_id}` 详情；列表使用 `items/total/limit/offset` 信封。
  `tier=high|medium|low|unrated` 过滤；非法 kind 404、非法 tier 422。
- 只读 CLI：`shelf list [--kind ...]`、`shelf distant`、`shelf classic`、`shelf show
  <paper>`；`shelf review` 仍是 S1 提名队列，查看综述桶用 `shelf list --kind review`。
- 只读页面：`/ui/shelves`（主导航“特殊桶”），按 review→distant→classic 分区展示
  配额、档位分布、标题、档位、理由、证据与专属解释；空桶显示“暂无内容（不占位）”。
  特殊桶详情页改走档位视图，不再把 `special_bucket` 当成 P 解释。
- 展示字段：档位、主/副标签、提名理由、逐字证据、档位说明、review layers、classic
  basis、四段 bridge memo、评估来源/时间与改档备注。**不返回/展示 V、P、rank_score**；
  `excluded_from_priority=true`。未评估的已归属文献显示 `tier=null` 并提示 `shelf assess`。
- 只展示 `shelved/pushed/reading/done` 的特殊桶文献；`archived` 走归档页，`rejected`
  不展示。空桶返回空列表，不生成占位文献。读取不写库、不自动补评估、不调用 LLM。
- 本卡无迁移/新依赖；不做特殊桶推送、日报特殊槽位、档位/配额编辑或自定义视图。

## S3 执行记录（2026-10-22）

- 已完成：`delivery/shelf_view.py` 只读服务、`/shelves` API、`shelf list|distant|classic|show`
  CLI、`/ui/shelves` 页面与特殊桶详情分支、导航入口和档位徽章样式。
- 新增 8 项集成测试：概览/过滤/分页/详情、无 P/V/rank、未知 kind 404、非法 tier 422、
  只读不写档位、未建桶空视图、页面转义与 bridge/classic 展示、CLI 列表/详情/错误路径。
- 完整本地门：`make check` **992 passed / 4 deselected**，ruff 256 文件、strict mypy
  **126** 源文件通过；`make test-integration` **322 passed**；`make migrate-check`
  head 0012 upgrade/downgrade/upgrade 通过；`make e2e` **4 passed**；`make demo`
  health/papers 通过；`make secret-scan` OK。测试仅用 FakeLLM/临时 SQLite，无新依赖。
- 能做：按三个特殊桶查看档位/理由/证据与专属解释，CLI/API/页面均只读；不能做：
  特殊桶推送、日报实际槽位、档位/配额编辑、周用量、低档清理建议或自定义视图。
- 规模说明：本卡超过 8 文件/400 行指引，原因是只读服务、API、CLI、页面/导航、
  样式与安全/空桶/未评估回归共同构成完整竖切；未提前实现 S4–S6。
- 阶段门稳定性：CLI ingest 集成测试固定关闭安静时段、队列待审提名测试固定时钟，
  避免 22:00 后安静时段导致既有测试误报；生产时间/调度逻辑未改。
- 回滚：删除 API/CLI/页面代码即可；无迁移、无数据写入，已保存的档位/配额/Filing 不变。
- 已知限制：页面每桶最多 50 条并提供“查看更多”；API 支持 limit/offset；页面只提供
  kind 过滤，tier 过滤走 API/CLI。已归档特殊桶走归档页，不在 shelf 视图重复展示。

## P7 Handoff（后续卡）

- S3 的 `shelf_view`/API 只读契约已冻结；S4 自定义视图不得复用特殊桶 Filing 绕过
  `excluded_from_priority`，也不得把 V 当 P 或改写档位历史。
- S4/S5 不得写全局配置或特殊桶 Filing；自定义视图只影响当前排序，权重变更仍需
  预览/确认/版本。S6 e2e 消费 S3 的 `/shelves` 与 `/ui/shelves`。
- S6 完成后再更新 Beta 验收与 CHANGELOG；P7 tag 待发布流程，不提前宣称 Beta。
