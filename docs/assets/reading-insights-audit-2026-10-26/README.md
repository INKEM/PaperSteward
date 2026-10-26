# 第一阶段独立复核：补充边界探针

基线：`3a351b4`（2026-10-26）。所有内容为虚构测试资料，不打开用户论文库或模型服务。

```bash
.venv/bin/python docs/assets/reading-insights-audit-2026-10-26/audit_probes.py
```

脚本在 `/dev/shm` 中新建独立 SQLite 文件库，以 metadata 建表，结束后清理临时库。正式 Alembic 路径由本轮全量测试和迁移检查另外覆盖；需要当前环境具有内存盘临时目录写权限。

- [复现脚本](audit_probes.py)：6 组审计探针。
- [本轮原始输出](probe-results.json)：6 组 `reproduced` 均为 true，表示发现未修复问题，不是 6 项功能验收通过。
- [验收结论与返工条件](../../reading-insights-phase1-acceptance.md)：逐项影响及第二阶段前置批次。

探针用于记录基线行为，不能替代修复后的正式回归测试。后续应分别补成断言正确行为的契约、集成和页面测试；脚本异常退出或没有输出不能解释成问题已修复。脚本直接调用服务和表单解析器，不宣称完成了新的可视浏览器走查。
