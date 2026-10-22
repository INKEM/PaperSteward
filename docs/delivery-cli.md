# 日报、阅读与归档

先完成G1–G4、采集、process/review。日报生成会调用P5快照；不会调用真实LLM。

```bash
papersteward digest --date 2026-10-22 --json
papersteward digest --date 2026-10-22 --out out/digest.md --json
# 只有G4明确选择smtp，并配置PAPERSTEWARD_SMTP_*后才允许邮件
papersteward digest --date 2026-10-22 --send-smtp --json
```

POST /digests body `{"digest_date":"2026-10-22"}` 生成不可变日报；GET /digests/2026-10-22
读取结构化正文，`?format=markdown|html`读取渲染。读取不写库，暂停仍可审阅。
发送仅提供本地CLI，以便持久化预约事务与外部I/O隔离；HTTP不接收任意文件输出路径。

默认主队列最多7、每标签2、精读2/粗读5；特殊桶为空占位。候选不足/冷却/精读满时
记录deferred原因；`--allow-downgrade`显式允许精读降级粗读。生成同一日期返回原日报。

日期按G4选定时区；正式发送只能发送本地今日、到达已确认push_time后且非暂停/安静时段。
日报按日期唯一；投递按日期/渠道唯一。输出文件0600、不覆盖不同内容；路径冲突后先核对
文件，解决后用相同目标 `--recover-file` 显式恢复。外部I/O前的第二次复核失败会记为
`failed` 并明确“未尝试投递”，不会写成 `uncertain`：SMTP修正后可直接重试同一预约，
本地文件仍需 `--recover-file`。只有实际进入文件写入或SMTP发送后的异常/崩溃才可能留下
`failed`（文件）或 `uncertain`（SMTP）；SMTP预约/uncertain禁止自动重发，须人工核对；
无提供绕过SMTP幂等的重置命令。
同日第二渠道可以独立发送；SMTP失败不会阻止本地文件。G4选择local_markdown时不发邮件。

SMTP配置：HOST、PORT（默认465）、FROM、TO、SECURITY（ssl/starttls）、TIMEOUT_S；
可选USERNAME/PASSWORD须成对。所有变量加PAPERSTEWARD_SMTP_前缀；密码SecretStr且不入报告。
未配置时不发邮件；真实测试只手动运行，CI使用替身。旧G4只确认本地渠道的部署不能
直接切到SMTP；需未来再协商流程或在全新初始化G4时明确选择smtp，不手改确认快照。

数据库head0013（P7-S4）：0009新增digest/delivery，0010扩展归档content_json，
0011新增special_nomination，0012新增special_tier_snapshot，0013新增view_config；
停服务备份后降0012只删除自定义视图，降0011删除档位历史，降0010删除特殊桶提名，
降0009删除完整归档内容列，降0008删除日报与投递记录。
事件/文献状态不会自动撤销，完整回滚使用操作前备份；删除投递记录可能导致重发，禁止
将降迁移作为重试方法。

## 阅读操作（S4）

```bash
papersteward done p001 --mode deep --score 4 --note "可复用到S1" --request-id read-p001
papersteward reading score p001 --score 5 --request-id rate-p001
papersteward reading note p001 --note "修订备注" --request-id note-p001
papersteward reading snooze p002 --days 3 --request-id later-p002
papersteward reading skip p003 --request-id skip-p003
papersteward reading reject p004 --note "与当前问题无关" --request-id reject-p004
```

POST /papers/{id}/actions 接收request_id和kind。done必填mode，可选score/note；score和note
修订必填对应字段（JSON null明确清除）；snooze必填days=3/7/14；reject必填note原因。
请求ID在整个操作日志中唯一；同ID同正文复用回执，不同正文409；CLI省略ID时生成新ID。
评分为严格1–5整数或null，未评分不当低分；修订时间必须晚于此前反馈。修订保留旧值，
不改初始Rating。done只允许已入队/已推送/阅读中且摘要/审核/分类/快照就绪的条目。
用户主动完成尚未投递的队列文献时，会在 `queued→pushed` 迁移中写 `paper.pushed`
事件，并在 payload 标记 `trigger=user_read`；已处于 `pushed` 的条目不会重复写该事件。
没有独立的 `user_read` 事件，`paper.pushed` 也不表示邮件已送达。
阅读操作只需要G4、不受暂停影响；发送已生成但随后被跳过/拒绝/完成的条目会拒绝。
S5已加入同事务即时归档；归档写入失败将撤销done、Rating和事件。返回status=archived。

