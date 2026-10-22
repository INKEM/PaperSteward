# P7 特殊桶、自定义排序与权重治理

> 状态：**P7-S1–S6 + F1–F3/N1–N4 返工完成；2026-10-22 二次独立复验
> Accepted（功能层面）；P7 tag/Beta 未发布，merge/CI/Docker 发布门待完成。**
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
- 状态只有 `pending / accepted / rejected`；决策记录 `decided_at/decided_by/note`；`reopen` 仅允许 rejected 且文献仍 classified，清空本次决策字段并写事件。
  已决策提名重跑 process 不会被静默覆盖；待审提名重跑只刷新证据与时间，不重复建行。
- 三个特殊桶由固定 ID 幂等补齐：`b-special-review`（跨层综述）、`b-special-distant`
  （远邻领域）、`b-special-classic`（经典/奠基）。只补目录，不设置配额/档位。
- `process` 只持久化提名，不自动归入特殊桶；提名不再复制到 `review_item`，由
  `papersteward shelf review` 专门审核。证据不足的提名仍可在人工确认后 `--force` 接受，
  但必须记录决策人和备注。
- accept 只允许 `classified`、已有唯一主标签与主题归属且无待决 review 的文献：
  替换 Filing 为主题桶→特殊桶，状态转 `shelved`，追加 `paper.bucket_moved` 与
  `paper.shelved`；同一文献其他待审提名自动 rejected（`superseded`）。
- reject 不改变文献状态、主标签或主题归属；被拒绝后可 `shelf reopen` 受控回到 pending（追加审计事件），不重跑 LLM 或重置 Filing。
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

## S4 契约（2026-10-22）

- 迁移 `0013` 新增 `view_config`：保存个人组件权重视图（名称大小写不敏感唯一、
  归一化权重、可选来源 preset/说明、创建时间）；内置预设是代码常量，不建行。
- 预设（组件权重合计 100）：balanced 45/15/10/12/8/10；frontier 35/10/10/25/10/10；
  beginner 45/25/10/10/5/5；reproducible 35/10/40/10/5/0（R/A/E/F/H/U）。
- 权重输入支持任意非负百分比/相对值，至少一个正数，自动归一化；未知组件、重复组件、
  非有限/负值、全零一律拒绝。`P_view = Σ w_i·component_i` 保留一位小数。
- 预览只读当前合格队列（P5 `read_queue`）：不扩大候选集、R 门槛仍生效、特殊桶仍排除；
  按 view score 重排，同分沿用 R→出版日→A→标题→paper_id 的稳定顺序；返回默认名次与
  名次变化。预览不写 `score_snapshot`、不改全局配置。
- CLI：`view presets`、`view preview`（preset/name/view-id/weights 任选）、
  `view save`、`view list`、`view show`、`view delete`；API：`GET /views/presets`、
  `GET/POST /views`、`GET/DELETE /views/{id}`、`POST /views/preview`；页面
  `/ui/views` 只读预览，导航“视图”。保存/删除走 CLI/API。
- 视图与全局配置隔离：保存只写 `view_config`，删除只删该行；同名大小写不敏感冲突、
  与内置 preset 同名冲突；不支持原地覆盖。
- 本卡只支持 R/A/E/F/H/U 组件级权重，不提供静态/动态/准动态整组勾选；不做权重影响
  预览/配置版本/确认（S5）、权重滑块 UI、多用户权限或自动调参。

## S4 执行记录（2026-10-22）

- 已完成：`view_config` 迁移/实体/仓储、预设与归一化、当前队列重排、保存/加载/删除、
  API、CLI、`/ui/views` 页面与导航/表格样式。
- 新增 11 项测试：6 项单元（预设权重/归一化/公式/view score/非法输入/实体校验）；
  5 项集成（API save/load/delete、预览重排与只读、错误路径、CLI、页面与保存视图）。
- 完整本地门：`make check` **1003 passed / 4 deselected**，ruff 262 文件、strict mypy
  **129** 源文件通过；`make test-integration` **327 passed**；`make migrate-check`
  0012↔0013 upgrade/downgrade/upgrade 通过；`make e2e` **4 passed**；`make demo`
  health/papers 通过；`make secret-scan` OK。测试仅用 FakeLLM/临时 SQLite，无新依赖。
- 能做：预设/自定义权重重排当前队列、保存/加载/删除视图并查看公式/名次变化；不能做：
  全局权重预览/激活/版本、权重滑块 UI、视图内特殊桶混排或扩大候选集。
- 规模说明：本卡超过 8 文件/400 行指引，原因是迁移、实体/仓储、权重服务、API、CLI、
  页面和安全/只读/重排回归共同构成完整竖切；未提前实现 S5–S6。
- 回滚：`alembic downgrade 0012` 删除已保存视图；内置预设是代码常量。历史 P 快照、
  全局配置、特殊桶档位/Filing 均不受影响。
- 已知限制：预览日期缺省 UTC；同日快照不可重写；视图只排序当前队列，不生成持久化
  视图分数；同名保存需先删除或另起名称。

## S5 契约（2026-10-22）

