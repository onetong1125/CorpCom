"""Probe the agreed companies and 2023/2024 annual financial statements.

Run from the repository root: uv run python examples/probe_fundamentals.py
This is an interface acceptance script, not the production financial tool.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import signal

import akshare as ak


# AKShare's calls here have no timeout parameter. Bound this macOS/Linux probe.
signal.alarm(90)
started_at = datetime.now(timezone.utc)
output = Path("runs") / f"fundamentals-{started_at.strftime('%Y%m%dT%H%M%S%fZ')}"
output.mkdir(parents=True)
result = {
    "retrieved_at": started_at.isoformat(),
    "akshare_version": ak.__version__,
    "interface": "stock_financial_report_sina",
    "source": "Sina Finance via AKShare",
    "currency": "CNY",
    "amount_unit": "yuan",
    "period_basis": "annual cumulative, consolidated",
    "first_publication_verified": False,
    "companies": [],
}

for stock, name in (("sh600519", "贵州茅台"), ("sz000858", "五粮液")):
    company = {"symbol": stock, "name": name, "statements": [], "yoy_2024_pct": {}}
    for statement, fields in (
        ("利润表", ("营业收入", "归属于母公司所有者的净利润")),
        ("现金流量表", ("经营活动产生的现金流量净额",)),
    ):
        print(f"Fetching {stock} {statement}", flush=True)
        frame = ak.stock_financial_report_sina(stock=stock, symbol=statement)
        raw_file = output / f"{stock}-{statement}.json"
        raw_file.write_text(frame.to_json(orient="records", force_ascii=False), encoding="utf-8")
        annual = frame.loc[frame["报告日"].isin(("20231231", "20241231"))]
        if len(annual) != 2 or annual["报告日"].nunique() != 2:
            raise ValueError(f"{stock} {statement}: missing or duplicate annual periods")
        annual = annual.set_index("报告日").sort_index()
        if not (annual["币种"].eq("CNY") & annual["类型"].eq("合并期末")).all():
            raise ValueError(f"{stock} {statement}: unexpected currency or statement scope")
        for period, row in annual.iterrows():
            amounts = {}
            for field in fields:
                amount = Decimal(str(row[field]))
                if not amount.is_finite():
                    raise ValueError(f"{stock} {period} {field}: missing or nonfinite amount")
                amounts[field] = str(amount.quantize(Decimal("0.01")))
            company["statements"].append({
                "statement": statement,
                "period_end": datetime.strptime(period, "%Y%m%d").date().isoformat(),
                "amounts_yuan": amounts,
                "source_publish_date": row["公告日期"],
                "source_updated_at": row["更新日期"],
                "source_url": (
                    "https://vip.stock.finance.sina.com.cn/corp/go.php/"
                    f"vFD_FinanceSummary/stockid/{stock[2:]}.phtml"
                ),
                "raw_file": raw_file.name,
            })
        for field in fields:
            previous = Decimal(str(annual.loc["20231231", field]))
            current = Decimal(str(annual.loc["20241231", field]))
            if previous <= 0:
                raise ValueError(f"{stock} {field}: this probe requires a positive prior-year base")
            company["yoy_2024_pct"][field] = str(
                ((current / previous - 1) * 100).quantize(Decimal("0.0001"))
            )
    result["companies"].append(company)

(output / "summary.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    encoding="utf-8",
)
signal.alarm(0)
print(f"Saved raw statements and summary: {output.resolve()}")
