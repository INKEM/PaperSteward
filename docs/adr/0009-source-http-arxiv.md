# ADR 0009：HTTP 边界与 arXiv

- 采用，2026-10-22，P3-S3。先记录依赖决策，再提升 httpx>=0.27,<1.0 为 runtime；
  不增加新第三方包，复用现有 httpcore/certifi/idna 等依赖。urllib 可替代，但缺少统一
  可注入 transport/timeout；不引入 tenacity、feedparser、录制插件，避免额外体积与维护。
- 公共同步 HTTP client：固定 HTTPS API 端点、禁用代理/重定向、显式超时、User-Agent、
  请求总预算（重试也计数）、串行最小间隔、有限指数退避、429/5xx/网络错误映射。
  Retry-After 超过 60 秒时停止该次调用留待下次运行，不提前重试；成功响应仅内存缓存，
  TTL 24h、最多128项。缓存不保存认证头，不写磁盘。响应上限8MiB。
- arXiv 使用 Atom、start/max_results 分页、submittedDate 日期闭区间、原始 published
  作为首次公开日；版本 ID 留在 provenance.source_id，canonical arxiv_id 留待 S6。
  不支持 team 字段或含双引号/反斜杠/控制符的字面值时显式 unsupported_query，
  不将用户词当原生查询语法。venue 用 jr，author 用 au，默认 all；OR/AND 与 ANDNOT。
- XML 拒绝 DTD/entity 声明、错误 feed/无效必填字段；保留 entry 原始 XML。整批失败
  不返回半批结果；请求预算耗尽为 budget_exceeded。没有 PDF 下载/来源持久化/调度。
- 测试用合成 Atom、httpx.MockTransport、固定 clock/sleep，默认无外网；未进行 live。
- 官方依据（2026-10-22 查阅）：[arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)、
  [HTTPX transport](https://www.python-httpx.org/advanced/transports/)。API 使用保守3秒间隔。
- 回滚：撤销代码与 runtime 依赖变更，无 DB migration。真正多进程限流/持久缓存留后续。
