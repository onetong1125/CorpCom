"""Inspect annual balance-sheet fields before defining the data contract."""

from pathlib import Path
import signal

import akshare as ak


# Bound the probe because this AKShare interface has no timeout parameter.
signal.alarm(90)

frame = ak.stock_financial_report_sina(
    stock="sh600519",
    symbol="资产负债表",
)

annual = frame.loc[
  frame["报告日"].isin(["20231231", "20241231"])
].sort_values("报告日")

output = Path("runs/probe-balance-sheet")
output.mkdir(parents=True, exist_ok=True)

annual.to_csv(output / "annual.csv", index=False)

print("AKShare:", ak.__version__)
print("Annual rows:", len(annual))
print(annual.transpose().to_string())
print("Saved:", (output / "annual.csv").resolve())

signal.alarm(0)