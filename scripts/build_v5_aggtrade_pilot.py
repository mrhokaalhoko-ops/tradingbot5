"""Download and validate a small V5 USD-M aggregate-trade pilot.

The pilot has no execution authority.  It proves data availability, integrity,
schema, causal aggregation and storage cost before any multi-month download.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (  # noqa: E402
    BinanceVisionArchiveClient,
    DailyAggTradesArchive,
    parse_agg_trades_archive,
)


DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "HYPEUSDT",
    "XRPUSDT", "WLDUSDT", "LABUSDT", "BNBUSDT", "DOGEUSDT",
)


def aggregate_trades(rows, frequency: str = "1min") -> pd.DataFrame:
    records = [asdict(row) for row in rows]
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records).sort_values(
        ["transact_time_ms", "aggregate_trade_id"]
    )
    frame["timestamp"] = pd.to_datetime(
        frame["transact_time_ms"], unit="ms", utc=True
    )
    frame["quote_notional"] = frame["price"] * frame["quantity"]
    frame["buy_notional"] = np.where(
        ~frame["is_buyer_maker"], frame["quote_notional"], 0.0
    )
    frame["sell_notional"] = np.where(
        frame["is_buyer_maker"], frame["quote_notional"], 0.0
    )
    frame["signed_notional"] = frame["buy_notional"] - frame["sell_notional"]
    frame["price_x_notional"] = frame["price"] * frame["quote_notional"]
    frame["bucket"] = frame["timestamp"].dt.floor(frequency)
    grouped = frame.groupby("bucket", sort=True)
    result = grouped.agg(
        first_price=("price", "first"),
        high_price=("price", "max"),
        low_price=("price", "min"),
        last_price=("price", "last"),
        aggregate_trades=("aggregate_trade_id", "size"),
        first_aggregate_trade_id=("aggregate_trade_id", "first"),
        last_aggregate_trade_id=("aggregate_trade_id", "last"),
        total_notional=("quote_notional", "sum"),
        buy_notional=("buy_notional", "sum"),
        sell_notional=("sell_notional", "sum"),
        signed_notional=("signed_notional", "sum"),
        price_x_notional=("price_x_notional", "sum"),
        max_trade_notional=("quote_notional", "max"),
    ).reset_index()
    nonzero = result["total_notional"].replace(0.0, np.nan)
    result["vwap"] = result["price_x_notional"] / nonzero
    result["aggressor_imbalance"] = result["signed_notional"] / nonzero
    result["largest_trade_share"] = result["max_trade_notional"] / nonzero
    result["return_bps"] = (
        result["last_price"] / result["first_price"] - 1.0
    ) * 10_000.0
    return result.drop(columns=["price_x_notional"])


def aggregate_and_validate_trades(rows, frequency: str = "1min"):
    """Validate and aggregate a trade iterator with bounded memory.

    Daily BTC archives can contain more than a million rows.  This streaming
    path keeps only the current minute accumulator and the ~1,440 output rows,
    rather than materialising every aggregate trade as Python objects twice.
    """
    output = []
    errors = set()
    count = 0
    first_timestamp = last_timestamp = None
    first_id = last_id = None
    buy_rows = sell_rows = 0
    current_bucket = None
    accumulator = None

    def flush():
        if accumulator is None:
            return
        total = accumulator["total_notional"]
        output.append({
            **accumulator,
            "vwap": (
                accumulator["price_x_notional"] / total
                if total > 0 else np.nan
            ),
            "aggressor_imbalance": (
                accumulator["signed_notional"] / total
                if total > 0 else np.nan
            ),
            "largest_trade_share": (
                accumulator["max_trade_notional"] / total
                if total > 0 else np.nan
            ),
            "return_bps": (
                accumulator["last_price"] / accumulator["first_price"] - 1.0
            ) * 10_000.0,
        })

    for row in rows:
        identifier = row.aggregate_trade_id
        timestamp = row.transact_time_ms
        if first_id is None:
            first_id = identifier
            first_timestamp = timestamp
        else:
            if identifier <= last_id:
                errors.add("NON_INCREASING_AGGREGATE_TRADE_ID")
                if identifier == last_id:
                    errors.add("DUPLICATE_AGGREGATE_TRADE_ID")
            if timestamp < last_timestamp:
                errors.add("DECREASING_TIMESTAMP")
        if row.price <= 0 or row.quantity <= 0:
            errors.add("NON_POSITIVE_PRICE_OR_QUANTITY")
        count += 1
        last_id = identifier
        last_timestamp = timestamp
        if row.is_buyer_maker:
            sell_rows += 1
        else:
            buy_rows += 1

        bucket = pd.Timestamp(timestamp, unit="ms", tz="UTC").floor(frequency)
        notional = row.quote_notional
        buy_notional = 0.0 if row.is_buyer_maker else notional
        sell_notional = notional if row.is_buyer_maker else 0.0
        if current_bucket is None or bucket != current_bucket:
            flush()
            current_bucket = bucket
            accumulator = {
                "bucket": bucket,
                "first_price": row.price,
                "high_price": row.price,
                "low_price": row.price,
                "last_price": row.price,
                "aggregate_trades": 1,
                "first_aggregate_trade_id": identifier,
                "last_aggregate_trade_id": identifier,
                "total_notional": notional,
                "buy_notional": buy_notional,
                "sell_notional": sell_notional,
                "signed_notional": buy_notional - sell_notional,
                "price_x_notional": row.price * notional,
                "max_trade_notional": notional,
            }
            continue
        accumulator["high_price"] = max(accumulator["high_price"], row.price)
        accumulator["low_price"] = min(accumulator["low_price"], row.price)
        accumulator["last_price"] = row.price
        accumulator["aggregate_trades"] += 1
        accumulator["last_aggregate_trade_id"] = identifier
        accumulator["total_notional"] += notional
        accumulator["buy_notional"] += buy_notional
        accumulator["sell_notional"] += sell_notional
        accumulator["signed_notional"] += buy_notional - sell_notional
        accumulator["price_x_notional"] += row.price * notional
        accumulator["max_trade_notional"] = max(
            accumulator["max_trade_notional"], notional
        )
    flush()
    if count == 0:
        errors.add("EMPTY_ARCHIVE")
    frame = pd.DataFrame(output)
    if not frame.empty:
        frame = frame.drop(columns=["price_x_notional"])
    validation = {
        "rows": count,
        "first_timestamp_ms": first_timestamp,
        "last_timestamp_ms": last_timestamp,
        "first_aggregate_trade_id": first_id,
        "last_aggregate_trade_id": last_id,
        "aggressor_buy_rows": buy_rows,
        "aggressor_sell_rows": sell_rows,
        "errors": sorted(errors),
        "passed": not errors,
    }
    return frame, validation


def validate_rows(rows) -> dict:
    rows = list(rows)
    if not rows:
        return {"rows": 0, "passed": False, "errors": ["EMPTY_ARCHIVE"]}
    errors = []
    ids = [row.aggregate_trade_id for row in rows]
    times = [row.transact_time_ms for row in rows]
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_AGGREGATE_TRADE_ID")
    if any(right <= left for left, right in zip(ids, ids[1:])):
        errors.append("NON_INCREASING_AGGREGATE_TRADE_ID")
    if any(right < left for left, right in zip(times, times[1:])):
        errors.append("DECREASING_TIMESTAMP")
    if any(row.price <= 0 or row.quantity <= 0 for row in rows):
        errors.append("NON_POSITIVE_PRICE_OR_QUANTITY")
    return {
        "rows": len(rows),
        "first_timestamp_ms": times[0],
        "last_timestamp_ms": times[-1],
        "first_aggregate_trade_id": ids[0],
        "last_aggregate_trade_id": ids[-1],
        "aggressor_buy_rows": sum(not row.is_buyer_maker for row in rows),
        "aggressor_sell_rows": sum(row.is_buyer_maker for row in rows),
        "errors": errors,
        "passed": not errors,
    }


def run(symbols: tuple[str, ...], day: str, root: Path) -> dict:
    client = BinanceVisionArchiveClient()
    archive_root = root / "archives" / "daily_aggtrades"
    feature_root = root / "features_1m" / day
    reports = []
    for symbol in symbols:
        archive = DailyAggTradesArchive(symbol, day)
        destination = archive_root / symbol / archive.filename
        path = client.download(archive, destination)
        if path is None:
            reports.append({"symbol": symbol, "status": "MISSING"})
            continue
        features, validation = aggregate_and_validate_trades(
            parse_agg_trades_archive(path)
        )
        output = feature_root / f"{symbol}.parquet"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".parquet.tmp")
        features.to_parquet(temporary, index=False, compression="zstd")
        temporary.replace(output)
        reports.append({
            "symbol": symbol,
            "status": "OK" if validation["passed"] else "INVALID",
            "archive": str(path),
            "archive_bytes": path.stat().st_size,
            "archive_sha256": client.sha256(path),
            "feature_output": str(output),
            "feature_rows": int(len(features)),
            "feature_bytes": output.stat().st_size,
            "validation": validation,
        })
    return {
        "dataset": "V5 USD-M aggregate-trade pilot",
        "source": "https://data.binance.vision/data/futures/um/daily/aggTrades/",
        "authority": "RESEARCH_ONLY",
        "day": day,
        "symbols": list(symbols),
        "feature_clock": "causal 1-minute buckets after bucket close",
        "reports": reports,
        "passed": bool(reports) and all(row["status"] == "OK" for row in reports),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default="2026-07-15")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--root", type=Path,
        default=Path("research_data/v5_aggtrade_pilot"),
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=Path("research_data/v5_aggtrade_pilot/manifest.json"),
    )
    args = parser.parse_args()
    result = run(tuple(args.symbols), args.day, ROOT / args.root)
    manifest = ROOT / args.manifest
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(manifest)
    print(json.dumps({
        "passed": result["passed"],
        "symbols": len(result["reports"]),
        "rows": sum(
            row.get("validation", {}).get("rows", 0) for row in result["reports"]
        ),
        "archive_bytes": sum(row.get("archive_bytes", 0) for row in result["reports"]),
    }))
    print(manifest)
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
