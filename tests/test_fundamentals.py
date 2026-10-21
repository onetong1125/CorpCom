import json
import unittest
from unittest.mock import call, patch
from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation
from requests.exceptions import ConnectionError

import akshare as ak
import pandas as pd

from corpcom.fundamentals import get_annual_financials


class FundamentalsTests(unittest.TestCase):
  def test_returns_annual_amounts(self):
    # Reverse the input years to verify chronological output.
    common = {
      "报告日": ["20241231", "20231231"],
      "币种": ["CNY", "CNY"],
      "类型": ["合并期末", "合并期末"],
      "公告日期": ["20250430", "20240430"],
      "更新日期": ["2025-04-25", "2024-04-25"],
    }
    income = pd.DataFrame({
      **common,
      "营业收入": ["120.5", "100"],
      "营业总收入": ["999", "888"],
      "归属于母公司所有者的净利润": ["12", "10"],
    })
    cash_flow = pd.DataFrame({
      **common,
      "经营活动产生的现金流量净额": ["-5", "20"],
    })

    # Replace network access while exercising the real transformation.
    with patch(
      "corpcom.fundamentals.ak.stock_financial_report_sina",
      side_effect=[income, cash_flow],
    ) as fetch:
      before = datetime.now(timezone.utc)
      result = get_annual_financials("sh600519", [2024, 2023])
      after = datetime.now(timezone.utc)

    self.assertEqual(fetch.call_args_list, [
      call(stock="sh600519", symbol="利润表"),
      call(stock="sh600519", symbol="现金流量表"),
    ])

    actual = [
      (
        entry["statement"],
        entry["period_end"],
        entry["amounts_yuan"],
      )
      for entry in result["statements"]
    ]
    self.assertEqual(actual, [
      (
        "利润表",
        "2023-12-31",
        {"营业收入": "100.00", "归属于母公司所有者的净利润": "10.00"},
      ),
      (
        "利润表",
        "2024-12-31",
        {"营业收入": "120.50", "归属于母公司所有者的净利润": "12.00"},
      ),
      (
        "现金流量表",
        "2023-12-31",
        {"经营活动产生的现金流量净额": "20.00"},
      ),
      (
        "现金流量表",
        "2024-12-31",
        {"经营活动产生的现金流量净额": "-5.00"},
      ),
    ])

    self.assertEqual(result["symbol"], "sh600519")
    self.assertEqual(result["currency"], "CNY")
    self.assertEqual(result["amount_unit"], "yuan")
    self.assertFalse(result["first_publication_verified"])

    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    self.assertEqual(json.loads(encoded), result)

    self.assertEqual(result["source"], "Sina Finance via AKShare")
    self.assertEqual(result["interface"], "stock_financial_report_sina")
    self.assertEqual(result["akshare_version"], ak.__version__)
    self.assertEqual(
      result["period_basis"],
      "annual cumulative, consolidated",
    )

    retrieved_at = datetime.fromisoformat(result["retrieved_at"])
    self.assertEqual(retrieved_at.utcoffset(), timedelta(0))
    self.assertLessEqual(before, retrieved_at)
    self.assertLessEqual(retrieved_at, after)

    expected_dates = {
      "2023-12-31": ("20240430", "2024-04-25"),
      "2024-12-31": ("20250430", "2025-04-25"),
    }
    for entry in result["statements"]:
      self.assertEqual(
        (entry["source_publish_date"], entry["source_updated_at"]),
        expected_dates[entry["period_end"]],
      )
      self.assertEqual(
        entry["source_url"],
        "https://vip.stock.finance.sina.com.cn/corp/go.php/"
        "vFD_FinanceSummary/stockid/600519.phtml",
      )

  def test_rejects_invalid_arguments_before_fetching(self):
    cases = [
      ("600519", [2023, 2024], "symbol must be"),
      ("sh600519", [], "years must not be empty"),
      ("sh600519", [2023, 2023], "years must not contain duplicates"),
    ]

    for symbol, years, message in cases:
      with self.subTest(symbol=symbol, years=years):
        with patch(
          "corpcom.fundamentals.ak.stock_financial_report_sina"
        ) as fetch:
          with self.assertRaisesRegex(ValueError, message):
            get_annual_financials(symbol, years)

          fetch.assert_not_called()

  def test_rejects_missing_or_duplicate_annual_periods(self):
    valid = pd.DataFrame({
      "报告日": ["20231231", "20241231"],
      "币种": ["CNY", "CNY"],
      "类型": ["合并期末", "合并期末"],
      "公告日期": ["20240430", "20250430"],
      "更新日期": ["2024-04-25", "2025-04-25"],
      "营业收入": ["100", "120"],
      "归属于母公司所有者的净利润": ["10", "12"],
      "经营活动产生的现金流量净额": ["20", "25"],
    })
    cases = [
      ("missing", [0]),
      ("duplicate_replacing_year", [0, 0]),
      ("extra_duplicate", [0, 1, 1]),
    ]

    for index, statement in enumerate(("利润表", "现金流量表")):
      for case, positions in cases:
        with self.subTest(statement=statement, case=case):
          frames = [valid.copy(), valid.copy()]
          frames[index] = valid.iloc[positions].copy()

          with patch(
            "corpcom.fundamentals.ak.stock_financial_report_sina",
            side_effect=frames,
          ):
            with self.assertRaisesRegex(
              ValueError,
              f"{statement}: missing or duplicate annual periods",
            ):
              get_annual_financials("sh600519", [2023, 2024])

  def test_rejects_unexpected_currency_or_scope(self):
    valid = pd.DataFrame({
      "报告日": ["20231231", "20241231"],
      "币种": ["CNY", "CNY"],
      "类型": ["合并期末", "合并期末"],
      "公告日期": ["20240430", "20250430"],
      "更新日期": ["2024-04-25", "2025-04-25"],
      "营业收入": ["100", "120"],
      "归属于母公司所有者的净利润": ["10", "12"],
      "经营活动产生的现金流量净额": ["20", "25"],
    })
    cases = [
      ("币种", "USD"),
      ("类型", "母公司"),
    ]

    for index, statement in enumerate(("利润表", "现金流量表")):
      for column, value in cases:
        with self.subTest(statement=statement, column=column):
          frames = [valid.copy(), valid.copy()]
          frames[index].loc[0, column] = value

          with patch(
              "corpcom.fundamentals.ak.stock_financial_report_sina",
              side_effect=frames,
          ):
            with self.assertRaisesRegex(
                ValueError,
                f"{statement}: unexpected currency or statement scope",
            ):
              get_annual_financials("sh600519", [2023, 2024])

  def test_rejects_invalid_amounts(self):
    valid = pd.DataFrame({
      "报告日": ["20231231", "20241231"],
      "币种": ["CNY", "CNY"],
      "类型": ["合并期末", "合并期末"],
      "公告日期": ["20240430", "20250430"],
      "更新日期": ["2024-04-25", "2025-04-25"],
      "营业收入": ["100", "120"],
      "归属于母公司所有者的净利润": ["10", "12"],
      "经营活动产生的现金流量净额": ["20", "25"],
    })
    fields = [
      (0, "营业收入"),
      (0, "归属于母公司所有者的净利润"),
      (1, "经营活动产生的现金流量净额"),
    ]
    cases = [
      ("NaN", ValueError),
      ("Infinity", ValueError),
      ("-Infinity", ValueError),
      ("--", InvalidOperation),
    ]

    for index, field in fields:
      for value, expected_error in cases:
        with self.subTest(field=field, value=value):
          frames = [valid.copy(), valid.copy()]
          frames[index].loc[0, field] = value

          with patch(
              "corpcom.fundamentals.ak.stock_financial_report_sina",
              side_effect=frames,
          ):
            with self.assertRaises(expected_error) as caught:
              get_annual_financials("sh600519", [2023, 2024])

          if expected_error is ValueError:
            self.assertIn(field, str(caught.exception))
            self.assertIn("nonfinite amount", str(caught.exception))

  def test_propagates_fetch_error(self):
    error = ConnectionError("connection failed")

    with patch(
      "corpcom.fundamentals.ak.stock_financial_report_sina",
      side_effect=error,
    ) as fetch:
      with self.assertRaises(ConnectionError) as caught:
        get_annual_financials("sh600519", [2023, 2024])

      self.assertIs(caught.exception, error)
      fetch.assert_called_once_with(
        stock="sh600519",
        symbol="利润表",
      )
