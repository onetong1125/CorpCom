import json
import os
import unittest
from decimal import Decimal

from corpcom.fundamentals import get_annual_financials


@unittest.skipUnless(
  os.environ.get("CORPCOM_LIVE_TESTS") == "1",
"Set CORPCOM_LIVE_TESTS=1 to enable live financial requests",
)
class FundamentalsLiveTests(unittest.TestCase):
  def test_matches_annual_report_baseline(self):
    # Fixed 2024/2023 figures from docs/financial-data-sources.md.
    expected = {
      "sh600519": {
        ("利润表", "2023-12-31"): {
          "营业收入": "147693604994.14",
          "归属于母公司所有者的净利润": "74734071550.75",
        },
        ("利润表", "2024-12-31"): {
          "营业收入": "170899152276.34",
          "归属于母公司所有者的净利润": "86228146421.62",
        },
        ("现金流量表", "2023-12-31"): {
          "经营活动产生的现金流量净额": "66593247721.09",
        },
        ("现金流量表", "2024-12-31"): {
          "经营活动产生的现金流量净额": "92463692168.43",
        },
      },
      "sz000858": {
        ("利润表", "2023-12-31"): {
          "营业收入": "83272067317.19",
          "归属于母公司所有者的净利润": "30210585269.30",
        },
        ("利润表", "2024-12-31"): {
          "营业收入": "89175178322.70",
          "归属于母公司所有者的净利润": "31853172533.98",
        },
        ("现金流量表", "2023-12-31"): {
          "经营活动产生的现金流量净额": "41742479908.23",
        },
        ("现金流量表", "2024-12-31"): {
          "经营活动产生的现金流量净额": "33939755192.78",
        },
      },
    }

    for symbol, baseline in expected.items():
      with self.subTest(symbol=symbol):
        result = get_annual_financials(symbol, [2023, 2024])

        self.assertEqual(result["symbol"], symbol)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["amount_unit"], "yuan")
        self.assertEqual(
          result["period_basis"],
          "annual, consolidated",
        )
        self.assertFalse(result["first_publication_verified"])
        self.assertEqual(len(result["statements"]), 6)

        actual = {
          (entry["statement"], entry["period_end"]): entry["amounts_yuan"]
          for entry in result["statements"]
          if entry["statement"] != "资产负债表"
        }
        self.assertEqual(actual, baseline)

        self.assertEqual(result["source"], "Sina Finance via AKShare")
        self.assertEqual(result["interface"], "stock_financial_report_sina")
        self.assertIn("akshare_version", result)
        self.assertIn("retrieved_at", result)

        for entry in result["statements"]:
          self.assertIn("source_publish_date", entry)
          self.assertIn("source_updated_at", entry)
          self.assertEqual(
            entry["source_url"],
            "https://vip.stock.finance.sina.com.cn/corp/go.php/"
            f"vFD_FinanceSummary/stockid/{symbol[2:]}.phtml",
          )
          if entry["statement"] == "资产负债表":
            self.assertEqual(entry["period_basis"], "period-end balance")
            self.assertEqual(
                set(entry["amounts_yuan"]),
                {
                  "应收账款",
                  "存货",
                  "资产总计",
                  "负债合计",
                  "所有者权益(或股东权益)合计",
                },
            )
            amounts = {
              field: Decimal(value)
              for field, value in entry["amounts_yuan"].items()
            }
            self.assertEqual(
                amounts["资产总计"],
                amounts["负债合计"] + amounts["所有者权益(或股东权益)合计"],
            )
          else:
            self.assertEqual(entry["period_basis"], "annual cumulative")

        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertEqual(json.loads(encoded), result)