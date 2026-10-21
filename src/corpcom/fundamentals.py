"""Fetch annual financial statements via AKShare."""
from datetime import date, datetime, timezone
from decimal import Decimal
import re

import akshare as ak


def get_annual_financials(symbol: str, years: list[int]) -> dict:
  """Return annual consolidated financial data for one stock."""
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
