# ADR 0012：G4 约束下的采集与运行报告

- 采用，2026-10-22，P3-S7；无新增依赖。S7 按实现任务拆为 A（过滤/来源表）、
  B（编排/CLI/阶段门），同一独立S7分支提交，超过8文件/400行在handoff说明。
- 迁移0005仅新增 paper_source，保存每条原始来源版本和候选JSON；指纹由source/id/
  raw metadata内容哈希生成，重跑不重复，更新版追加。外部session_scope提交，内部
  savepoint保证审计/状态写入失败不留半批文献。downgrade仅删来源表，paper/run保留。
- 硬过滤配置取已选Profile的可选 ingestion 对象；默认不凭未知语言/日期/类型拒绝，
  已知类型限article/preprint/review。排除项为字面短语，默认继承profile.exclude；
  黑名单/predatory清单须用户显式提供，不编造在线名单。日期豁免需显式classic_ids，
  不自动提名经典桶。未知元数据留待P4复核，不伪造数据。
- 生产与dry-run均读取G1/G2/G4精确绑定，通过read_preview复核G3/报告/反馈和选定
  scoring/runtime配置；暂停和安静时段在任何来源调用前拒绝，提交前再验证。
  不执行模型/推送，故模型隐私/推送配额不被采集代码替代或篡改。
- 每来源独立失败；已完成查询可保留，unsupported_query跳过该查询，其余来源继续。
  RunStatus维持P1枚举，部分成功用stats.partial；全部来源失败为failed。
  明确的运行报告保存新增/重复/过滤原因/来源错误/请求/耗时/选定版本；不记录密钥。
- 同库先按来源身份及去重指纹匹配，保留库内ID。只更新由ingest创建且仍在
  discovered/deduped/screened的元数据，追加paper.metadata_updated；种子/校准记录与
  scored/archived/rejected等均不改写、不恢复，仍可追加新来源版本。
- new Paper 从discovered→deduped→screened/rejected，不入队/评分/归档。
  每次最多1000候选、20查询/来源；CLI live需显式--live，fixture绝不联网。
  --dry-run不写Run/文献/事件/来源记录；preflight门禁拒绝尚不算启动运行。
- 回滚：停采集、备份DB，downgrade0004会丢来源溯源，paper/run/审计仍留存；
  恢复完整旧行为/数据需恢复S7前备份，不能仅降迁移撤销已采集文献。
