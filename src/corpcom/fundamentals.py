"""Fetch annual financial statements via AKShare."""
from datetime import date, datetime, timezone
from decimal import Decimal
import re

import akshare as ak


def get_annual_financials(symbol: str, years: list[int]) -> dict:
  """Fetch annual consolidated revenue, parent-attributable profit, and operating cash flow for a Shanghai/Shenzhen stock.

  Args:
      symbol: Exchange-prefixed stock code, such as "sh600519" or "sz000858".
      years: Non-empty list of distinct integer years, such as [2023, 2024].
          Each year selects the annual reporting period ending December 31.

  Returns:
      A JSON-serializable dict containing:
      - symbol: The requested stock code.
      - source, interface, akshare_version: Data provider and retrieval metadata.
      - retrieved_at: UTC retrieval start time as an ISO datetime string.
      - currency: "CNY".
      - amount_unit: "yuan".
      - period_basis: "annual cumulative, consolidated".
      - first_publication_verified: Always False.
      - statements: A list of statement records.

      Each statement record contains:
      - statement: "利润表" or "现金流量表".
      - period_end: Reporting period end as "YYYY-MM-DD".
      - amounts_yuan: A dict of decimal strings with two fractional digits.
          Profit statements contain "营业收入" and
          "归属于母公司所有者的净利润".
          Cash flow statements contain "经营活动产生的现金流量净额".
      - source_publish_date, source_updated_at: Unmodified upstream date values.
      - source_url: The source page URL.

      Records are grouped by statement, with years ascending within each group.
      The result contains only these three metrics, not complete statements.
      Use Decimal for monetary calculations and convert it to str for JSON.

  Notes:
      Amounts cover the full year on a consolidated basis.
      Upstream dates are not verified first-publication dates, and historical
      values may have been revised. This result does not establish which data
      was available at a historical point in time.
      The function fetches data but does not calculate growth or write files.
      Requests have no function-level timeout.

  Raises:
      ValueError: Rejected arguments, missing or duplicate annual periods,
          unexpected currency or statement scope, or nonfinite amounts.
      Other amount-parsing and upstream request exceptions propagate unchanged.

  Example:
      from decimal import Decimal
      from corpcom.fundamentals import get_annual_financials

      data = get_annual_financials("sh600519", [2023, 2024])
      income = {
          item["period_end"]: item["amounts_yuan"]
          for item in data["statements"]
          if item["statement"] == "利润表"
      }
      revenue_2024 = Decimal(income["2024-12-31"]["营业收入"])
  """

  if re.fullmatch(r"(sh|sz)[0-9]{6}", symbol) is None:
    raise ValueError("symbol must be sh or sz followed by six digits")

  if not years:
    raise ValueError("years must not be empty")
  if len(years) != len(set(years)):
    raise ValueError("years must not contain duplicates")

  periods = [
    date(year, 12, 31).strftime("%Y%m%d")
    for year in sorted(years)
  ]

  retrieved_at = datetime.now(timezone.utc).isoformat()

  income = ak.stock_financial_report_sina(
      stock=symbol,
      symbol="利润表",
  )
  cash_flow = ak.stock_financial_report_sina(
      stock=symbol,
      symbol="现金流量表",
  )

  statements = []

  for statement, frame, fields in (
      ("利润表", income, ("营业收入", "归属于母公司所有者的净利润"),),
      ("现金流量表", cash_flow, ("经营活动产生的现金流量净额",)),
  ):
    annual = frame.loc[frame["报告日"].isin(periods)]

    if (
        len(annual) != len(periods)
        or annual["报告日"].nunique() != len(periods)
    ):
      raise ValueError(
          f"{symbol} {statement}: missing or duplicate annual periods"
      )

    if not (
        annual["币种"].eq("CNY")
        & annual["类型"].eq("合并期末")
    ).all():
      raise ValueError(
          f"{symbol} {statement}: unexpected currency or statement scope"
      )

    annual = annual.set_index("报告日").sort_index()
    for period, row in annual.iterrows():
      amounts = {}

      for field in fields:
        amount = Decimal(str(row[field]))
        if not amount.is_finite():
          raise ValueError(
              f"{symbol} {period} {field}: missing or nonfinite amount"
          )
        amounts[field] = str(amount.quantize(Decimal("0.01")))

      statements.append({
        "statement": statement,
        "period_end": datetime.strptime(
            period, "%Y%m%d"
        ).date().isoformat(),
        "amounts_yuan": amounts,
        "source_publish_date": row["公告日期"],
        "source_updated_at": row["更新日期"],
        "source_url": (
          "https://vip.stock.finance.sina.com.cn/corp/go.php/"
          f"vFD_FinanceSummary/stockid/{symbol[2:]}.phtml"
        )
      })

  return {
    "symbol": symbol,
    "retrieved_at": retrieved_at,
    "source": "Sina Finance via AKShare",
    "interface": "stock_financial_report_sina",
    "akshare_version": ak.__version__,
    "currency": "CNY",
    "amount_unit": "yuan",
    "period_basis": "annual cumulative, consolidated",
    "first_publication_verified": False,
    "statements": statements,
  }
