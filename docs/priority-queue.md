# 优先级快照与队列

先完成 G1–G4、采集、process 与人工审核；评分完全离线，不产生发送或归档。

```bash
.venv/bin/papersteward scores snapshot --date 2026-10-22 --json
.venv/bin/papersteward queue --date 2026-10-22 --limit 20 --json
.venv/bin/papersteward score explain p001 --date 2026-10-22 --json
```

- `POST /scores/snapshot` body `{"score_date":"2026-10-22"}` 生成不可变日快照，并将合格
  classified→scored→queued。重复同日复用，不重复EMA；未来/倒序补算拒绝。
- `GET /queue?date=2026-10-22&limit=50&offset=0` 返回 items/total/limit/offset，读取不写库。
- `GET /scores/{paper_id}/explain?date=2026-10-22` 返回当日分项/权重/贡献/置信度/证据/
  版本；省略日期读取最近日快照。特殊FILING桶只返回excluded_from_priority与原因，无P/rank；
  待审特殊桶提名返回 `pending_special_nomination` 且 `excluded_from_priority=false`。
  已归入特殊桶的档位/配额/轮换见 `papersteward shelf assess|tier|plan`；档位 V 存于
  `special_tier_snapshot`，只用于特殊桶内 high/medium/low，不是 P/rank。
  特殊桶只读展示见 `GET /shelves`、`/ui/shelves` 与 [特殊桶视图指南](shelves.md)；
  自定义排序只重排当前合格队列，见 [自定义排序视图指南](views.md)。
  全局权重变更需先预览/确认，确认后按生效日换用新版本；见
  [权重治理指南](config-governance.md)。
- 尚无快照时队列为空，explain返回404；先执行snapshot。G4未完成返回409；非法日期/分页422。
- CLI默认也输出可读缩进JSON，`--json`可用于脚本；失败返回非0且给出初始化/迁移提示。

合格候选：classified/scored/queued、合法激活主标签与主题归属、无开放review、无待审
特殊桶提名、通过摘要、R≥max(55,选定门槛)、静态快照版本匹配G1/G2/G4。
snoozed/rejected/done/archived与特殊归属不进入队列；待审提名的拒绝/接受后，下一日快照
才重新评估（同日快照不重写）。特殊MEMBERSHIP徽章不排除主题文献。人工批准不会绕过未通过摘要。
主标签改动后若静态分未重算，会明确排除，避免沿用旧分类；静态重算仍走版本化确认流程。

解释包含F/H窗口与份额、U周截止/覆盖率/收缩、EMA原值/±8限制/reset、配置与静态hash。
S/D冻结为一位小数后的中间舍入差额在scores.rounding_adjustment，不能把分项贡献之和
当作未舍入的精确P。默认示例组件算术75.1与P4冻结S后75.2的区别见ADR0015。
P4无review的历史条目未保留R证据，解释明确数据不足、R置信度0，并展示现存摘要证据与
静态breakdown；不会编造丢失的证据。后续完整R证据持久化需单独契约变更。

历史消费：paper.done按主标签统计（包括特殊桶），Rating每篇取周截止前最后修订；
未评分不当低分、不使用校准标注、不读取备注。周更新日00:00为排他截止；同周新评分
下周生效。作者/venue暂使用现有Paper元数据，P6归档后可提供阅读时元数据。
没有实际阅读时H=50，没有评分时U=50。积压超过两周总阅读容量时，正H收益减半。

读取队列/解释允许在暂停或安静时段进行，生成快照仍执行已确认的暂停/安静时段。
当前配置只消费G4精确选定版本；config import保存新版本不等于激活（治理属于P7）。

回滚：无新迁移，head0008；回退P5代码不会删除score_snapshot或撤销已入队状态。
完整回到运行前需停止应用并恢复备份。无日报/调度/推送/阅读操作/归档/自定义视图。
