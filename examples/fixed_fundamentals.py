"""Verify annual financial retrieval and saved files inside the sandbox."""

import json
from pathlib import Path

from corpcom.fundamentals import get_annual_financials


years = [2023, 2024]
companies = []

for symbol in ("sh600519", "sz000858", "sz000568"):
  data = get_annual_financials(symbol, years)
  filename = f"{symbol}-annual-financials.json"

  # Preserve the complete function result for later inspection.
  Path(filename).write_text(
    json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    encoding="utf-8",
  )

  companies.append({
    "symbol": symbol,
    "years": years,
    "statement_count": len(data["statements"]),
    "source": data["source"],
    "data_file": filename,
  })

# Return a compact file index through the sandbox tool.
Path("result.json").write_text(
  json.dumps(
    {"companies": companies},
    ensure_ascii=False,
    indent=2,
    allow_nan=False,
  ) + "\n",
  encoding="utf-8",
)