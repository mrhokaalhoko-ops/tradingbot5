"""Short credential-free connectivity probe for the R4 public stream."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shadow_runtime.models import CandleOpenEvent, ExecutionQuote
from shadow_runtime.stream import PublicMarketStream


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    stream = PublicMarketStream(("BTCUSDT",))
    stream.start()
    try:
        time.sleep(max(1.0, args.seconds))
        events = stream.drain()
    finally:
        stream.stop()
    payload = {
        "probe": "BINANCE_DEMO_PUBLIC_STREAM_ONLY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "credentials_used": False, "order_routes_used": False,
        "execution_quote_events": sum(isinstance(row, ExecutionQuote) for row in events),
        "candle_open_events": sum(isinstance(row, CandleOpenEvent) for row in events),
        "stream_errors": stream.errors, "stream_reconnects": stream.reconnects,
        "last_error": stream.last_error,
        "passed": any(isinstance(row, ExecutionQuote) for row in events) and stream.errors == 0,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
