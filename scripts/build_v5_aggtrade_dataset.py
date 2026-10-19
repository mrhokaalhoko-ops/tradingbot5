"""Resumable staged builder for the frozen V5 aggregate-trade dataset."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_v5_aggtrade_pilot import (  # noqa: E402
    aggregate_and_validate_trades,
)
from trading_v2.binance_archive import (  # noqa: E402
    BinanceVisionArchiveClient,
    DailyAggTradesArchive,
    parse_agg_trades_archive,
)


def iter_days(start_day: str, end_day: str) -> tuple[str, ...]:
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    if end < start:
        raise ValueError("end day cannot precede start day")
    rows = []
    current = start
    while current <= end:
        rows.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(rows)


def causal_monthly_symbols(
    universe: dict,
    metadata: dict,
    months: set[str],
    limit: int = 10,
) -> dict[str, tuple[str, ...]]:
    """Use each month's already causal prior-month liquidity ordering."""
    coin_symbols = {
        str(row["symbol"])
        for row in metadata.get("symbols", [])
        if row.get("underlying_type") == "COIN"
    }
    result = {}
    for month in sorted(months):
        monthly = universe.get("monthly", {}).get(month)
        if monthly is None:
            raise KeyError(f"month absent from causal universe: {month}")
        ordered = [
            str(row["symbol"])
            for row in sorted(monthly.get("symbols", []), key=lambda item: item["rank"])
            if str(row["symbol"]) in coin_symbols
        ]
        if len(ordered) < limit:
            raise ValueError(f"only {len(ordered)} COIN symbols for {month}")
        result[month] = tuple(ordered[:limit])
    return result


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Windows readers (including the live progress monitor) can briefly hold
    # the destination open. Retry the atomic swap instead of losing the whole
    # multi-hour build to a transient sharing violation.
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))


def _existing_manifest(path: Path, *, start_day: str, end_day: str) -> dict:
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("start_day") != start_day or payload.get("end_day") != end_day:
            raise ValueError("existing manifest date range differs from requested range")
        return payload
    return {
        "dataset": "V5 USD-M aggregate-trade staged dataset",
        "source": "https://data.binance.vision/data/futures/um/daily/aggTrades/",
        "authority": "RESEARCH_ONLY",
        "start_day": start_day,
        "end_day": end_day,
        "feature_clock": "causal 1-minute buckets after bucket close",
        "tasks": {},
        "complete": False,
    }


def run(
    *,
    start_day: str,
    end_day: str,
    universe_path: Path,
    metadata_path: Path,
    root: Path,
    manifest_path: Path,
    symbol_limit: int = 10,
    max_tasks: int | None = None,
) -> dict:
    days = iter_days(start_day, end_day)
    months = {day[:7] for day in days}
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    symbols_by_month = causal_monthly_symbols(
        universe, metadata, months, limit=symbol_limit
    )
    manifest = _existing_manifest(
        manifest_path, start_day=start_day, end_day=end_day
    )
    manifest["symbols_by_month"] = {
        month: list(symbols) for month, symbols in symbols_by_month.items()
    }
    client = BinanceVisionArchiveClient()
    processed_this_run = 0
    limit_reached = False
    for day in days:
        for symbol in symbols_by_month[day[:7]]:
            key = f"{day}:{symbol}"
            previous = manifest["tasks"].get(key, {})
            feature_path = root / "features_1m" / day / f"{symbol}.parquet"
            if previous.get("status") == "OK" and feature_path.exists():
                continue
            if max_tasks is not None and processed_this_run >= max_tasks:
                limit_reached = True
                break
            archive = DailyAggTradesArchive(symbol, day)
            archive_path = root / "archives" / symbol / archive.filename
            try:
                downloaded = client.download(archive, archive_path)
            except (requests.Timeout, requests.ConnectionError) as exc:
                # A transient DNS/network failure must not discard all progress.
                # Leave the task unfinished so a later resume retries it.
                manifest["tasks"][key] = {
                    "day": day,
                    "symbol": symbol,
                    "status": "RETRYABLE",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                _write_json_atomic(manifest_path, manifest)
                processed_this_run += 1
                continue
            if downloaded is None:
                manifest["tasks"][key] = {
                    "day": day, "symbol": symbol, "status": "MISSING",
                }
                _write_json_atomic(manifest_path, manifest)
                processed_this_run += 1
                continue
            features, validation = aggregate_and_validate_trades(
                parse_agg_trades_archive(downloaded)
            )
            if not validation["passed"]:
                manifest["tasks"][key] = {
                    "day": day,
                    "symbol": symbol,
                    "status": "INVALID",
                    "validation": validation,
                }
                _write_json_atomic(manifest_path, manifest)
                processed_this_run += 1
                continue
            feature_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = feature_path.with_suffix(".parquet.tmp")
            features.to_parquet(temporary, index=False, compression="zstd")
            temporary.replace(feature_path)
            manifest["tasks"][key] = {
                "day": day,
                "symbol": symbol,
                "status": "OK",
                "archive_url": archive.url,
                "archive_bytes": downloaded.stat().st_size,
                "archive_sha256": client.sha256(downloaded),
                "feature_path": str(feature_path),
                "feature_rows": int(len(features)),
                "feature_bytes": feature_path.stat().st_size,
                "validation": validation,
            }
            _write_json_atomic(manifest_path, manifest)
            processed_this_run += 1
        if limit_reached:
            break
    expected = sum(len(symbols_by_month[day[:7]]) for day in days)
    statuses = [row.get("status") for row in manifest["tasks"].values()]
    manifest["expected_tasks"] = expected
    manifest["completed_ok"] = statuses.count("OK")
    manifest["missing"] = statuses.count("MISSING")
    manifest["invalid"] = statuses.count("INVALID")
    manifest["complete"] = (
        len(statuses) == expected
        and manifest["completed_ok"] == expected
    )
    manifest["processed_this_run"] = processed_this_run
    manifest["last_run_limited"] = limit_reached
    _write_json_atomic(manifest_path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-day", default="2026-05-01")
    parser.add_argument("--end-day", default="2026-07-31")
    parser.add_argument("--symbol-limit", type=int, default=10)
    parser.add_argument(
        "--max-tasks", type=int,
        help="Process at most this many unfinished symbol-days, then exit cleanly.",
    )
    parser.add_argument(
        "--universe", type=Path,
        default=Path("research_data/causal_futures_dataset/universe.json"),
    )
    parser.add_argument(
        "--metadata", type=Path,
        default=Path(
            "research_data/causal_futures_dataset/universe_contract_audit.json"
        ),
    )
    parser.add_argument(
        "--root", type=Path,
        default=Path("research_data/v5_aggtrade_dataset"),
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=Path("research_data/v5_aggtrade_dataset/manifest.json"),
    )
    args = parser.parse_args()
    result = run(
        start_day=args.start_day,
        end_day=args.end_day,
        universe_path=ROOT / args.universe,
        metadata_path=ROOT / args.metadata,
        root=ROOT / args.root,
        manifest_path=ROOT / args.manifest,
        symbol_limit=args.symbol_limit,
        max_tasks=args.max_tasks,
    )
    print(json.dumps({
        key: result[key]
        for key in ("expected_tasks", "completed_ok", "missing", "invalid", "complete")
    }))
    if result["complete"]:
        return 0
    return 0 if args.max_tasks is not None and result["invalid"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
