# P6 日报推送、阅读反馈与归档

状态：**S1→S7 本地实现、Docker/CI 与本地阶段门通过；独立验收未通过（F1/F2），返工已实施并通过本地阶段门，独立复验待执行**。
依据：开发计划 §3 P6、设计 §9/11/12、P5 handoff 与独立验收。测试离线、固定时钟、tmp_path。

## 执行卡（每卡预计 0.5–2 天）

每卡执行 make check、相关 unit/contract/integration、secret-scan；阶段结束跑完整阶段门。
前卡提交为下一卡 stacked 基线；无未通过检查时才开始下一卡。各卡均交代码、测试、文档、
CHANGELOG。超过 8 文件/400 行在执行记录说明。跨模块决策见 ADR0016。

| 卡 | 目标、输入→输出与文件 | 契约、失败路径、验收/演示 | 范围外与回滚 |
|---|---|---|---|
| S1 编排 | P5 QueueItem/选定配置/历史→delivery/contracts.py、compose.py | 7主队列、每标签2、至少2标签、H高槽、精读2/粗读5；冷却/延期/缺候选解释；unit test_digest_compose | 无发送/写库；撤销代码 |
| S2 渲染 | Digest→delivery/render.py、MD/HTML golden | 元信息/标签/理由/完整解释/特殊占位/待审数；不可信文本转义、URL白名单；unit test_digest_render | 无发送/操作；撤销代码 |
| S3 推送 | 已确认上下文+Digest→delivery/service.py、channels.py、storage/delivery.py、0009、API/CLI | 持久化日期唯一日报与渠道投递，文件/SMTP、幂等、暂停安静门、失败隔离；integration test_digest_delivery | 无用户反馈；恢复备份后降0008 |
| S4 操作 | Action→delivery/actions.py、api/actions.py、CLI | done/score/note/snooze/skip/reject，事件与Rating原子追加；严格整数/幂等冲突/冷却；integration test_reading_actions | 无完整归档展示；无迁移，回退保留事件 |
| S5 归档 | done+当时上下文→delivery/archive.py、0010、archive API/CLI/export | 原子归档冻结元数据/摘要/标签桶/建议实际方式/完整分数与版本；修订历史；BibTeX/CSV/MD；integration test_archive | 无删除/同步；备份后降0009 |
| S6 页面 | 已有服务→api/pages.py、本地HTMX、最小今日/队列/详情/归档 | 薄页面调用操作，安全文本/错误显示；真实Uvicorn阅读闭环e2e；make e2e | 无权重治理/特殊桶规则；回退页面不撤销数据 |
| S7 周任务 | Rating/done/阅读时快照→jobs/preference.py、CLI/API、Run报告 | 调P5 U、截止/修订/覆盖率检查、周幂等、只写未读当日新快照，历史归档不改；integration test_weekly_preference | 无真实评分调优/备注模型/常驻调度；回退保留报告 |

## 冻结决策

- 日报消费 P5 合格主题队列，不重算静态分；不足不补低质量文献。特殊桶空占位，P7补齐。
- 精读需 R≥80且E≥70，或已持久化rubric问题/方法直接相关证据；不猜主线/经典信号。
  超精读容量优先延期，只有显式请求允许降级时才粗读并注明原因。
- 主动skip冷却7自然日；snooze 3/7/14天，按到期日恢复，覆盖一般已推送去重。
- 本地日期以G4 runtime时区为准；读取允许暂停，生成/发送执行完整schedule门。
- G4默认local_markdown；SMTP仅新G4明确选定smtp时可发送，旧配置不升级为发信授权。
- done在S4写DONE/Rating；S5加入同一事务即时归档；S4中间版本不宣称完整阅读闭环。
- 评分/备注修订追加Rating和事件，初始归档永久冻结；导出同时显示初始与最新反馈和历史。
- 不向任何模型发送备注，不启用后台抓取、真实模型或真实邮件测试。

## 执行记录

### S1（2026-10-22）

- 编排/容量/多样性/H高槽/精读溢出延期、冷却与重推纯函数完成；积压时优先选择非当日新文献。
- make check：885 passed / 4 deselected，ruff、strict mypy 103源文件通过；secret-scan/diff通过。
  9项新增unit/contract；测试数据库使用/dev/shm独立tmp_path，未触碰var。
- 能做：生成结构化可解释日报；不能做：持久化、渲染、发送。回滚撤销S1代码，无迁移。
- 共7文件；七卡/跨模块ADR同交。下一卡S2仅消费Digest渲染。P5 tag留待发布流程。

### S2（2026-10-22）

- Markdown/HTML完整卡片与解释、链接白名单/转义、空日报完成；basic.json/md/html为golden。
- make check：888 passed / 4 deselected，ruff/mypy 104源文件通过；secret-scan/diff通过。
  新增3项渲染/注入测试；无网络。能渲染，不能发送；无迁移，撤销代码即可回滚。
- S1净407行略超400指引（七卡/ADR同交）；S2较大golden含完整解释JSON，未推进S3。

