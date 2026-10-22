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
文件，解决后用相同目标 `--recover-file` 显式恢复。SMTP失败/崩溃后可能实际已送达，
预约/uncertain状态禁止自动重发，须人工核对；无提供绕过SMTP幂等的重置命令。
同日第二渠道可以独立发送；SMTP失败不会阻止本地文件。G4选择local_markdown时不发邮件。

SMTP配置：HOST、PORT（默认465）、FROM、TO、SECURITY（ssl/starttls）、TIMEOUT_S；
可选USERNAME/PASSWORD须成对。所有变量加PAPERSTEWARD_SMTP_前缀；密码SecretStr且不入报告。
未配置时不发邮件；真实测试只手动运行，CI使用替身。旧G4只确认本地渠道的部署不能
直接切到SMTP；需未来再协商流程或在全新初始化G4时明确选择smtp，不手改确认快照。

数据库head0010：0009新增digest/delivery，0010扩展归档content_json；停服务备份后
降0009只删除完整归档内容列，降0008删除日报与投递记录。
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
用户主动完成队列文献会沿状态机写user_read触发，paper.pushed不表示发过邮件。
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