- 迁移 `0014` 新增 `scoring_change`：previewed/confirmed 状态、base/config version、
  预览日、生效日、完整 proposed 配置、影响预览 JSON、preview hash、确认人/时间/备注。
  预览只追加 previewed 行；确认在原行记录 confirmed 字段，历史不删除。
- 预览：`config preview --set/--file` 或 `POST /config/scoring/preview`。先验证 G4 与
  当前激活配置，再按 R/A/E/F/H/U 六组件重排当前合格队列；保存旧/新组件权重、逐篇
  旧/新名次与 rank_score、Top-N 明细和 hash。预览不写配置版本、不写静态快照、不激活。
- 治理 allowlist：只允许 `static.weight/relevance/authority/extra_value`、
  `dynamic.weight/freshness/hunger`、`quasi_dynamic.weight`；`gates`、`stability`、
  half-life、U 子权重等非权重字段 400 拒绝，完整 config 也做非权重 diff。
- 确认：`config confirm <id>` 或 `POST /config/scoring/changes/{id}/confirm`。重算影响并
  核对 hash（队列/基线变化则 409 重新预览）；校验版本不存在、生效日晚于所有已有静态
  快照；确认人在新配置版本、`config.activated` 事件和生效日静态快照写入后记录。
- 激活：`bound_context(day)` 取 `effective_on≤day` 的最新 confirmed 变更；否则用 G4
  原始评分版本。G4 原始配置仍复核，防止初始化证据漂移。生效日前队列继续用旧配置；
  生效日及以后使用新配置。确认时为生效日重算已有静态快照：
  `S_new = R·wR' + A·wA' + E·wE'`，不重新调用 LLM，不改写历史快照。
- 确认后、生效日前新 process/promote 的文献会为每个 pending activation 追加生效日
  新版本静态快照（R/A/E 线性重算），避免生效日 `static_version_mismatch` 丢失；
  确认时只覆盖当时已存在的快照。
- 版本：`config import` 仍只保存版本不激活；自定义视图 `view_config` 与全局配置隔离，
  不能作为激活版本。预览/确认不修改归档、阅读历史或特殊桶 Filing。
- 生效日/预览日缺省今天/+1 天；必须晚于所有已有静态快照。同一变更不能重复确认；
  不做自动调参、自动确认、权重滑块 UI 或多用户权限。

## S5 执行记录（2026-10-22）

- 已完成：`scoring_change` 迁移/实体/仓储、影响预览/hash、确认激活、按日选择配置、
  生效日静态分重算、`config preview|confirm|changes|show` CLI 与 `/config/scoring`
  API、组件权重展开/覆盖工具。
- 新增 6 项测试：3 项单元（分层权重展开、版本提升、覆盖/交叉校验）；3 项集成
  （预览不改配置/快照、确认激活与生效日队列重排、CLI/API 错误与历史）。
- 完整本地门：`make check` **1009 passed / 4 deselected**，ruff 267 文件、strict mypy
  **131** 源文件通过；`make test-integration` **330 passed**；`make migrate-check`
  0013↔0014 upgrade/downgrade/upgrade 通过；`make e2e` **4 passed**；`make demo`
  health/papers 通过；`make secret-scan` OK。测试仅用 FakeLLM/临时 SQLite，无新依赖。
- 能做：预览 Top-N 影响、自动提升版本、显式确认、按生效日激活并在生效日使用新权重；
  不能做：自动调参/自动确认、UI 滑块、原地覆盖版本、跨日重写历史快照或撤销已激活版本。
- 规模说明：本卡超过 8 文件/400 行指引，原因是迁移、治理记录、影响计算、按日绑定、
  静态重算、API/CLI 与并发/过期/错误路径共同构成完整竖切；未提前实现 S6。
- 回滚：`alembic downgrade 0013` 删除变更记录；已保存配置版本/静态快照不回退。撤销
  激活需新建更高版本重新预览/确认，不删除历史。旧代码不认识 activation 时继续使用
  G4 版本，但需先备份/导出变更记录。
- 已知限制：预览 hash 以完整当前队列为输入，队列变化会使确认失败并需重新预览；
  静态重算只覆盖已有静态快照的未归档文献；治理没有独立页面（CLI/API）。

## S6 契约（2026-10-22）

- 真实 Uvicorn + 真实 `papersteward` 子进程、固定时钟和临时 SQLite：一组 P7 合成来源
  经 ingest/process 产生主题候选与待审特殊提名；逐篇 `shelf accept` 进入 review/distant/
  classic，检查 `/shelves`、`/ui/shelves`、桥接假设、经典依据、档位/计划与“特殊桶不进
  队列”；再生成日快照，验证特殊桶不占 P。
- 自定义视图：`view preview --preset` 校验预设/公式；`--weights R:100` 重排当前队列，
  `view save` 后按名称预览；`/ui/views` 可见 P_view；视图不写全局配置/激活版本。
- 全局治理：`config preview` 预览 Top-N 影响且不写 1.0.1 配置；`config confirm` 写版本、
  `config.activated` 与生效日静态快照；旧日队列仍用旧配置，生效日快照用新配置重排。
