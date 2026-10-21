# ADR 0001：使用 PyYAML 解析文档 front matter

- **状态**：接受
- **日期**：2026-10-21
- **阶段**：P1-S3

## 背景

研究方向档案（profile）与分类法（taxonomy）使用 Markdown + YAML front matter
作为人可读、机器可解析的源格式（设计方案 §4.3–4.4）。P1-S3 需要把 front matter
解析成受校验的模型；P1 的文档格式不能改成 JSON 或 TOML，否则会破坏设计契约。

## 决策

引入运行时依赖 **PyYAML 6.x**（`PyYAML>=6.0,<7.0`），并只使用
`yaml.safe_load`。解析器对非 mapping 根节点、YAML 语法错误、闭合缺失和字段
校验失败统一抛出带 `source/line/field` 的 `DocumentError`。

## 替代方案

| 方案 | 不采用原因 |
|------|------------|
| 手写 YAML 子集解析器 | 需要处理嵌套 mapping/list、引号、日期与多行字符串；边界多，安全性与可维护性差 |
| `ruamel.yaml` | 支持 round-trip 注释等本阶段不需要的能力，依赖面更大 |
| JSON front matter | 与设计方案和用户可读性目标冲突 |
| TOML front matter | 同上，且嵌套列表表达不如 YAML 直观 |

## 影响

- 体积：PyYAML 为纯 Python/可选 C 扩展包，常见环境已预装；只增加一个运行时依赖；
- 安全：`safe_load` 不构造任意 Python 对象；文档文本仍视为不可信输入；
- 测试：解析器使用固定 fixture，不访问网络；
- 回滚：移除依赖与 `documents/` 解析器即可；Markdown 源文件不受影响。

## 约束

- 不在 front matter 中执行模板、锚点展开或自定义构造器；
- 解析失败不写数据库；
- 版本升级若改变解析语义，需要新增 ADR 与迁移/兼容说明。
