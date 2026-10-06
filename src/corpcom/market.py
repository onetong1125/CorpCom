"""The data tool shared by direct tool calling and future sandbox execution."""

from datetime import date

import akshare as ak


def get_price_history(symbol: str, start_date: str, end_date: str) -> dict:
    """Return daily forward-adjusted Shanghai/Shenzhen closing prices via AKShare."""
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit A-share code")

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("start_date must be on or before end_date")

    frame = ak.stock_zh_a_hist_tx(
        symbol=symbol,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="qfq",
        timeout=15,
    )
    if frame.empty:
        raise ValueError(f"no daily prices for {symbol} from {start_date} to {end_date}")

    return {
        "symbol": symbol,
        "source": "Tencent Finance via AKShare stock_zh_a_hist_tx",
        "adjustment": "qfq",
        "currency": "CNY",
        "rows": [
            {"date": row["date"].isoformat(), "close": float(row["close"])}
            for _, row in frame.iterrows()
        ],
    }
