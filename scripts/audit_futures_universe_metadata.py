"""Snapshot current Binance contract types for the historical research universe.

This audit does not rewrite the historical, lookahead-safe universe.  It tags
the union of symbols using a timestamped ExchangeInfo snapshot so crypto rules
can be evaluated separately from equity, commodity and other TradFi contracts.
Historical selection remains based solely on prior-month volume.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def fetch_exchange_info(url: str = DEFAULT_URL, timeout: float = 45.0) -> dict:
    request = Request(url, headers={"User-Agent": "TradingBotV3-Research/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def audit_universe(universe: dict, exchange_info: dict, *, observed_at: str) -> dict:
    monthly = universe.get("monthly") or {}
    symbols = sorted({
        str(row["symbol"])
        for month in monthly.values()
        for row in month.get("symbols", [])
    })
    metadata = {
        str(row.get("symbol")): row
        for row in exchange_info.get("symbols", [])
        if row.get("symbol")
    }

    rows = []
    for symbol in symbols:
        item = metadata.get(symbol)
        rows.append({
            "symbol": symbol,
            "found_in_snapshot": item is not None,
            "underlying_type": str(item.get("underlyingType") or "UNKNOWN") if item else "UNKNOWN",
            "underlying_subtypes": list(item.get("underlyingSubType") or []) if item else [],
            "contract_type": str(item.get("contractType") or "UNKNOWN") if item else "UNKNOWN",
            "status": str(item.get("status") or "UNKNOWN") if item else "UNKNOWN",
        })

    counts = Counter(row["underlying_type"] for row in rows)
    per_month = {}
    by_symbol = {row["symbol"]: row for row in rows}
    for month, body in sorted(monthly.items()):
        month_counts = Counter(
            by_symbol[str(row["symbol"])]["underlying_type"]
            for row in body.get("symbols", [])
        )
        per_month[month] = dict(sorted(month_counts.items()))

    return {
        "source": DEFAULT_URL,
        "observed_at": observed_at,
        "warning": (
            "ExchangeInfo is a current metadata snapshot, not historical metadata. "
            "It must be used for stratified analysis, never to reconstruct past availability."
        ),
        "universe_symbols": len(rows),
        "found_in_snapshot": sum(row["found_in_snapshot"] for row in rows),
        "type_counts": dict(sorted(counts.items())),
        "monthly_type_counts": per_month,
        "symbols": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--universe",
        type=Path,
        default=Path("research_data/causal_futures_dataset/universe.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research_data/causal_futures_dataset/universe_contract_audit.json"),
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    universe = json.loads(args.universe.read_text(encoding="utf-8"))
    observed_at = datetime.now(timezone.utc).isoformat()
    result = audit_universe(
        universe,
        fetch_exchange_info(args.url),
        observed_at=observed_at,
    )
    result["source"] = args.url
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({
        "universe_symbols": result["universe_symbols"],
        "found_in_snapshot": result["found_in_snapshot"],
        "type_counts": result["type_counts"],
    }, ensure_ascii=True))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
