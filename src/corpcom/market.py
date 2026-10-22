"""The data tool shared by direct tool calling and future sandbox execution."""

from datetime import date

import akshare as ak


def get_price_history(symbol: str, start_date: str, end_date: str) -> dict:
    """Fetch daily forward-adjusted closing prices for a Shanghai/Shenzhen stock over a date range.

    Args:
        symbol: Six-digit stock code without an exchange prefix,
            such as "600519" or "000858".
        start_date: Requested start date in "YYYY-MM-DD" format.
        end_date: Requested end date in "YYYY-MM-DD" format.
            Must be on or after start_date.

    Returns:
        A dict containing:
        - symbol: The requested stock code.
        - source: Data provider and interface description.
        - adjustment: "qfq", meaning forward-adjusted prices.
        - currency: "CNY".
        - rows: A non-empty list of daily price records.

        Each row contains only:
        - date: Actual returned date as a "YYYY-MM-DD" string.
        - close: Forward-adjusted closing price as a float.

    Notes:
        This function provides daily closing prices, not intraday data,
        opening prices, high/low prices, or trading volume.
        Requested boundaries may be non-trading days. Use the actual
        returned dates when reporting the observed period.
        The function preserves upstream row order without explicitly sorting.
        Sort rows by date before selecting the first and last prices.
        The upstream request receives timeout=15; this is not a total
        execution-time guarantee for the entire function.
        The function returns data without calculating returns or writing files.

    Raises:
        ValueError: Invalid stock code or dates, reversed date range,
            or no price data returned.
        Other upstream request and data-conversion exceptions propagate unchanged.

    Example:
        from corpcom.market import get_price_history

        data = get_price_history("600519", "2025-01-01", "2025-03-31")
        rows = sorted(data["rows"], key=lambda row: row["date"])
        first, last = rows[0], rows[-1]
        first_date = first["date"]
        last_close = last["close"]
    """
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