### S3（2026-10-22）

- 0009新增digest/delivery，日期唯一正文与渠道唯一预约。生成调用P5快照，发送前独立提交
  预约、复核G4/暂停/安静/本地今日/push_time/渠道；仅queued推进pushed。
- API/CLI/SMTP配置见[delivery-cli.md](../delivery-cli.md)；SMTP只在G4明确smtp时启用。
  本地文件0600、原子不覆盖；SMTP异常留uncertain且不自动重试，文件仍可独立成功。
- make check：900 passed / 4 deselected，ruff/mypy109源文件；integration275通过；
  migrate-check base↔0009通过；secret-scan/diff通过。新增10集成+2单元，未调用真实邮件。
- 能做：生成、审阅、手动投递和文件恢复；不能做：阅读操作、完整归档、常驻调度。
- 超8文件/400行：迁移/预约事务/两渠道/配置门禁/API/CLI及12项失败回归组成完整竖切；
  旧SMTP非法值测试改为webhook，doctor/head断言随迁移更新，无无关重构。回滚见ADR0016。

### S4（2026-10-22）

- 六种阅读操作API/CLI、严格字段/评分、全局request_id回执、冷却/到期恢复、Rating修订
  与状态/事件原子写入完成。done暂落DONE；S5接入同事务归档。发送前阻止已变化条目，
  已送达回执在完成后仍可幂等读取；未声称paper.pushed的user_read触发是邮件投递。
- make check：915 passed / 4 deselected，ruff/mypy112源文件；secret-scan/diff通过。
  新增8单元+7集成，覆盖G4/非法状态/幂等冲突/修订历史/延期与skip/回滚/真实CLI。
- 无迁移；完整撤销需恢复操作前备份。超8文件/400行：输入校验、服务/API/CLI、审计
  失败和投递衔接回归共同构成S4，未开始S5内容冻结或导出。

### S5（2026-10-22）

- 0010新增archive_snapshot.content_json；done/Rating/完整内容/hash/archived同事务。
  阅读时元数据/四槽摘要证据/标签桶与徽章/建议实际阅读方式/完整评分及模型版本冻结。
  修订仅追加Rating/事件；查询并列显示初始与最新反馈及历史，不回写已归档P。
- 归档API/CLI筛选、BibTeX/CSV/Markdown/JSON导出完成；已有P1空内容历史明确标记缺失。
  导出链接/标签/评分/备注/修订齐全，CSV防公式注入，文件0600不覆盖。
- make check：923 passed / 4 deselected，ruff/mypy116源文件；22项定向回归通过，
  migrate-check base↔0010、secret-scan/diff通过。新增4unit+4integration；无真实外部调用。
- 能完成API/CLI阅读归档闭环；不能做网页/周任务/归档删除。超8文件/400行：归档内容
  迁移/映射、事务接入、查询/多格式导出/API/CLI与golden共同构成S5。回滚见ADR0016。

### S6（2026-10-22）

- 今日/队列/详情/归档薄HTML页面与本地HTMX2.0.11完成（官方SRI核对、0BSD许可证随包），
  无Python新依赖/CDN。CSP、同源表单、转义、错误内联、可选反馈、修订入口均通过。
- make check：926 passed / 4 deselected，ruff/mypy117源文件；3项页面集成通过；
  make e2e：4 passed，真实Uvicorn/CLI全链路已扩展到日报→HTML表单→归档→导出。
  子进程使用测试目录sitecustomize固定基准+单调微秒，应用无测试时钟入口；secret/diff通过。
- 浏览器实测使用独立E2E库副本：展开卡片→4分+备注→完成→归档列表可见；本地HTMX实际
  替换卡片成功。截图保存于本次工作区可视化目录p6-ui-archive.png；临时服务/标签已关闭。
- 验收补齐阅读方式覆盖事件（归档原有overridden字段保留）；无schema变更。
  超8文件/400行：四页/样式/vendor/许可证/package data、API集成/e2e和必要审计衔接。
  无常驻调度、特殊桶完整规则或P7治理；回滚撤销页面不撤销归档。S7尚未开始。

### S7（2026-10-22）

- 周度任务API/CLI/Run报告和数据检查实现；调用P5快照/偏好算术，周截止00:00排他，
  默认同周同G4幂等，失败报告保留、可显式新run_id重试；只追加未读候选快照，不回写归档P。
- 原始评分/标签非法时报告failed且零候选写入；P1可选字段缺失仅警告；null不当低分。
  覆盖率<30%标weak_signal。读取历史优先SQL投影归档metadata/标签，不读备注或完整正文。
- 新增6项周任务集成、4项输入单元；收尾补首次入队清库存、有数据迁移、未知SMTP跨日
  去重各1项回归。12项S7+P5定向通过；最终全部阶段门见[P6本地验证](P6-acceptance.md)。
- 收尾修正两点：积压优先按首次入队日而非出版日；reserved/uncertain投递同样纳入可能
  已送达历史，避免次日因换日期重推。未评分的人类界面显示“未评分”，JSON仍为null。
