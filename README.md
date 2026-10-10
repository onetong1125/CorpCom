# CorpCom

围绕投资假设持续积累研究的 Python Agent 项目：一个研究目标对应一个 Workspace，保存会话、偏好、证据和报告，并支持后续更新。

当前已跑通沪深 A 股股价对比的普通工具调用与 Docker PTC，并实现 Workspace、多会话消息、文件的本地持久化及基于 Workspace 的模型对话。后端 HTTP 接口已支持研究管理、对话和文件上传下载；经营研究报告、新闻更新、定时任务及前端尚未实现。首个已确认案例与设计见 [持续研究设计](docs/research-workspace.md)；经营数据接口的实测与复跑方式见 [验证记录](docs/financial-data-validation.md)。

## 启动后端

```bash
uv sync
uv run uvicorn corpcom.api:create_app --factory --host 127.0.0.1 --port 8000
```

打开 [接口调试页面](http://127.0.0.1:8000/docs) 创建 Workspace、会话并发送消息。后端沿用本机 `.env` 模型配置和 `data/` 存储，前端只需调用 HTTP 接口。完整接口、错误响应和前端示例见 [后端接口文档](docs/backend-api.md)。

## Workspace 本地验收

Workspace 的创建、会话、偏好和文件操作统一使用 [后端 HTTP 接口](docs/backend-api.md)。存储接口不需要模型密钥、Docker 或网络；发送消息需要配置模型。

```bash
uv run python -m unittest discover -s tests -v
```

测试使用临时数据目录，覆盖持久化恢复、Workspace 隔离、文件副本、Agent 对话和 HTTP 行为。存储结构与框架执行说明见 [本地存储与验收](docs/workspace-local.md)。

默认数据保存于服务启动目录下的 `data/`（已被 Git 忽略）；从其他位置启动时，通过 `CORPCOM_DATA_DIR` 指向已有数据目录。行情与 Docker PTC 命令保留为独立开发验收工具。

## 首阶段验收

- 输入两只六位 A 股代码及起止日期。
- 两只股票的行情均来自 AKShare 的 `stock_zh_a_hist_tx`（底层为腾讯财经），使用日线前复权收盘价。
- 回答列出各自首末交易日、收盘价、区间涨跌幅及公式，并注明来源和复权方式。
- 用同一批工具数据独立复算两个涨跌幅，确认模型数值正确。模型调用与行情抓取均须真实运行；静态代码检查不算通过。

## 运行

```bash
uv sync
cp .env.example .env
# 仅在本机编辑 .env，填写兼容接口地址、密钥、模型名
uv run corpcom 600519 000858 2025-01-01 2025-03-31
```

命令行中的公司代码和日期仅为可替换示例。`.env` 已被 Git 忽略，不要把密钥发到聊天或提交到仓库。行情可能受数据提供方可用性影响；程序遇到抓取失败时直接报错。

如果本机设置了已停止的 HTTP 代理，腾讯财经请求可能报 `ProxyError`。仅让行情域名直连即可，模型接口仍使用原有网络配置：

```bash
NO_PROXY=web.ifzq.gtimg.cn,proxy.finance.qq.com \
no_proxy=web.ifzq.gtimg.cn,proxy.finance.qq.com \
uv run corpcom 600519 000858 2025-01-01 2025-03-31
```

## Docker PTC 验证

先构建只包含行情工具的镜像，再运行固定脚本确认容器内的数据链路：

```bash
docker build -f Dockerfile.sandbox -t corpcom-sandbox:local .
uv run corpcom-sandbox examples/fixed_compare.py
```

固定脚本通过后，运行模型生成代码的 PTC 链路：

```bash
uv run corpcom-ptc 600519 000858 2025-01-01 2025-03-31
```

模型生成的 Python 保存在 `runs/<运行编号>/analysis.py`，在 Docker 内调用同一个 `get_price_history`，写出 `result.json`。宿主程序只将结果摘要或执行错误返回模型，不回传完整日线。每次执行只挂载对应的 `runs/<运行编号>` 目录；`.env` 不进入镜像或容器。运行目录被 Git 忽略，可在本机检查生成的代码和结果。
