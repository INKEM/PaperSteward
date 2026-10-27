# UI 组件清单与使用约定

批次 A–F 沉淀的可复用组件（UXR-21）。组件 = Python 渲染函数 + CSS 类，不引入前端框架。
新增页面优先复用以下组件；修改组件时同步更新本清单。

## 页面外壳与布局

| 组件 | 生成位置 | 用途 |
|---|---|---|
| `page(title, body, nav=, heading_actions=)` | `api/html.py` | 全站外壳：主导航（工作区切换器 + 阅读队列/归档/探索▾/首次设置/设置/帮助）、标题区、`#network-alert` 提示条、页脚。`nav=` 显式声明所属导航项（动态标题页必传）；`heading_actions=` 放标题行右侧任务工具条 |
| `#workspace-switcher` + `_switcher_fragment()` | 外壳占位 + `api/workspace_pages.py` | 顶栏工作区切换器：htmx 懒加载片段（`hx-trigger="load"`，桌面导航首位；移动端导航用「工作区管理」链接代替），`<details class="nav-group">` 同构「探索▾」；行内 `POST /ui/workspaces/switch`，当前项 `aria-current` + ✓，运行中加 `.ws-badge` |
| `.ws-list` / `_workspace_row()` | `api/workspace_pages.py` | 管理页工作区列表行：名称/创建时间/运行徽章 + 切换与删除动作；当前行标注「当前使用」并给重置入口 |
| `.page-heading` / `.queue-toolbar` | `workflow_pages.dashboard` | 队列页标题行 + 「挖掘新论文/采集参数/刷新排序」工具条 |
| `fragment_error(container_id, message)` / `error_fragment(exc)` | `api/html.py` | htmx 片段错误：保留稳定容器 id，`role=alert` 并接管焦点 |
| `#network-alert` + `setup.js` htmx 事件 | 外壳 + `static/setup.js` | 网络失败/服务错误全局提示条，成功请求后自动收起（UXR-20） |

## 队列与论文卡

| 组件 | 生成位置 | 用途 |
|---|---|---|
| `_card()` | `api/pages.py` | 阅读卡：徽章行 → 标题 + `.score-pill` 推荐参考分 → `.card-meta` 元信息 → `.card-summary`（3 行截断）→ `.card-actions`（记录阅读/不感兴趣） |
| `.queue-filters` | `pages._backlog_page` / `archive()` | 筛选条：桌面单行 flex，手机双列网格；高级字段收进 `.adv-fields`（更多筛选芯片） |
| `pagination(base_url, params, offset=, total=)` | `api/html.py` | 分页条 `<nav class="pagination">`：上一页/下一页均保留筛选条件；空时不输出 |
| `.empty` 空状态 | 各页面 | 按原因给直接行动（清除筛选/挖掘引导），不只写文字 |
| `queue_stats_line()` | `workflow_pages` | `<p id="queue-stats" role="status">` 未读总数统计，动作响应经 `hx-swap-oob` 同步 |

## 表单

| 组件 | 用途 |
|---|---|
| `.stacked-form` | 「正文为主」表单（材料/提意见/阅读反馈）：标签与控件纵向，正文占满宽度 |
| `.settings-form` | 「多短字段」表单（模型连接）：最多两列，长字段占整行，低频项进 `.advanced` 折叠 |
| `_form(..., context=)` | `api/pages.py` 阅读动作表单：`hx-post` 原地换卡片，`context=detail` 时返回完整详情正文（内容不丢） |
| 失败保留 | 原生表单原地重渲染带已填内容（X-01）；htmx 表单失败返回错误卡/详情正文，备注保留（UXR-09/20） |

## 详情页

| 组件 | 生成位置 | 用途 |
|---|---|---|
| `_detail_article()` | `api/pages.py` | 详情正文：返回入口（按归属）→ 徽章 → 元信息 → 模型概括（标注）→ `.summary-slots` 四槽 → `.source-material` 摘要/证据（折叠）→ 记录阅读 → `.score-details` 推荐依据（折叠） |
| `_summary_slots()` / `_source_material()` / `_model_summary_figure()` | `api/pages.py` | 内容概括四槽（缺失显示「暂无」）；原始摘要与证据折叠；模型概括标注 |

## 审阅与运行

| 组件 | 生成位置 | 用途 |
|---|---|---|
| `.review-overview` | `setup_pages._review_overview` | 草案速览：定位一句话、数量统计、跳转锚点（`#setup-feedback` / `#setup-confirm`） |
| `draft_card()` | `api/profile_view.py` | 草案卡：变化摘要（默认展开）→ 渲染视图 → Markdown 源码 → 原始差异（折叠） |
| `run_body(run, card=, current=, home=)` | `workflow_pages` | 运行进度/报告：终态结果摘要 + 下一步 + 三段计数 + 折叠明细（质量标记/检索式/来源失败）；`stopping` 状态显示「正在停止」 |
| `queue_stats_line(…, oob=)` | `workflow_pages` | 顶部统计（`role="status"`），动作响应带外同步 |

