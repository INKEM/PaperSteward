# ADR 0010：Semantic Scholar 来源

- 采用，2026-10-22，P3-S5。使用官方 bulk search、token 分页、短语与 +/|/- 布尔操作，
  日期闭区间；只支持 all，其他字段明确 unsupported_query，由其他来源补充召回。
- 请求 title/abstract/authors/venue/publicationDate/externalIds/citationCount/isOpenAccess/
  openAccessPdf/publicationTypes/tldr/url。TLDR 只留 raw metadata，不能冒充原始摘要。
  年份缺精确日期时保持 None，不按1月1日制造新鲜度。
- HTTP client 可接收可选 SecretStr api_key，仅 Semantic Scholar 时设 x-api-key 头；
  不放 URL/cache key/raw metadata，不记录请求头。无 key 时使用公共接口，401/429
  为来源失败，绝不自动借用用户账号或真实 key。离线测试只用合成 key。
- 不增加依赖/迁移，不下载引用列表/全文。原始 provenance 仍为完整单条返回元数据。
- [官方依据](https://api.semanticscholar.org/api-docs)（2026-10-22 搜索索引读取官方说明；
  原网页为客户端渲染）；未进行 live。回滚代码即可，DB head仍0004。
