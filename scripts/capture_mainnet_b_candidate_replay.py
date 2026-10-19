"""Capture read-only Mainnet candidate inputs plus Demo replay context.

Historical best-bid/ask is not available from Binance REST.  The artifact
therefore keeps exact historical Mainnet/Demo 1m klines and current contract
catalogue state, while explicitly quarantining the current Demo quote so it
cannot be mistaken for the candidate-time execution quote.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_execution.cross_environment import MAINNET_USDM_PUBLIC_URL
from shadow_runtime.market import DEMO_USDM_PUBLIC_URL
from strategy.active_altcoin_shadow import detect_active_event


PUBLIC_BASES = {
    "MAINNET": MAINNET_USDM_PUBLIC_URL,
    "DEMO": DEMO_USDM_PUBLIC_URL,
}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest().upper()


def parse_utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def public_get(base_url: str, path: str, params: dict | None = None) -> Any:
    if base_url not in PUBLIC_BASES.values():
        raise RuntimeError("UNAPPROVED_PUBLIC_BASE_URL")
    if path not in {
        "/fapi/v1/klines", "/fapi/v1/exchangeInfo",
        "/fapi/v1/ticker/bookTicker", "/fapi/v1/time",
    }:
        raise RuntimeError("UNAPPROVED_PUBLIC_PATH")
    response = requests.get(
        f"{base_url}{path}", params=params or {}, timeout=30
    )
    response.raise_for_status()
    return response.json()


def one_minute_frame(payload: list) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "open_time": pd.to_datetime(int(row[0]), unit="ms", utc=True),
            "close_time": pd.to_datetime(int(row[6]), unit="ms", utc=True),
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]),
            "volume": float(row[5]), "quote_volume": float(row[7]),
            "trades": int(row[8]),
            "taker_buy_quote_volume": float(row[10]),
        }
        for row in payload if isinstance(row, list) and len(row) >= 11
    ])


def contract(payload: dict, symbol: str) -> dict | None:
    row = next(
        (item for item in payload.get("symbols", []) if item.get("symbol") == symbol),
        None,
    )
    if row is None:
        return None
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    return {
        "symbol": symbol,
        "status": row.get("status"),
        "contract_type": row.get("contractType"),
        "base_asset": row.get("baseAsset"),
        "quote_asset": row.get("quoteAsset"),
        "margin_asset": row.get("marginAsset"),
        "filters": filters,
    }


def capture_candidate(
    spec: dict,
    mainnet_info: dict,
    demo_info: dict,
) -> dict:
    symbol = str(spec["symbol"])
    as_of = parse_utc(str(spec["as_of"]))
    closed_at = parse_utc(str(spec["closed_5m_close_time"]))
    end_ms = int(as_of.timestamp() * 1000)
    start = closed_at - timedelta(minutes=180)
    replay_end = closed_at + timedelta(minutes=5)
    mainnet_1m = public_get(
        MAINNET_USDM_PUBLIC_URL,
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": "1m", "limit": 180, "endTime": end_ms},
    )
    demo_1m = public_get(
        DEMO_USDM_PUBLIC_URL,
        "/fapi/v1/klines",
        {
            "symbol": symbol, "interval": "1m", "limit": 200,
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(replay_end.timestamp() * 1000),
        },
    )
    reconstructed = detect_active_event(symbol, one_minute_frame(mainnet_1m), as_of)
    demo_quote = public_get(
        DEMO_USDM_PUBLIC_URL,
        "/fapi/v1/ticker/bookTicker",
        {"symbol": symbol},
    )
    demo_server_time = public_get(DEMO_USDM_PUBLIC_URL, "/fapi/v1/time")
    result = {
        "requested_candidate": spec,
        "reconstructed_mainnet_event": reconstructed,
        "event_id_match": reconstructed.get("event_id") == spec.get("event_id"),
        "mainnet_1m": mainnet_1m,
        "mainnet_1m_hash": canonical_hash(mainnet_1m),
        "demo_1m_replay_context": demo_1m,
        "demo_1m_replay_context_hash": canonical_hash(demo_1m),
        "mainnet_contract": contract(mainnet_info, symbol),
        "demo_contract": contract(demo_info, symbol),
        "historical_demo_book_quote_available": False,
        "current_demo_quote_quarantined_not_for_backtest": {
            "captured_at_demo_server_ms": demo_server_time.get("serverTime"),
            "book_ticker": demo_quote,
        },
        "public_get_only": True,
        "credentials_used": False,
        "order_route": False,
    }
    result["capture_hash"] = canonical_hash(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-json", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    specs = [json.loads(value) for value in args.candidate_json]
    mainnet_info = public_get(MAINNET_USDM_PUBLIC_URL, "/fapi/v1/exchangeInfo")
    demo_info = public_get(DEMO_USDM_PUBLIC_URL, "/fapi/v1/exchangeInfo")
    artifact = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "HISTORICAL_DEMO_BEST_BID_ASK_NOT_AVAILABLE_FROM_REST",
            "CURRENT_DEMO_QUOTE_IS_QUARANTINED_AND_MUST_NOT_BE_USED_AS_HISTORICAL_FILL",
        ],
        "candidates": [
            capture_candidate(spec, mainnet_info, demo_info) for spec in specs
        ],
    }
    artifact["artifact_hash"] = canonical_hash(artifact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(args.output),
        "artifact_hash": artifact["artifact_hash"],
        "candidate_count": len(artifact["candidates"]),
        "event_id_matches": [
            row["event_id_match"] for row in artifact["candidates"]
        ],
        "public_get_only": True,
        "credentials_used": False,
        "order_route": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
