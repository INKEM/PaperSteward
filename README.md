# PaperSteward

把文献发现、阅读队列和读后反馈放在一起的个人工具。本地保存研究方向、论文摘要、评分和备注，帮你每天挑出值得读的几篇论文。

支持 arXiv、OpenAlex 和 Semantic Scholar 采集，100 字概括、主题分类、可解释排序、日报、阅读归档与导出；还可浏览综述、远邻和经典文献，保存自己的排序视图。

当前为 Beta；下一步改善安装与浏览器首用体验。

## 安装与启动

需要 Docker Compose，或 Python 3.11+。当前请从源码安装；也支持从本仓库安装到 pipx：`pipx install .`。

```bash
git clone https://github.com/INKEM/PaperSteward.git
cd PaperSteward
docker compose up --build
```

打开 [本地页面](http://127.0.0.1:8000/ui/today)。容器启动前显式迁移数据库，数据保存在命名卷中。`docker compose down` 停止服务；保留数据时不要加 `-v`。

Python 源码方式：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
.venv/bin/papersteward start --data-dir ./var
```

安装依赖需要联网。默认使用离线模型替身；处理真实论文前请先[配置模型](docs/llm-provider.md)。

## 第一次使用

1. 用[初始化指南](docs/onboarding-cli.md)创建并审阅研究方向、分类法、校准报告和试运行日报，逐步显式确认。
2. 按[采集指南](docs/ingestion-cli.md)获取候选，按[处理与审核指南](docs/process-review-cli.md)生成概括、分类和评分，解决待审核项。
3. 按[日报与归档指南](docs/delivery-cli.md)生成日报，然后在浏览器阅读、评分、写备注和归档。

当前初始化和采集处理主要通过 CLI 完成，浏览器首启向导正在准备。示例与合成资料只验证流程，不代表真实模型质量或你的研究偏好。

## 日常阅读

- [今日](http://127.0.0.1:8000/ui/today)：生成日报、记录阅读反馈。
- [阅读队列](http://127.0.0.1:8000/ui/queue)：查看合格候选与[评分解释](docs/priority-queue.md)。
- [特殊桶](docs/shelves.md)：综述、远邻、经典按档位浏览。
- [排序视图](docs/views.md)：按自己的权重临时重排；[全局权重变更](docs/config-governance.md)需要预览与确认。
- [归档](http://127.0.0.1:8000/ui/archive)：搜索读过的论文，导出 BibTeX、CSV、Markdown 或 JSON。

## 配置、隐私与排障

配置使用 `.env` 或 `PAPERSTEWARD_*` 环境变量，环境变量优先；示例见 [.env.example](.env.example)。默认只绑定本机，运行页面无需 CDN，不收集遥测。

真实来源采集与模型调用需要明确授权；API key 只放本地配置，阅读备注默认不发送到模型。邮件必须在初始化中明确选择并配置后才能手动发送。

启动会显式升级迁移后运行服务；`--no-browser` 关闭自动打开，`--port 8001` 更换端口，`--data-dir 新目录` 创建独立工作区。源码安装也可用 `make start`。

启动失败先运行 `.venv/bin/papersteward doctor`；它只诊断，不迁移。若报告迁移落后，停止服务、备份数据，再运行 `.venv/bin/alembic upgrade head`。端口占用可用 Uvicorn 的 `--port 8001`。不要删除数据库或投递记录来重试未知送达的邮件。

目前面向单用户本地使用；没有多用户登录、PDF 全文解析或外部笔记同步。不会因打开页面自动采集或发送邮件。

## 许可证

[MIT](LICENSE)。随包分发的 HTMX 保留其 [0BSD 许可证](src/papersteward/static/HTMX-LICENSE)。