- 无新迁移/依赖。超8文件/400行：任务/报告/API/CLI、阅读时投影、失败/隐私/跨日回归、
  最终验收与状态文档构成P6收尾；未提前实现P7。
- 最终make check **939 passed / 4 deselected**、ruff/mypy **120**源文件；integration
  **297 passed**，e2e **4 passed**；base↔0010、临时库demo8789、secret-scan/diff通过。
  有数据0010↔0009与wheel静态资源/SRI核对通过；Docker因权限未验证，远程CI未触发。

### S7 返工（2026-10-22，F1/F2/N1–N4）

- F1：周任务候选改为 `WEEKLY_ELIGIBLE = ELIGIBLE | PUSHED`，`create_snapshots` 接受
  候选集合；PUSHED 不重新入队，报告查询同步。snooze 到期的 SNOOZED 按日报同规则恢复，
  报告新增 `resumed`。归档/特殊桶仍无新P且不被回写。
- F2：第二次无副作用 preflight 与文件/SMTP I/O 分开捕获；复核失败记录 `failed` 和
  “未尝试投递”，不写 `uncertain`；SMTP 修正后同一预约可直接重试，文件仍用
  `--recover-file`。只有实际 I/O 异常才写 SMTP `uncertain`。
- N1：文档明确同日快照不可重写；周任务建议在 update_weekday 00:00 截止后、当日日报前
  运行，报告复用已有同日快照，补同周/同日回归。
- N2：无日报页面的生成表单携带所选 `day`，生成后回到该日期。
- N3：文档统一为写 `paper.pushed` 并在 payload 标记 `trigger=user_read`，无独立事件。
- N4：`safe_url` 拒绝任何 userinfo（含空用户名、空密码），补 URL 回归。
- make check **944 passed / 4 deselected**、ruff/mypy **120**源文件；新增 5 项回归
  （F1 投递后周任务、snooze 恢复、同日快照、F2 二次 preflight、N2 日期表单），
  N4 扩展现有 URL 用例。完整阶段门与限制见[返工验证记录](P6-acceptance-rework.md)。

## P6 Handoff → P7

- 已具备日报/动作/归档/导出/API/CLI/最小页面、周任务；操作指南delivery-cli.md。
  Digest/Action/ArchiveRecord冻结；可直接使用QueueItem解释、特殊桶空占位和反馈事件。
- head0010；0009日报/投递，0010完整归档内容；P5日快照/静态快照仍只读历史。
  跨日冷却、未知SMTP预约、归档不可变与备注隐私必须保留；不要绕过G4选定版本。
- P6 独立验收未通过后已按 F1/F2/N1–N4 返工：周任务覆盖 PUSHED 未读、二次 preflight
  不再伪造 uncertain，并补齐同日快照/日期表单/URL/事件语义。返工阶段门通过；独立复验
  通过前 P7 不开始、P6 tag 不创建。复验后再让用户审阅真实首日报，然后按计划拆特殊桶/
  自定义视图/权重治理卡片。本轮未使用合成4分/认可替代真实反馈，不自动修改研究方向或全局配置。
- 当前没有归档删除/撤销、完整特殊桶分档/配额、权重激活、常驻调度、真实模型/邮件质量
  验收。P8处理真实偏好评估、隐私文本授权与运维硬化；不能暗中外发备注。
- Docker 本地与远程 CI 已通过；PR #8 已合并到 main，P6 tag 未创建，独立复验待执行。
  继承P1 F5/F3与既有阶段tag债务。完整风险/回滚/命令/证据见 P6-acceptance.md、
  [P6 独立验收报告](P6-acceptance-independent.md)、[P6 返工验证](P6-acceptance-rework.md)
  及 ADR0016/0017。

### 本地提交链

S1 `4c5fff1` → S2 `2233b5e` → S3 `e8fdac5` → S4 `03659e0` → S5 `439dd3f`
→ S6 `2d1c717` → S7本分支收尾提交。每张卡前一检查通过后才建立下一张stacked分支。
返工：`38e3293`（F1）→ `77b0d50`（F2）→ `dc590e5`（N2/N4）→ `a6fbe58`（返工文档）
→ 随后仅追加全新 clone 证据与规模说明的文档提交。返工基线为独立验收文档 `5684be0`。

## 独立验收（2026-10-22）

独立验收结论为 **未通过（Not Accepted）**：阶段门、全新 clone、Docker、CI 与浏览器闭环通过，
但 F1（`PUSHED` 未读候选不进入周任务）和 F2（第二次 preflight 失败在未发送时被写成 SMTP
`uncertain`）属于 P6 契约内缺陷。P6 暂不关闭，P7 不开始，P6 tag 不创建。
返工条件、复现与证据见[P6 独立验收报告](P6-acceptance-independent.md)。

返工已按该报告实施（F1/F2/N1–N4），本地阶段门重跑通过；独立复验仍未执行。返工代码、
回归与门禁证据见[P6 返工验证记录](P6-acceptance-rework.md)。
