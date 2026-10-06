# CorpCom

聚焦沪深 A 股两家公司股价对比的 Python Agent。当前第一阶段只运行普通工具调用：模型向 `get_price_history` 请求真实日线，工具返回数据，模型生成对比结论。PTC、沙箱、前后端尚未接入。

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
