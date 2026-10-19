"""Detect newly listed crypto perpetuals from first Binance archive timestamps."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_futures_universe_metadata import DEFAULT_URL, fetch_exchange_info


def detect_launches(
    summaries: list[dict],
    exchange_info: dict,
    *,
    baseline_month: str,
) -> list[dict]:
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in summaries:
        by_symbol[str(row["symbol"])].append(row)
    metadata = {
        str(row["symbol"]): row
        for row in exchange_info.get("symbols", [])
        if row.get("symbol")
    }
    launches = []
    for symbol, rows in sorted(by_symbol.items()):
        first = min(rows, key=lambda row: str(row["month"]))
        month = str(first["month"])
        if month <= baseline_month or first.get("first_open_time_ms") is None:
            continue
        launch_time = datetime.fromtimestamp(
            int(first["first_open_time_ms"]) / 1000.0,
            tz=timezone.utc,
        )
        month_start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
        # A first archive that starts at the month boundary is not proof of a
        # listing.  Requiring an intramonth first candle prevents old contracts
        # with truncated history from being mislabeled as launches.
        if launch_time <= month_start + timedelta(hours=1):
            continue
        item = metadata.get(symbol)
        if item is None or str(item.get("underlyingType")) != "COIN":
            continue
        launches.append({
            "symbol": symbol,
            "launch_time": launch_time.isoformat(),
            "archive_month": month,
            "first_month_rows_1h": int(first["rows"]),
            "contract_type": str(item.get("contractType") or "UNKNOWN"),
            "status_at_snapshot": str(item.get("status") or "UNKNOWN"),
            "underlying_subtypes": list(item.get("underlyingSubType") or []),
        })
    return launches


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summaries-root", type=Path, default=Path("research_data/causal_futures_dataset/summaries/1h"))
    parser.add_argument("--output", type=Path, default=Path("research_data/launch_event_dataset/launch_universe.json"))
    parser.add_argument("--baseline-month", default="2025-07")
    parser.add_argument("--exchange-info-url", default=DEFAULT_URL)
    args = parser.parse_args()

    summaries = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (ROOT / args.summaries_root).glob("*/*.json")
    ]
    observed_at = datetime.now(timezone.utc).isoformat()
    exchange_info = fetch_exchange_info(args.exchange_info_url)
    launches = detect_launches(
        summaries,
        exchange_info,
        baseline_month=args.baseline_month,
    )
    payload = {
        "dataset": "Binance USD-M crypto launch event universe",
        "archive_source": "https://data.binance.vision/data/futures/um/monthly/klines/",
        "metadata_source": args.exchange_info_url,
        "metadata_observed_at": observed_at,
        "baseline_month": args.baseline_month,
        "method": (
            "first available 1h archive after baseline, first candle >1h after month start, "
            "current ExchangeInfo underlyingType=COIN"
        ),
        "warning": (
            "Current metadata classifies market type only; first archive timestamp supplies "
            "the historical launch observation. Delisted contracts absent from the current "
            "snapshot are excluded and reported as a survivorship limitation."
        ),
        "summary_symbols": len({row["symbol"] for row in summaries}),
        "launches": launches,
        "launch_count": len(launches),
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"launch_count": len(launches), "summary_symbols": payload["summary_symbols"]}))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
