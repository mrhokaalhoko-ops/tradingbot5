"""Read-only live-data smoke test for the active-altcoin shadow detector."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.active_altcoin_shadow import detect_active_event  # noqa: E402
from trading_v2.active_altcoin_scalper import MAJOR_SYMBOLS  # noqa: E402
from utils.active_altcoin_shadow_monitor import ActiveAltcoinShadowMonitor  # noqa: E402
from utils.hot_list_monitor import BINANCE_USDM_PUBLIC_URL  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    session = requests.Session()
    server_time = session.get(f"{BINANCE_USDM_PUBLIC_URL}/fapi/v1/time", timeout=15)
    server_time.raise_for_status()
    now = datetime.fromtimestamp(server_time.json()["serverTime"] / 1000, tz=timezone.utc)
    response = session.get(f"{BINANCE_USDM_PUBLIC_URL}/fapi/v1/ticker/24hr", timeout=15)
    response.raise_for_status()
    tickers = [
        row for row in response.json()
        if str(row.get("symbol") or "").endswith("USDT")
        and str(row.get("symbol") or "") not in MAJOR_SYMBOLS
        and float(row.get("quoteVolume") or 0) >= 2_000_000
    ]
    tickers.sort(key=lambda row: abs(float(row.get("priceChangePercent") or 0)), reverse=True)
    symbols = [row["symbol"] for row in tickers[: max(1, args.limit)]]

    def inspect(symbol):
        monitor = ActiveAltcoinShadowMonitor(state_manager=None, session=requests.Session())
        bars = monitor._fetch_1m(symbol)
        return detect_active_event(symbol, bars, now)

    events = []
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(inspect, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                events.append(future.result())
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
    base = [row for row in events if row.get("base_qualified")]
    scored = [row for row in base if row.get("score_passed")]
    qualified = [row for row in scored if row.get("qualified")]
    print({
        "server_time": now.isoformat(),
        "symbols_checked": len(symbols),
        "parsed": len(events),
        "errors": len(errors),
        "base_events": len(base),
        "score_pass": len(scored),
        "micro_gate_pass": len(qualified),
    })
    for event in sorted(base, key=lambda row: row["event_score"], reverse=True)[:10]:
        print({
            "symbol": event["symbol"], "side": event["side"],
            "score": round(event["event_score"], 4),
            "return_5m_pct": round(event["return_5m_pct"], 4),
            "return_20m_pct": round(event["return_20m_pct"], 4),
            "volume_ratio": round(event["volume_ratio"], 4),
            "flow_price_agreement": round(event["micro"].get("flow_price_agreement", 0), 6),
            "qualified": event["qualified"], "reason": event["reason"],
        })
    if errors:
        print({"first_errors": errors[:5]})
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
