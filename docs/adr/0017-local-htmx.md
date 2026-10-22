# ADR0017：最小阅读页面本地分发HTMX

状态：采纳；2026-10-22。

计划P6-S6要求HTMX薄页面；选择本地固定HTMX 2.0.11（0BSD许可证），不引入Node/Jinja/
multipart Python依赖。依据[官方安装文档](https://htmx.org/docs/)，vendor官方min.js并核对
文档提供的SHA384 SRI：`2OatzQy1H+Zd/IIrjr1TcuDGqLXeHhbooAyJY1KdQMKnr4LZ22k31GBLdYKHmVjg`。
许可证随文件保存，静态资源作为Python package data分发；运行页面无需外网/CDN。

替代：CDN会令本地UI依赖第三方网络；手写fetch会增加交互维护成本；Jinja+HTMX额外
引入模板依赖，当前四页的HTML转义可复用标准库。固定脚本约51KiB，升级需要独立审查。
禁HTMX动态eval、脚本标签执行、跨源请求和history缓存；输出文本全部HTML转义，链接
白名单http(s)；写表单核对Origin/Sec-Fetch-Site，缺Origin时要求自定义同源请求头。
UI仅调用既有服务，不复制评分/归档/门禁；错误显示在卡片，普通HTML表单可同源回退。

回滚：撤销pages路由、vendor及package data，API/CLI/数据库和已归档内容保留。
