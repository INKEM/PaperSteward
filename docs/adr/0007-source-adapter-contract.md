# ADR 0007：来源候选与离线回放边界

- 状态：采用（P3-S1，2026-10-22）
- 背景：P1 Paper 已冻结；来源尚未去重，不能预先分配 canonical ID 或生产状态。
- 决策：同步 `SourceAdapter.search(QuerySpec) -> list[PaperCandidate]`，与现有同步
  仓储/CLI 一致。每次调用自行处理分页并遵守 max_results；空结果返回 []，可预期
  失败统一 SourceError(source/code/retryable)，不返回半批成功，跨源部分成功由 S7 记账。
- QuerySpec 使用有序关键词、可选日期闭区间、1–1000 结果上限。来源查询编译属于
  S2/S3–S5；关注列表/布尔扩展届时显式扩展，S1 不伪造生产查询能力。
- PaperCandidate 的 Paper 同名字段遵循 P1 语义；额外 language/document_type/
  citation_count 供后续过滤/富化消费。来源 ID 独立于库内 ID；不设状态、评分、分类。
  published_at 为来源报告的最早公开时间，未知为 None，不拿检索时间补齐。
- SourceProvenance 记录 source/source_id/url/retrieved_at/raw_metadata，独立于
  模型溯源 core.Provenance。时间须带时区并转 UTC；原始元数据只放记录内容，不放
  请求头/认证/用户备注；论文内容为不可信数据。文本链接不被执行或自动请求。
- Pydantic 拒绝未知字段、空白必填文本、负引用数/非法日期；缺失元数据用 None，
  作者用 ()，OA 用 unknown。模型冻结外层，嵌套 JSON 仍为普通容器；回放每次重新
  校验产生独立对象，消费者不得就地修改 provenance。
- cassette v1 使用规范化 items 与可选 error/retryable，必须同一来源。Fake 记录查询，
  按文件顺序截断，不执行关键词/日期检索、不分页/重试/联网。真实 HTTP cassette
  由各来源阶段补充，不能把规范化 fixture 当真实解析证据。
- 替代方案：直接返回 Paper 会混淆身份、丢失来源；原始字典缺校验；异步接口会增加
  当前同步调用链成本，暂不采用。无新增依赖、持久化或 P1 契约变更。
- 后果：来源可独立离线验证。S6/S7 负责 canonicalization、来源持久化迁移、状态与
  事务；S1 的 API e2e 仅在测试中投影并写入 discovered Paper，不是生产采集入口。
- 回滚：撤销本次代码/文档即可；数据库 head 仍为 0004，无用户数据变更。
