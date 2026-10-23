# 快速开始

目标是先体验一份**合成试运行日报**，再配置自己的研究工作区。安装下载时间取决于网络；真实论文的质量与速度取决于来源和模型，不能用示例指标代替。

## 安装

需要 Docker Compose，或 Python 3.11+；源码来自[项目仓库](https://github.com/INKEM/PaperSteward)。

```bash
git clone https://github.com/INKEM/PaperSteward.git
cd PaperSteward
docker compose up --build
```

打开 [首次设置](http://127.0.0.1:8000/ui/setup)。再次启动用 `docker compose up`，停止用 `docker compose down`，命名卷继续保存。不要用 `down -v` 清理正在使用的数据。

Python 用户可选 pipx：

```bash
pipx install .
papersteward start --data-dir ./var
```

或虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/papersteward start --data-dir ./var
```

已下载本项目 wheel 时，安装命令的 `.` 可换成 wheel 路径。wheel 自带迁移与 Starter，可在仓库外运行；当前不假设 PyPI 发布。源码贡献环境也支持 `make bootstrap` / `make start`。

## 浏览器首用

1. 点击“开始设置”，研究方向预填 Starter。保存草稿，检查全文与提示，勾选审阅确认，确认 G1。
2. 分类法预填合成标签和20条种子。保留“示例/合成”资料性质，保存并审阅覆盖率、边界规则，再确认 G2。
3. 点击“使用示例生成校准报告”。阅读关键指标及展开的完整分歧报告，勾选并确认 G3。高级入口支持自己的样本和双方独立标注。
4. 试运行保持默认时间、时区及“仅离线体验，不授权外发”。点击“生成新的试运行预览”，即可看到7条合成论文的日报。检查容量、分类、阅读方式；有问题先记录调整意见，认可后点击“我认可这份试运行日报”，最后勾选并确认 G4。
5. 完成后可展开已确认的预览，或进入今日。没有真实候选时，今日页会说明下一步。

示例只验证流程，不会采集真实论文、调用模型或发送邮件。保留默认值的设置路径可在10分钟内完成；首次依赖下载和镜像构建不作固定时长保证。

## 开始自己的工作区

Python 停止服务后：`papersteward start --data-dir ./my-research`。Docker 可在另一个目录保留独立 checkout，并以不同 Compose project 启动和调整本地端口；不要删除示例卷来冒充迁移。

先[配置模型](llm-provider.md)，再用自己的研究方向、分类种子、独立校准和试运行确认四步。G4 选择允许论文元数据/摘要发送给配置的模型；备注不外发。既有工作区不提供跳过门禁或直接重置确认数据的按钮。

在今日页依次执行“同步 / 处理 → 处理审核 → 生成日报”。采集参数折叠在同步表单中；本次授权必须勾选。已保存的日报按日期不可变，候选不足时先完成处理审核。

[配置](configuration.md) · [排障](troubleshooting.md) · [隐私](privacy.md)
