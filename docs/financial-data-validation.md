# 经营数据接口实测记录

## 结论

2026-10-06 在本机 Python 3.11 / AKShare 1.19.1 下，真实调用 `stock_financial_report_sina` 获取贵州茅台、五粮液的利润表和现金流量表成功。

两家公司 × 两个年度（2023、2024）× 三项指标，共 **12 个金额与 2024 年原始年报中的本期及比较数一致，按人民币元保留两位小数对账通过**；自行计算的 6 个同比保留两位小数后，与年报披露百分数一致。原始年报链接及精确金额见 [资料核查](financial-data-sources.md#原始年报对账基准)。

本批验证的是接口和固定样本的数据语义，未生成投资研究结论，也未验证每日稳定性、所有上市公司或历史首次披露版本。

## 如何在本地复跑

在项目根目录执行：

```bash
uv run python examples/probe_fundamentals.py
```

脚本固定验证本案例，发起四次报表请求，检查报告期、币种、合并范围和金额是否有效，计算同比，保存结果并打印目录。没有模型调用，不需要读取 `.env`，不需要 Docker。

每次运行生成 `runs/fundamentals-<时间戳>/`，内含以下文件。

- 两家公司各一份利润表、现金流量表 JSON，共四份。这是 AKShare 返回表的直接序列化，**不是未经解析的 HTTP 原文**。
- `summary.json`：选出的年度金额、同比、AKShare 版本、抓取时间、来源链接和上游日期原值。金额用字符串保存到分。

`runs/` 已被 Git 忽略；其他机器复跑会创建新的带时间戳目录。金额与原始年报的对账由本批独立检查完成，探针自身不会下载年报进行自动对账。脚本用 90 秒进程时限约束无 timeout 参数的 AKShare 请求，适用于当前 macOS/Linux 环境；请求失败或超时直接失败，部分已下载文件会保留。

## 年度经营数据函数

独立脚本和 Agent 沙箱均可调用年度经营数据函数。

```python
from corpcom.fundamentals import get_annual_financials

result = get_annual_financials("sh600519", [2023, 2024])
```

股票代码使用 `sh` / `sz` 加六位数字。函数获取利润表和现金流量表，按指定年度筛选年末报告期，返回营业收入、归母净利润和经营现金流净额。

返回值为可序列化为 JSON 的字典。`statements` 按报表和报告期组织记录，金额位于 `amounts_yuan`，使用人民币元、两位小数字符串。结果保留来源链接、AKShare 版本、UTC 抓取开始时间、上游公告日期和更新日期，以及合并年度累计口径。

函数不计算同比、不保存文件；文件保存由调用方负责。首次披露时间仍未核验，`first_publication_verified` 为 `false`。该函数已注册到 Agent 的函数目录，可在 Docker 沙箱中调用。

非法参数、缺失或重复年度、币种或合并口径不符、非有限金额会明确报错；无法解析的金额和请求异常继续向上传播。

### 测试命令

在项目根目录执行。单元测试使用模拟报表，不联网：

```bash
uv run python -m unittest discover -s tests -p 'test_fundamentals.py' -v
```

真实接口测试默认跳过，显式启用后请求两家公司，并对照已有年报基准核对 12 个金额：

```bash
CORPCOM_LIVE_TESTS=1 uv run python -m unittest discover -s tests -p 'test_fundamentals_live.py' -v
```

启用后，网络异常或金额不一致会使测试失败，不会自动跳过。测试不重新下载原始年报；基准来自本文关联的资料核查记录。2023、2024 年为固定验收样本，不代表当前最新数据。

## 在 Docker 中复跑

在仓库根目录构建沙箱镜像，再运行固定脚本：

```bash
docker build -f Dockerfile.sandbox -t corpcom-sandbox:local .
uv run corpcom-sandbox examples/fixed_fundamentals.py
```

独立沙箱命令将脚本、`result.json` 和生成的数据保存在 `runs/<执行编号>/`。行情也有固定脚本和模型生成脚本两种入口：

```bash
uv run corpcom-sandbox examples/fixed_compare.py
uv run corpcom-ptc 600519 000858 2025-01-01 2025-03-31
```

## 已记录的端到端样本

2026-10-21 的固定脚本在容器内获取了两家公司的年度数据，12 个金额与年报基准一致。2026-10-22 的真实模型验收覆盖了先读取函数契约，再执行年度财务、日频行情及组合查询。茅台 2024 年营业收入同比为 15.71%；行情样本返回 2025-01-02 至 2025-03-31 共 57 条记录，未做跨来源行情对账。

Workspace 中的 Python 工具将声明的输出复制到任务目录并登记，文件可以通过前端或 HTTP 接口读取。[Workspace 存储与执行](workspace-local.md)