## 归档查询与导出（S5）

```bash
papersteward archive list --tag T01 --min-score 4 --query 检索 --json
papersteward archive export --format bibtex --out out/archive.bib
papersteward archive export --format csv --out out/archive.csv
papersteward archive export --format markdown --out out/archive.md
```

GET /archive 支持limit/offset、tag、min_score、query、bucket；GET /archive/{id}显示冻结
内容、初始反馈、最新反馈及全部修订；GET /archive/export?format=bibtex|csv|markdown|json
导出全部记录，包含标签/主观分/备注/链接与反馈历史。不存在删除或修改冻结内容入口。
查询按完成时间倒序；搜索标题/作者/标签/最新备注。缺少完整内容的P1历史快照明确标记
content_available=false，不从当前Paper伪造历史内容。hash不匹配则拒绝读取，保留数据待查。
CSV文本防公式执行；BibTeX与Markdown对不可信字段转义。导出0600、不覆盖不同内容。
删除归档不在P6范围；修订note/score仅追加Rating/事件，不改初始归档和旧优先级。

## 本地页面（S6）

启动原有API后打开 `/ui/today`，可生成今日日报、展开卡片完成阅读并填写实际方式/可选评分/
备注；`/ui/queue`看队列，`/ui/papers/{id}`看完整解释或冻结归档并修订反馈，`/ui/archive`
搜索和导出。阅读方式覆盖建议会追加独立审计事件。发送仍通过显式CLI，不会因开页面发邮件。
HTMX 2.0.11及样式在包内本地分发，运行无需外网；界面不支持删除归档或P7权重治理。

## 周度U任务与评分数据检查（S7）

```bash
papersteward jobs preference-weekly --json
# 数据问题核对修复后，可用明确的新ID记录一次重试；原报告不覆盖
papersteward jobs preference-weekly --run-id weekly-recheck-20261025 --json
```

POST /jobs/preference/weekly body `{}` 或 `{"run_id":"weekly-recheck-20261025"}`；调用P5的
create_snapshots/周偏好算法，按已确认时区和update_weekday的00:00排他截止；同周后续评分
下周生效。默认ID由周截止+G4绑定组成，重复任务返回原Run，不重复更新候选或归档P。
候选是未读合格主题集合加上已投递未读（PUSHED）；PUSHED不重新入队，日报投递历史不受
影响。snooze到期的SNOOZED与日报一致恢复为QUEUED，报告以 `resumed` 计数。
建议按设计在 update_weekday **20:00、当日日报投递后**运行周任务：这样本周已投递未读也
在候选内；若当日日报已生成同日快照，周任务不会重写该快照，报告复用其中的U，下一自然日
快照继续使用同一周截止。若提前到当日投递前运行，当日稍后才变为PUSHED的条目不进入本次
Run（同run_id幂等，不会事后补录），但仍会由当日日报创建快照。归档/特殊桶始终不产生P。
Run报告含完成/评分数、覆盖率/弱信号、修订数、非法评分/标签、孤立评分、恢复数、
快照数量和U/置信度；null不作负分，覆盖率<30%弱化。
非法评分数据返回failed报告且不写候选快照，CLI非0；历史缺阅读方式/空标签仅警告，
保留P1可选字段兼容。原始Rating和报告不被自动修复、删除或覆盖。

反馈优先使用归档时作者/venue/标签，旧归档缺完整内容才用既有P5元数据回退；只SQL投影
这些字段，不读取Rating.note、ArchiveSnapshot.note或完整归档正文。备注从不传入模型。
任务尊重G4与暂停/安静时段；应用启动不自动调度。可由外部调度器在已确认时段调用CLI，
本阶段不启动常驻进程/采集/真实LLM。任务报告可用 `papersteward runs show --last` 查看。