## 探索与排序

| 组件 | 生成位置 | 用途 |
|---|---|---|
| `.preset-card` | `pages._view_links` | 排序预设卡片（含情境说明与「当前」标注）；公式收进 `.formula-details` |
| `_shelf_section()` / `_shelf_inner()` | `api/pages.py` | 探索收藏：说明段、提示性周上限文案、空收藏引导、档位与证据 |
| `_view_table()` | `api/pages.py` | 排序表：短中文表头 + 单元格 `data-label`（窄屏卡片化字段名） |

## 导入已读论文

| 组件 | 生成位置 | 用途 |
|---|---|---|
| 归档页头部入口「导入已读论文」 | `pages.archive()`（`heading_actions=`） | 低频动作不进主导航；归档空态文案带同一入口链接 |
| `/ui/import` 两步表单 | `api/import_pages.py` | 第一步标识可选（留空直接手填）→ 第二步确认元数据+评分（`.stacked-form`）；解析失败/未命中降级手填并给 `.notice` 说明；解析溯源经 `hidden()` 字段带入确认步；校验错误 X-01 原地重渲染（400） |
| `<span class="tag">导入</span>` 徽标 | `pages.archive()` 列表 / `_detail_article()` 详情 | 判定 `content_json.origin == "import"`；导入论文无主标签时不渲染标签项 |
| 导入冲突页 `_conflict_page()` | `api/import_pages.py` | 重复导入 409：守卫文案 + 「打开论文详情页」链接 + 返回导入/归档 |
| 导入成功通知 | `pages.detail(imported=1)` | 303 到详情页带 `?imported=1`，`_detail_article` 顶部渲染成功提示 |

## 阅读回顾

| 组件 | 位置 | 约定 |
|---|---|---|
| 回顾次级导航 `insights_navigation()` | `api/insights_pages.py` | 本周／本月／全部历史／已保存回顾／手工资产共用入口，仍归属主导航“归档”；窄屏换行 |
| 周期筛选与明细 | `insights_pages._filter_form()`／`_section_block()` | 原生 GET 表单，可键盘提交；切换周期禁用无关日期字段；历史标签携带分类版本；分页保留模式、日期与主题，各明细组可独立展开分页 |
| 全部历史完整性 | `_all_history_body()` | 全部归档来源及日期精度，当前资产入口；不输出长期画像；分页不截断统计分母 |
| 专题资产 | `_scope_assets_html()` | 按活动当时的来源版本归属，跨主题计一次并标明；显示范围内版本，避免改来源后漂移 |
| 报告快照与变化提示 | `_report_detail_body()` | 正文来自冻结快照，按持久范围与有效输入清单比较；范围外变动不标为失效；旧范围未知时明确提示；回到论文保留上下文 |
| 报告系列与比较 | `_report_detail_body()`／`list_series_reports()` | 完整系列身份在数据库先筛选再分页；跨范围比较返回可操作错误；历史标签不在现分类表时仍显示已选历史值 |
| 统一导出选择 | `api/insights_export_pages.py` | 默认事实分享；私人完整副本下载前勾选说明；报告私人副本保持冻结；全部历史私人副本为含修订的 JSON |
| 资产表单与删除范围 | `_asset_create_form()`／`_asset_detail_body()` | 新建或冲突错误就近显示，完整草稿保留并转义；正文副本报告的删除复选框必须位于删除表单内 |
| 阅读日期与本地数据管理 | `read_time_block()`／设置与工作区删除页 | 日期独立于归档；清空预览明确列出日期声明、报告、资产版本及来源；同一备份保存全部表 |
| 知识版回顾入口与预览 | `synthesis_page()`／`_synthesis_page_body()` | 与阅读分析并列的次级入口；范围、模式、材料类别、排除与授权同一页；先预览清单/覆盖/省略，再显式提交。窄屏可换行，不把私人草稿放进 URL |
| 知识整理报告正文 | `_synthesis_report_html()` | 专题并列支持/限制材料与条件对照、开放问题、候选联系；私人来源就地标注；「实际分析覆盖」表披露使用/清单、缺失、省略、拒绝与部分失败；未知版本不按旧格式解析 |
| 私人归纳导出选择 | `api/insights_export_pages.py`／`export_report()` | 分享副本按来源链整条排除私人归纳并显示类别；个人副本显式勾选；「另存脱敏版本」清除私人归纳并保留公开条目，原报告哈希不变 |

回顾沿用全局 Ctrl／⌘+Enter 提交。打印隐藏导航、筛选及写入按钮，保留表格列头与事实正文。

## 保留的全局基础层（有意为之）

`article`/`form`/`table`/`details` 的基础样式仍是全局规则——它们是「基线层」，让未
沉淀组件的页面保持可用；组件类在其上覆盖。按计划 §6.5 不为令牌化一次改遍全站：
新页面/新组件必须使用上表组件类，仅当某全局规则被 ≥3 个组件各自覆盖时才考虑
把该全局规则下沉为基类。
