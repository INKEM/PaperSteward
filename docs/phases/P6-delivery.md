# P6 日报推送、阅读反馈与归档

状态：执行中；严格按 S1→S7，本地 stacked 分支逐卡验证和提交，远程合并/发布另记。
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
  已送达回执在完成后仍可幂等读取；未声称user_read事件是邮件投递。
- make check：915 passed / 4 deselected，ruff/mypy112源文件；secret-scan/diff通过。
  新增8单元+7集成，覆盖G4/非法状态/幂等冲突/修订历史/延期与skip/回滚/真实CLI。
- 无迁移；完整撤销需恢复操作前备份。超8文件/400行：输入校验、服务/API/CLI、审计
  失败和投递衔接回归共同构成S4，未开始S5内容冻结或导出。
