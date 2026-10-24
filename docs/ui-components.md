# UI 组件清单与使用约定

批次 A–F 沉淀的可复用组件（UXR-21）。组件 = Python 渲染函数 + CSS 类，不引入前端框架。
新增页面优先复用以下组件；修改组件时同步更新本清单。

## 页面外壳与布局

| 组件 | 生成位置 | 用途 |
|---|---|---|
| `page(title, body, nav=, heading_actions=)` | `api/html.py` | 全站外壳：主导航（阅读队列/归档/探索▾/首次设置/设置/帮助）、标题区、`#network-alert` 提示条、页脚。`nav=` 显式声明所属导航项（动态标题页必传）；`heading_actions=` 放标题行右侧任务工具条 |
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

## 保留的全局基础层（有意为之）

`article`/`form`/`table`/`details` 的基础样式仍是全局规则——它们是「基线层」，让未
沉淀组件的页面保持可用；组件类在其上覆盖。按计划 §6.5 不为令牌化一次改遍全站：
新页面/新组件必须使用上表组件类，仅当某全局规则被 ≥3 个组件各自覆盖时才考虑
把该全局规则下沉为基类。
