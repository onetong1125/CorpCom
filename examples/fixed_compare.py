"""Developer-written smoke test for the sandbox data path."""

import json
from pathlib import Path

from corpcom.market import get_price_history


results = []
for symbol in ("600519", "000858"):
    data = get_price_history(symbol, "2025-01-01", "2025-03-31")
    first, last = data["rows"][0], data["rows"][-1]
    results.append(
        {
            "symbol": symbol,
            "first": first,
            "last": last,
            "return_pct": round((last["close"] / first["close"] - 1) * 100, 4),
            "source": data["source"],
            "adjustment": data["adjustment"],
        }
    )

Path("result.json").write_text(json.dumps(results, ensure_ascii=False))
