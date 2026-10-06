# CorpCom

围绕投资假设持续积累研究的 Python Agent 项目：一个研究目标对应一个 Workspace，保存会话、偏好、证据和报告，并支持后续更新。

当前已跑通沪深 A 股股价对比的普通工具调用与 Docker PTC。Workspace、多会话、经营研究报告、新闻更新、定时任务及前后端尚未接入。首个已确认案例与下一批设计见 [持续研究设计](docs/research-workspace.md)；经营数据接口的实测与复跑方式见 [验证记录](docs/financial-data-validation.md)。

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
