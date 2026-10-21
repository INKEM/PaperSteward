# P3 采集与运行报告

先按 [初始化 CLI](onboarding-cli.md) 完成 G1–G4，并显式迁移到 `0005`。采集始终复核
所选档案/分类法/校准与运行参数；暂停或安静时段会在来源调用前拒绝，不自动修改门禁。

```bash
# 离线 fixture；数据库必须是已完成初始化、已迁移的本地库
papersteward ingest --source arxiv --fixture tests/fixtures/sources/arxiv.xml
papersteward ingest --source openalex --fixture tests/fixtures/sources/openalex.json --query retrieval
papersteward ingest --source semantic_scholar --fixture tests/fixtures/sources/semantic_scholar.json

# 多来源 fixture，全部仍为离线；每个来源可以独立失败
papersteward ingest --source arxiv --source openalex \
  --fixture arxiv=tests/fixtures/sources/arxiv.xml \
  --fixture openalex=tests/fixtures/sources/openalex.json --query retrieval

# 仅预览，不写文献/来源/事件/运行表；同样需要 G4 和有效运行时段
papersteward ingest --source fake --fixture tests/fixtures/sources/basic.json --dry-run

papersteward runs show --last
papersteward runs show RUN_ID

# 显式手动允许真实来源请求；离线验收不执行这条
papersteward ingest --source arxiv --live --query retrieval --max-results 20 --dry-run
```

`--query` 是单个字面关键词或短语，不接受来源原生表达式；省略时按已确认档案生成
多条关键词/关注列表查询。真实请求需明确 `--live`，不会自动注册账号或申请密钥。
可选 `PAPERSTEWARD_SEMANTIC_SCHOLAR_API_KEY` 仅以该来源的请求头发送；fixture 不使用它。
客户端不跟随论文链接、下载 PDF 或执行文本指令。

报告始终为 JSON，包含 `status`、`stats_json`、`errors_json`。`new` 是新增文献行数，
包括被硬过滤后保存为 rejected 的记录；`deduplicated` 是本批合并数，`existing` 是
匹配已有库内文献的组数，`filtered` 保存逐条理由。`source_versions_added` 为新来源版本数。
`partial=true` 表示部分查询成功、另有来源错误（包含不支持的字段查询），不伪装成全成功。
全部来源失败或持久化失败退出非0；持久化失败撤销半批文献，保留脱敏的 failed 运行报告。
门禁/输入 preflight 拒绝尚未启动运行，不写 Run。重复运行使用新的 Run ID，文献和相同
来源内容保持幂等；同 Run ID 明确拒绝覆盖。dry-run 的 `preview/would_create` 仅为预览。

硬过滤来自 Profile front matter 的可选字段，**须在确认 G1 前纳入档案**，例如：

```yaml
ingestion:
  languages: [en, zh]
  document_types: [article, preprint, review]
  date_from: 2020-01-01
  exclude_terms: ["advertisement"]
  blocked_authors: []
  blocked_venues: []
  predatory_venues: []
  classic_ids: ["10.0000/explicit-fixture-only"]
```

不提供 exclude_terms 时继承 profile.exclude 的完整字面短语，不推断自然语言规则。
未知元数据保留 None/unknown，不伪造语言/日期；classic_ids 只豁免日期，不创建经典桶。
ACTIVE 后无再协商/reset 实现，不能手改所选版本绕过证据校验。

每来源最多20查询、HTTP最多30请求（含重试）、单次至多1000候选。来源失败有错误码；
arXiv team、Semantic Scholar 字段关注列表等尚不支持的查询会明确报告。摘要相似采用
词法shingle，不具备跨语言语义去重能力；未进行 live 或质量召回率验收。

新文献最多到 screened/rejected；不分类、评分、入队、推送或归档。来源 raw metadata
以内容指纹追加到 paper_source；已归档/已拒绝不恢复，已评分/种子/校准文献不自动改写。
完整撤销采集须恢复运行前备份；降级0004只删来源表，不撤销 paper/run/event。