- 特殊桶提升：新增 `shelf promote <paper> --by/--note`。只允许 shelved 特殊桶且主标签
  主题桶存在、R 达到主队列门槛；替换 Filing、`shelved → classified`、写 bucket_moved
  事件。静态快照版本匹配时复用，否则按当前 R/A/E 追加新日期快照；下一次日快照纳入 P，
  历史 P/特殊桶档位不改。未提供主题桶→特殊桶的独立 demote 命令。
- 失败路径：未 shelved/重复提升冲突、低 R 拒绝、无效视图权重/重复名称、过期 preview
  hash、版本已存在、过期生效日；均由 unit/integration/CLI/API 覆盖。

## S6 执行记录（2026-10-22）

- E2E 在同一真实链路中新增 5 篇 P7 合成论文：2 篇主题（高 R / 高 E）、3 篇特殊提名；
  9 条待审提名，分别接受 review/distant/classic；特殊桶按档位展示且队列只含主题候选。
- 验证 `view presets/preview/save` + `/ui/views` 重排；`config preview/confirm` +
  `/config/scoring/*`；用独立生效日时钟生成 2026-11-02 快照，证明旧日仍用旧配置、
  生效日使用新版本且提升后的特殊文献回到主队列。
- 新增 1 项集成测试（提升回主题桶、事件、静态复用、日快照后回到 P、重复提升冲突），
  e2e 从 G1 串到 P7；新增 4 个合成 fixture（1 来源、3 process）。
- 本地门：`make check` **1010 passed / 4 deselected**，ruff **268** 文件、strict mypy
  **131** 源文件；`make test-integration` **331 passed**；`make e2e` **4 passed**；
  `make migrate-check` 0013↔0014 通过；`make demo`、`make secret-scan` 通过。无新依赖。
- 范围说明：S6 超过 8 文件/400 行指引，原因是最终 e2e、合成 fixture、提升补口与阶段
  验收文档同交；未开始 P8，不声明 Beta 发布。

## P7 返工记录（2026-10-22）

独立验收 F1–F3/N1–N4 已返工，详见 [P7 返工验证记录](P7-acceptance-rework.md)：

- F1 非权重字段 allowlist；F2 确认−处理/提升−生效窗口补快照；F3 `shelf reopen`；
  N1 changes total；N2 迁移 `0015` 档位 `reviewed_by`；N3 runtime 本地日且不污染
  binding hash；N4 预览改按 rank_score。
- 返工后本地门：`make check` **1015 passed / 4 deselected**，ruff **270** 文件、
  strict mypy **132** 源文件；integration **336 passed**；e2e **4 passed**；
  migrate-check 0014↔0015、demo、secret 通过；fresh clone Python 3.14.7 同数通过。
- **二次独立复验 Accepted（功能层面）**：F1–F3、N1/N2 保持闭环；N4 preview 层权重
  重复计算已修（preview 与生效日队列数值对齐），N3 config preview 缺省本地日已修，
  N2 API 已暴露 `tier_reviewed_by`。详见 [P7-acceptance-rework.md §12](P7-acceptance-rework.md)。
- 发布门未完成：返工分支 `fix/p7-rework-n4-n3`/`d8918de` 未推送、无对应 CI；
  Docker 未重建到 `d8918de`。P7 tag/Beta 不随功能验收自动发布。

## P7 Handoff（P8）

- P7-S1–S6 本地实现完成，返工 head 为 `0015`（迁移 `0015` 档位审核人）；S6 本体无
  schema 变更。原始验收对象 `6a877e9`（PR #10，已合并到 main `56382c0`）；返工分支
  `fix/p7-rework-n4-n3`，二次返工代码 `d8918de`（本地未推送）。
- **2026-10-22 独立验收 Not Accepted**：F1 权重治理可激活非权重字段、F2 确认后至
  生效日之间处理/提升的文献在生效日被旧静态快照排除、F3 拒绝后无法重新提名；另有
  N1–N4。详见 [P7-acceptance-independent.md](P7-acceptance-independent.md)。
- **二次独立复验 Accepted（功能层面）**：F1–F3 与 N1–N4 均闭环；N4 预览数值、N3
  本地日、N2 API reviewer 字段经独立探针/测试通过。详见
  [P7-acceptance-rework.md §12](P7-acceptance-rework.md)。
- 冻结契约：特殊桶无 P/rank；Filing 唯一；主标签保留；视图只重排当前队列且不得写
  全局配置；全局权重必须 preview→confirm→版本→生效日；G4 原始证据仍复核。
- 已知限制：主题桶→特殊桶独立 demote、提升 API（当前仅 CLI）、提升低 R 文献、
  P7 tag/Beta 发布未做；P8 成本/性能/降级不在此阶段。
- **P7 返工功能验收已通过；P7 tag/Beta 未发布**。P8 在返工合并到 main、CI/Docker
  发布门完成后开始；当前不得把本地未推送分支当发布版本。
- P8 开工前先读 `docs/phases/P7-acceptance.md`、
  `docs/phases/P7-acceptance-independent.md`、`docs/phases/P7-acceptance-rework.md`
  与 P7 handoff；当前 `make check` 1017 passed、integration 338、e2e 4 passed。
