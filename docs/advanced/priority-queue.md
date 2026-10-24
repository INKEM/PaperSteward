# 持久阅读队列与评分

日常流程：手动挖掘 → 持久阅读队列 → 随时阅读。挖掘完成自动调用 `refresh_queue(session, now)`，生成当日评分快照并把合格论文推进到 queued，无需日报。

`read_backlog(session, now=..., filters=...)` 读取 queued / pushed / reading 的最新可用快照，不要求日期等于今天，不应用每日配额、历史延期或冷却。默认展示未读，可筛选已读/全部、标签和 0–100 分范围。已完成和不感兴趣的论文不会重新入队。

- `GET /queue` 或 `GET /backlog`：持久队列，支持分页。`/backlog` 还支持 `tag/min_priority/max_priority/read_status`。
- `GET /queue?date=2026-10-22`：保留按日期读取的排查能力。
- `GET /scores/{paper_id}/explain`：最新快照；传 `date` 可查看特定日期的解释。
- 队列页面的“刷新排序”发起 `ui.refresh`，只做本地计算；挖掘的 `ui.sync` 也会自动刷新。

日快照继续保持不可变：同一天已有评分幂等复用，新论文可追加；次日刷新会重新计算。页面显示快照日期，名次按 rank_score 及稳定的并列规则计算，综合评分显示 P。评分解释面向读者展示分项与原文证据；完整技术字段仍保存在 JSON API 中。

合格条件保留：唯一有效主标签与主题归属、有摘要文本、相关度通过门槛、静态快照与版本一致。低置信度和摘要质量标记不阻塞。特殊桶使用独立视图，不与主队列混排。

旧 snoozed 论文在迁移 0018 恢复 queued，历史事件保留。完整回滚使用升级前备份，降迁移不会猜测并恢复旧延期状态。

高级排查仍可执行：

```bash
papersteward scores snapshot --date 2026-10-22 --json
papersteward queue --date 2026-10-22 --json
papersteward score explain p001 --json
```
