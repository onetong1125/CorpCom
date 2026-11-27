# CorpCom

CorpCom 是一个在本地运行的 A 股公司研究工作台。围绕一个研究目标创建工作区（Workspace），就可以让 Agent 核对年报数据、执行 Python 分析，并把底稿和报告保存在一起。下次打开工作区，新会话可以从上次留下的问题继续研究。

## 主要功能

- **取数与分析**　通过 AKShare 获取年度营业收入、归母净利润、经营现金流净额和日频前复权行情，在 Docker 沙箱中计算并生成报告、代码或数据文件。
- **跨会话研究**　每个会话独立保存问答。同一工作区共享研究档案、资料和工作笔记。项目判断可按主题保存，用户明确指定的通用偏好可跨工作区复用。
- **过程与证据**　页面流式显示回答及工具调用。关闭页面后，后台会继续本轮研究。重新打开会话可以回看执行过程，预览或下载已登记的产物。

## 快速开始

准备 Python 3.11+、uv、Node.js 和 npm，并启动支持 Compose 的 Docker。本仓库前端使用 Node.js 26.3.1 验证。发送研究问题需要支持工具调用的 OpenAI 兼容模型。

**1. 获取项目并配置模型**

```bash
git clone https://github.com/onetong1125/CorpCom.git
cd CorpCom
uv sync --locked
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

在「研究产物」中打开 CSV 核对结果。按两家公司 2024 年年报的比较数计算，贵州茅台经营现金流净额同比增长 **38.85%**，五粮液同比下降 **18.69%**。[原始年报与金额明细](docs/financial-data-sources.md#原始年报对账基准)

在同一工作区另开会话，可以接着问“请读取刚才的 CSV，哪家公司的经营现金流下降了？引用底稿里的两年金额。”

## 本地数据

FastAPI 后端默认将业务数据、Agent checkpoint 和研究文件保存在启动目录下的 `data/`。通过 `CORPCOM_DATA_DIR` 可指定其他数据目录。服务按单用户、单 worker 运行，后端重启会把未结束的研究标为中断。备份与沙箱维护见 [Workspace 本地存储](docs/workspace-local.md)。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| [api.py](src/corpcom/api.py) | FastAPI 入口，接收工作区操作、研究提问与历史查询 |
| [agent/](src/corpcom/agent/) | 组装研究 Agent，渲染提示词、控制输入预算、注册记忆与沙箱工具 |
| [runs/](src/corpcom/runs/) | 管理每轮研究的执行、事件记录、断线回放和历史读取 |
| [workspace.py](src/corpcom/workspace.py) · [memory.py](src/corpcom/memory.py) | 前者管理工作区、会话和运行记录，后者读写长期记忆的 Markdown 主题与索引 |
| [frontend/](frontend/) | Vue 3 工作台、Vite 开发服务和前端测试 |
| [examples/](examples/) | 可单独运行的取数脚本和沙箱验证示例 |
| [tests/](tests/) | 后端接口、研究历史、记忆与数据函数测试 |

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
| [Workspace 存储与执行](docs/workspace-local.md) | 文件布局、跨会话记忆、容器生命周期和产物登记 |
| [数据来源](docs/financial-data-sources.md) · [复跑验证](docs/financial-data-validation.md) | 报表字段、年报对账与样本验证命令 |
| [开发路线](docs/roadmap.md) | 当前进度、单 Agent 验收与后续计划 |
