# P6 本地实现验证记录

日期：2026-10-22。范围：P6-S1→S7，严格顺序、每卡单独分支/提交后才开始下一卡。
本记录由实现方整理，**不是独立验收结论**；远程PR/CI、独立验收与阶段tag尚未执行。
本地阶段门全部通过；逐卡基线见[P6执行文档](P6-delivery.md)。

## 交付与冻结契约

- 日报：只消费P5合格主题队列，最多7篇、每标签2、候选允许时至少2标签、饥饿槽、
  精读2/粗读5；精读溢出延期，可显式降级。清库存优先首次入队较早文献。
- 渲染：完整卡片/链接/建议/解释，MD和HTML golden；特殊桶空占位，预留≤2（完整规则P7）。
- 投递：日期唯一正文，日期+渠道唯一预约；文件0600原子不覆盖；G4、时区/今日、
  push_time/暂停/安静/渠道门禁。SMTP预约先提交，未知结果不自动重试、跨日也保守去重。
  本地文件和邮件独立；旧G4本地授权不会隐式启用SMTP。
- 阅读：done/score/note/snooze/skip/reject，严格整数、客户端全局请求ID回执、冲突拒绝；
  用户主动阅读沿状态机记录user_read，绝不声称真正发送过邮件。
- 归档：done、Rating、完整快照、archived和审计同事务；冻结元数据/摘要证据/标签桶与
  徽章/建议实际方式/完整S/D/U/P解释、配置/模型版本。方式覆盖有事件；评分备注修订追加。
- 导出：BibTeX/CSV/Markdown/JSON包含标签、评分、备注、链接和修订；CSV防公式，文本转义。
- 页面：`/ui/today`、`/ui/queue`、`/ui/papers/{id}`、`/ui/archive`；本地HTMX、CSP、
  同源表单保护；浏览器和真实Uvicorn/CLI完整闭环均已实测。
- 周任务：复用P5，选定周截止排他、每周幂等报告/显式重试、覆盖率检查与弱信号；
  只给未读合格候选追加快照。优先SQL投影阅读时元数据与标签，不查询备注或完整归档正文。

冻结：`delivery/contracts.py` 的Digest/DigestCard/Action/ArchiveRecord；现有P5
QueueItem/ScoreExplanation/RankConfig保持不变。无新Python运行依赖；本地HTMX采用0BSD，
版本与SRI见ADR0017。新增事件枚举只追加，数据库事件列本身无枚举CHECK，无需重写旧迁移。

## 验证证据

| 检查 | 结果 |
|---|---|
| make check | **939 passed / 4 deselected**；ruff格式/lint、strict mypy **120**源文件通过 |
| make test-integration | **297 passed**；包含跨日未知投递与有数据迁移回归 |
| make e2e | 4通过，真实Uvicorn + CLI；完整G1–G4→ingest→process/review→queue→digest→HTML表单→archive/export |
| make migrate-check | base→0010→base→0010通过；模型一致性通过 |
| 有数据0010↔0009 | 初始标签/评分/备注/状态保留；完整内容列删除后重建为空且明确标记不可用，不伪造历史 |
| make demo DEMO_PORT=8789 | 独立临时库迁移/seed/health/papers通过，服务已停止 |
| make secret-scan / git diff --check | 通过；最终提交前已核对 |
| wheel | 隔离构建通过；ZIP核对含HTMX/0BSD许可证/CSS，脚本SHA384匹配官方SRI |
| 浏览器 | 独立E2E库副本：选择4分、填写备注、HTMX完成后归档列表可见；临时标签/服务已关闭 |
| Docker | **未验证**：docker.sock权限拒绝；已有sudo被no-new-privileges阻止，未修改权限/安全设置 |
| 远程CI/独立验收/tag | **未执行**，不借用P5的Docker/CI证据声称本阶段通过 |

所有测试SQLite均为pytest tmp_path或mktemp独立临时库（/dev/shm），未触碰用户var数据库。
无真实来源、LLM、SMTP测试；浏览器用合成数据。首轮/tmp SQLite fsync停滞已中止并改用
前序阶段同样的/dev/shm方式，未降低同步级别。继承Starlette/httpx弃用警告，未升级依赖。

复现：

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-audit-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-audit-int'
make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p6-audit-e2e'
TMPDIR=/dev/shm make migrate-check
# demo必须显式提供独立临时PAPERSTEWARD_DATABASE_URL，禁止对用户库运行验收
make secret-scan
git diff --check
```

## 限制与下一步

- 本轮是七个本地stacked提交，未推送/建PR/合并/打tag；P6独立验收与用户第一份真实日报
  审阅仍待执行，不能宣称已发布Alpha或完成Beta入口的真人确认。
- SMTP未知送达必须人工核对，无绕过幂等的重置入口；真实厂商TLS/送达质量未验收。
- 未启动常驻采集/发信/周调度；外部调度器可显式调用CLI/API，不能把打开UI当发送授权。
- P7特殊桶完整档位/轮换/配额、自定义视图、权重确认治理与再协商不在本轮范围。
- 归档删除/撤销、RIS/格式化引用、真实偏好调优/文本信号审核、P8硬化仍未实现。
- P1 F5警告/F3失败usage断言债、P2–P5 tag仍留待相应维护/发布流程。

## 回滚

先停投递与任务、备份数据库/导出。0010→0009删除完整归档内容列，0009→0008删除日报/
投递表；事件、Rating、状态和初始归档字段不会自动撤销。完整回到操作前须恢复备份。
不得把降迁移/删除预约当重发办法；未知SMTP可能实际已送达。旧代码不认识新事件时必须
恢复对应代码版本的数据备份。回退页面/CLI不撤销已保存反馈；既有P5历史快照只读。
