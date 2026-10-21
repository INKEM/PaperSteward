# ADR 0008：确定性档案查询

- 采用，2026-10-22，P3-S2；无依赖/迁移。
- QuerySpec 向后兼容扩展 operator(any/all)、exclude_keywords、field(all/author/venue/team)。
  同一查询内关键词按 OR/AND 连接，排除项为完整字面短语；适配器负责转义与来源编译。
- 只消费 G1 明确选定 confirmed 档案，并重新解析正文核对缓存；不读取别的档案 latest。
- 按优先级稳定排序子方向关键词组，再补 methods/data 词组和 authority 作者/venue/团队。
  去空白、组内去重、查询去重，保留原顺序；预算截断。自由文本核心问题不臆造关键词，
  没有结构化检索输入明确报错；methods/data 为非列表时明确拒绝，避免把文本拆成字符。
- exclude 每条完整保留为字面排除短语，不把自然语言规则猜成单个词；硬过滤仍由 S7
  明确设置规则执行。生成查询不会修改 Profile/G1，也不代表来源支持全部字段。
- 网络、embedding、引文扩展、LLM 查询扩写不在本阶段。回滚代码即可，无持久化影响。
