"""Download only 1m daily archives surrounding strong historical alt events."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
import json
from pathlib import Path
import sys
import threading

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (  # noqa: E402
    BinanceVisionArchiveClient,
    DailyKlineArchive,
    parse_kline_archive,
)


_THREAD_STATE = threading.local()


def _client() -> BinanceVisionArchiveClient:
    client = getattr(_THREAD_STATE, "client", None)
    if client is None:
        client = BinanceVisionArchiveClient(timeout_seconds=60, retries=3)
        _THREAD_STATE.client = client
    return client


def event_tasks(ledger: pd.DataFrame, *, min_score: float = 8.0) -> tuple[DailyKlineArchive, ...]:
    eligible = ledger["event_score"].astype(float).ge(min_score)
    if "variant" in ledger.columns:
        eligible &= ledger["variant"].eq("IMMEDIATE_CONTINUATION")
    rows = ledger.loc[eligible].copy()
    time_column = "signal_time" if "signal_time" in rows.columns else "close_time"
    rows[time_column] = pd.to_datetime(rows[time_column], utc=True)
    tasks: set[tuple[str, str]] = set()
    for row in rows.itertuples(index=False):
        timestamp = getattr(row, time_column)
        tasks.add((str(row.symbol), timestamp.strftime("%Y-%m-%d")))
        if timestamp.hour == 0:
            tasks.add((str(row.symbol), (timestamp - timedelta(days=1)).strftime("%Y-%m-%d")))
        if timestamp.hour == 23:
            tasks.add((str(row.symbol), (timestamp + timedelta(days=1)).strftime("%Y-%m-%d")))
    return tuple(DailyKlineArchive(symbol, "1m", day) for symbol, day in sorted(tasks, key=lambda item: (item[1], item[0])))


def _download(root: Path, archive: DailyKlineArchive) -> dict:
    destination = root / archive.symbol / archive.filename
    downloaded = _client().download(archive, destination)
    if downloaded is None:
        return {"symbol": archive.symbol, "day": archive.day, "status": "MISSING", "rows": 0}
    rows = list(parse_kline_archive(downloaded))
    timestamps = [row.open_time_ms for row in rows]
    status = "OK" if rows and len(timestamps) == len(set(timestamps)) else "INVALID"
    return {
        "symbol": archive.symbol,
        "day": archive.day,
        "status": status,
        "rows": len(rows),
        "bytes": downloaded.stat().st_size,
        "first_open_time_ms": min(timestamps) if timestamps else None,
        "last_open_time_ms": max(timestamps) if timestamps else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=Path("research_data/causal_futures_dataset/active_altcoin_scalper_frozen_events.parquet"))
    parser.add_argument("--output-root", type=Path, default=Path("research_data/active_altcoin_1m/archives"))
    parser.add_argument("--manifest", type=Path, default=Path("research_data/active_altcoin_1m/manifest.json"))
    parser.add_argument("--min-score", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--limit", type=int, default=0, help="Pilot-only task limit; zero downloads all tasks")
    args = parser.parse_args()
    ledger = pd.read_parquet(ROOT / args.ledger)
    tasks = event_tasks(ledger, min_score=args.min_score)
    if args.limit > 0:
        tasks = tasks[:args.limit]
    print(f"active-event 1m tasks: {len(tasks)}", flush=True)
    results = []
    root = ROOT / args.output_root
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(_download, root, task): task for task in tasks}
        for completed, future in enumerate(as_completed(futures), 1):
            task = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"symbol": task.symbol, "day": task.day, "status": "ERROR", "rows": 0, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            if completed % 25 == 0 or completed == len(tasks):
                print(f"processed {completed}/{len(tasks)}", flush=True)
    results.sort(key=lambda row: (row["day"], row["symbol"]))
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "dataset": "Targeted Binance USD-M active-altcoin event 1m archives",
        "source": "https://data.binance.vision/data/futures/um/daily/klines/",
        "minimum_event_score": args.min_score,
        "pilot_limit": args.limit,
        "tasks": len(tasks),
        "status_counts": counts,
        "total_bytes": sum(int(row.get("bytes", 0)) for row in results),
        "results": results,
    }
    output = ROOT / args.manifest
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({key: payload[key] for key in ("tasks", "status_counts", "total_bytes")}), flush=True)
    print(output, flush=True)
    return 0 if not counts.get("ERROR") and not counts.get("INVALID") else 2


if __name__ == "__main__":
    raise SystemExit(main())
