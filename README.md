# PaperSteward

每天挑几篇值得读的论文，读完留下自己的判断。

PaperSteward 是单用户、本地优先的文献阅读工具：采集 arXiv、OpenAlex、Semantic Scholar，生成概括与分类，按研究方向安排阅读队列，记录评分、备注和归档。首次设置和日常操作都在浏览器完成。

当前为 Beta。真实论文处理需要一个 OpenAI 兼容的模型服务；没有模型 key 时无法生成研究档案或处理论文。

## 三步开始

### 1. 启动

Docker：

```bash
git clone https://github.com/INKEM/PaperSteward.git
cd PaperSteward
./scripts/docker-up.sh
```

该脚本使用 `docker-compose.hostnet.yml` 覆盖构建网络并调用 legacy builder：部分主机的
Docker bridge 在构建阶段没有默认路由，host 网络可以直接访问 PyPI。普通 Docker 环境
仍可直接用 `docker compose up --build`。

Python 3.11+（pipx 或虚拟环境）：

```bash
git clone https://github.com/INKEM/PaperSteward.git
cd PaperSteward
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/papersteward start --data-dir ./var
```

`start` 会先迁移数据库，再启动服务并打开浏览器；`--no-browser` 关闭自动打开，`--port 8001` 换端口。数据保存在 `--data-dir` 指定的目录，Docker 使用命名卷。停止用 `Ctrl-C` 或 `docker compose down`，不要加 `-v`。

### 2. 在浏览器连接模型

打开 [首次设置](http://127.0.0.1:8000/ui/setup)，第一步填写：

- API 地址（OpenAI 兼容根地址，例如 `https://api.deepseek.com/v1`）
- API 密钥（本机模型可留空）
- 模型名（例如 `deepseek-chat`、`gpt-4o-mini`）

可以点 **测试连接** 验证地址与密钥。保存后，配置写入本机工作区数据库，**保存即表示同意把论文标题与摘要发送给该模型服务**，用于分类、概括和评分。不需要编辑 `.env`，也不需要重启。

### 3. 丢材料，让 Agent 整理，提意见直到满意

粘贴一段描述研究方向的文字，或上传 `.md` / `.txt` 文件，可以分多次添加：

- 个人主页简介、研究计划、开题报告
- 代表性论文摘要、平时笔记、基金申请书

点 **让 Agent 生成研究草案**。后台任务会生成两样东西：

1. **研究方向档案**：定位、核心问题、子方向、纳入/排除标准、阅读节奏
2. **分类标签**：5–9 个带定义、纳入/排除与正例的主标签

在审阅页切换 **渲染视图 / Markdown 源码**，查看与上一版的差异。不满意就直接写意见，例如“方向太窄，把 XX 也加进去”“不要子方向 S3”“更重视评测”。Agent 会生成新版本，直到你点 **确认，开始使用**。确认会把档案与分类法绑定到当前版本，并写入默认运行参数。

完成后直接进入[今日](http://127.0.0.1:8000/ui/today)，点 **同步 / 处理** 开始采集第一批论文。

## 每天怎么用

在“今日”页同步 / 处理：检索词默认取自你确认的研究子方向关键词，可临时改。随后 **处理审核 → 生成日报 → 阅读并记录反馈**。

运行进度会保存，关闭页面后可回来查看；失败时保留已完成数据，检查原因后显式重试。阅读队列可查看评分解释；特殊桶浏览综述、远邻和经典；自定义视图按自己的偏好重排。读完的论文可搜索，并导出 BibTeX、CSV、Markdown 或 JSON。邮件只通过显式 CLI 操作发送。

## 从旧版本升级

- 已完成初始化的旧工作区升级后**保持可用**，已确认的档案、分类法和运行参数原样保留。
- 旧版把模型配置放在 `.env` 时，升级后到 **设置** 页保存一次即可（保存也是数据外发授权）。之后同步不再需要每次勾选。
- 旧版向导走到一半的工作区会进入新流程；页面会提示把遗留档案/分类法 **导入为材料**，重新生成草案。
- 升级前停止服务并复制完整数据目录；迁移失败时保留日志，参考[排障](docs/troubleshooting.md)。

## 配置与帮助

- [快速开始](docs/quickstart.md)、[配置与工作区](docs/configuration.md)、[模型接入](docs/llm-provider.md)
- [隐私与数据流向](docs/privacy.md)、[排障](docs/troubleshooting.md)、[常见问题](docs/faq.md)
- 高级用法：[采集](docs/advanced/ingestion-cli.md)、[处理与审核](docs/advanced/process-review-cli.md)、[日报与归档](docs/advanced/delivery-cli.md)
- [评分解释](docs/advanced/priority-queue.md)、[特殊桶](docs/advanced/shelves.md)、[排序视图](docs/advanced/views.md)、[全局权重变更](docs/advanced/config-governance.md)
- [下一步改进计划](docs/roadmap.md)（运行进度、移除人工审核）

默认绑定 `127.0.0.1`，阅读数据保存在本地，备注不发送到模型，没有遥测或外部 CDN。模型处理会发送论文标题与摘要。没有多用户登录、PDF 全文解析或笔记软件同步。

[MIT 许可证](LICENSE)。内置 HTMX 保留其 [0BSD 许可证](src/papersteward/static/HTMX-LICENSE)。
