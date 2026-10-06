# 双公司财务数据源核查（2023、2024 年度）

核查对象：贵州茅台（600519）、五粮液（000858）。目标字段为合并口径的**营业收入、归属于母公司股东的净利润、经营活动产生的现金流量净额**，另记录报告期、同比和信息可见日期。以下是 2026-10-06 对 AKShare 官方文档、源码及公司／交易所年报的核查；本机使用 AKShare 1.19.1，具体请求与数值对账见 [实测记录](financial-data-validation.md)。固定验收期为 2023、2024 年度，不代表截至当前的最新数据。

## 接口取舍

| 接口 | 文档或源码能确认的结构 | 对三个目标金额的适用性 | 日期限制 |
| --- | --- | --- | --- |
| [`stock_financial_abstract_new_ths`](https://akshare.akfamily.xyz/data/stock/stock.html#id226) | 同花顺新接口，已替换旧的 `stock_financial_abstract_ths`；按报告期返回 `report_date`、`metric_name`、`value`、`single`、`yoy` 等长表列。源码把上游 `date` 写入 `report_date`，逐个展开 `index_list`。见 [AKShare 源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_ths.py#L194-L280)。 | 文档只列通用字段，**没有保证具体指标齐全**。本批探针已见 `operating_income_total`（营业总收入）及每股经营现金流；二者不能分别冒充营业收入和经营现金流量净额。归母净利润与其他指标须按实际 `metric_name` 核实。 | 无公告／首次披露日期。`report_date` 是财务报告期末。 |
| [`stock_financial_abstract`](https://akshare.akfamily.xyz/data/stock/stock.html#id225) | 新浪关键指标宽表：`选项`、`指标`、各报告期列。文档样例有“归母净利润”“营业总收入”；[源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py#L94-L180)从 `report_list` 拼接 `item_title` 和 `item_value`，不保留公告日期。 | 可作辅助交叉检查，但不能仅凭文档样例断言有“营业收入”或经营现金流净额。源码请求参数 `paperCode=f"sh{symbol}"`，用于五粮液时需实测，不能假设深市代码已正确适配。 | 报告期是列名；无公告／首次披露日期。 |
| [`stock_financial_report_sina`](https://akshare.akfamily.xyz/data/stock/stock.html#id205) | 新浪三大报表；`stock="sh600519"` 或 `"sz000858"`，`symbol="利润表"`／`"现金流量表"`。文档列 `报告日`、报表项目、`类型`、`更新日期`；[源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py#L22-L91)另将上游 `publish_date`、`update_time`、`rCurrency`、`rType` 写成 `公告日期`、`更新日期`、`币种`、`类型`。 | 本批数据探针的优先来源：利润表取精确营业收入、归母净利润；现金流量表取经营活动产生的现金流量净额。必须用原始年报核对字段名称、数值及合并口径。 | `公告日期`是**上游当前记录的字段值**，尚未证明是首次公开日期。主探针在茅台 2024 年行观察到 `公告日期=20260417`、`更新日期=2025-04-02`，存在明显不适合直接作为首次可见日的冲突；保留原值及来源，历史回放另取首次披露公告证据。 |

以上“本批探针观察”来自项目本批实际运行。AKShare 文档明确记录新版同花顺接口替换旧版；不要沿用旧版的宽表列名或字段映射。在线文档和 main 分支源码会变化，复跑时应同时记录本机版本。[官方文档](https://akshare.akfamily.xyz/data/stock/stock.html#id226)

## 原始年报对账基准

下表是各公司 **2024 年年报所列的 2024／2023 比较数**，单位均为人民币元，增长率为年报百分数。贵州茅台来自[公司官网 2024 年年度报告，第 5 页](https://www.moutai.com.cn/mtgf/articleFileDir/2025-04/08/8055b7bed7db41bdbc617f4c9b9ec591.pdf)；五粮液来自[深交所披露的 2024 年度报告全文，第 7 页](https://disc.static.szse.cn/disc/disk03/finalpage/2025-04-26/e366c663-84d0-4700-a59a-00a7d6412d48.PDF)。

| 公司 | 指标 | 2024 年 | 2023 年比较数 | 年报同比 |
| --- | --- | ---: | ---: | ---: |
| 贵州茅台 | 营业收入 | 170,899,152,276.34 | 147,693,604,994.14 | 15.71% |
| 贵州茅台 | 归属于上市公司股东的净利润 | 86,228,146,421.62 | 74,734,071,550.75 | 15.38% |
| 贵州茅台 | 经营活动产生的现金流量净额 | 92,463,692,168.43 | 66,593,247,721.09 | 38.85% |
| 五粮液 | 营业收入 | 89,175,178,322.70 | 83,272,067,317.19 | 7.09% |
| 五粮液 | 归属于上市公司股东的净利润 | 31,853,172,533.98 | 30,210,585,269.30 | 5.44% |
| 五粮液 | 经营活动产生的现金流量净额 | 33,939,755,192.78 | 41,742,479,908.23 | -18.69% |

贵州茅台年报的[合并利润表，第 63 页](https://www.moutai.com.cn/mtgf/articleFileDir/2025-04/08/8055b7bed7db41bdbc617f4c9b9ec591.pdf)明确区分 2024 年“营业总收入” **174,144,069,958.25 元**和其中“营业收入” **170,899,152,276.34 元**。这是不能把同花顺 `operating_income_total` 直接映射为目标“营业收入”的可复核例子。

## 口径与历史回放限制

1. **单位和字段**：年报表内金额为人民币**元**，同比列为**百分数**；AKShare 同花顺长表文档仅写 `value`／`yoy` 等类型，没有给具体指标的单位或 `yoy` 缩放定义。实际接口金额和同比须与上表逐项比较，再确定换算规则。新浪三表需核对返回的 `币种`、`类型` 及合并报表行，不能把母公司口径、营业总收入、每股现金流混入目标字段。[同花顺文档](https://akshare.akfamily.xyz/data/stock/stock.html#id226)、[新浪三表源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py#L22-L91)
2. **累计和同比**：年报的 2024 数是全年 1—12 月，2023 是全年比较数；季度／半年／三季度财务报表的利润、现金流通常为年初至报告期末累计，单季值须由同年相邻累计值相减后另行标识。2024 年同比应与 **2023 年同口径期间**比较；负基数、零基数和上游重述需单独说明。不要把同花顺 `single`、`yoy` 当作已定义好的百分数或金额。[贵州茅台年报，第 5—6 页](https://www.moutai.com.cn/mtgf/articleFileDir/2025-04/08/8055b7bed7db41bdbc617f4c9b9ec591.pdf)、[五粮液年报，第 7—8 页](https://disc.static.szse.cn/disc/disk03/finalpage/2025-04-26/e366c663-84d0-4700-a59a-00a7d6412d48.PDF)
3. **可见时间**：`报告日=20241231` 只标识会计期间结束；不能让历史回放在 2024-12-31 看见 2024 年报。新浪三表的 `公告日期` 来自可更新的上游记录，当前观察值甚至晚于 `更新日期`，不能无证据地当作首次披露日。回放在补齐公司／交易所原公告的披露时间及版本之前，只能做事后分析，不应声明为当时可用的无未来信息回测。[新浪三表源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py#L22-L91)、[贵州茅台财务报告列表](https://www.moutai.com.cn/mtgf/tzzgx/cwbg/index.html)

验收时逐公司逐期记录：AKShare 版本、接口与请求参数、原始列名、报告日、报表类型／币种、三个金额原值及单位、同比原值及缩放、`公告日期`／`更新日期`原值、年报对账差额；保留不一致，不以自动替换或猜测字段补齐。
