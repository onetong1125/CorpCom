# CorpCom

CorpCom 是面向 A 股公司研究的本地 Agent 工作台。你可以围绕一个投资假设建立研究空间（Workspace），让 Agent 取数、运行 Python 分析并保存报告。下次打开工作台，可以在同一空间开一个新会话，引用之前的资料，继续追问。

## 可以做什么

- **比较公司经营表现和行情。** 内置函数通过 AKShare 获取年度营业收入、归母净利润、经营现金流净额，以及日频前复权行情。Agent 可以在 Docker 中执行分析代码，把数据、代码和报告保存到工作区。
- **围绕同一目标分开研究。** 每个会话有独立的对话历史，同一空间共享研究档案、上传资料和产物。例如，一个会话核对年报数据，另一个分析同期股价。单进程后端允许同时执行两轮研究。
- **查看研究过程。** 页面流式展示回答与工具调用，可展开参数和结果。关闭页面后研究继续执行，重新进入会话可以查看进度、预览和下载已登记的文件。

## 快速开始

准备 Python 3.11 或以上版本、uv、Node.js 和 npm，以及带 Compose 的 Docker。前端开发与测试可使用 Node.js 26（本仓库使用 26.3.1 验证）。发送研究问题需要支持工具调用的 OpenAI 兼容模型。

**1. 获取项目并配置模型**

```bash
git clone https://github.com/onetong1125/CorpCom.git
cd CorpCom
uv sync
cp .env.example .env
```

在 `.env` 中填写 `OPENAI_API_KEY`、`CORPCOM_MODEL`，并将 `OPENAI_BASE_URL` 改为模型服务的 API 地址。使用仓库的 Compose 配置时，保留示例中的 `CORPCOM_REDIS_URL=redis://127.0.0.1:6380/0`。

**2. 启动 Redis 和后端**

```bash
docker compose up -d redis
docker build -f Dockerfile.sandbox -t corpcom-sandbox:local .
uv run uvicorn corpcom.api:create_app --factory --host 127.0.0.1 --port 8001
```

Redis 支持运行中的事件回放，沙箱镜像供 Agent 执行 Bash 和 Python。后端启动后可打开 [API 调试页面](http://127.0.0.1:8001/docs)。

**3. 启动前端**

另开终端，从仓库根目录运行：

```bash
cd frontend
npm ci
npm run dev
```

打开 [本地工作台](http://127.0.0.1:5174)，创建研究空间和会话，试着提出一个可以核对结果的问题：

> 比较贵州茅台（600519）和五粮液（000858）2024 年的经营现金流净额，计算各自相对 2023 年的同比变化。列出原始金额、单位和来源，并把比较表保存成 CSV。

这组数据有[原始年报对账基准](docs/financial-data-sources.md#原始年报对账基准)，可以用来检查回答和文件里的数字。

## 本地数据

FastAPI 后端默认将业务数据、Agent checkpoint 和研究文件保存在启动目录下的 `data/`。通过 `CORPCOM_DATA_DIR` 可指定其他数据目录。服务按单用户、单 worker 运行，后端重启会把未结束的研究标为中断。备份与沙箱维护见 [Workspace 本地存储](docs/workspace-local.md)。

## 开发与测试

在仓库根目录运行：

```bash
uv run python -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
```

默认测试使用替身模型，真实财务接口测试需显式启用。也可以从命令行运行独立的双股行情比较示例：

```bash
uv run corpcom 600519 000858 2025-01-01 2025-03-31
```

## 文档

| 主题 | 内容 |
| --- | --- |
| [研究案例](docs/research-workspace.md) | 如何定义假设、组织初始报告和更新报告 |
| [前端开发](frontend/README.md) | 开发代理、预览和会话恢复 |
| [后端 API](docs/backend-api.md) | HTTP 接口、SSE 事件、历史读取与错误响应 |
| [Workspace 存储与执行](docs/workspace-local.md) | 文件布局、容器生命周期和产物登记 |
| [数据来源](docs/financial-data-sources.md) · [复跑验证](docs/financial-data-validation.md) | 报表字段、年报对账与样本验证命令 |
| [开发路线](docs/roadmap.md) | 上下文管理、研究记忆与后续验收目标 |
