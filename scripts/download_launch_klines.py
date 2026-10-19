"""Download first/next-month 5m archives for detected crypto launches."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (
    BinanceVisionArchiveClient,
    MonthlyArchive,
    parse_kline_archive,
)


_THREAD_STATE = threading.local()


def _client() -> BinanceVisionArchiveClient:
    value = getattr(_THREAD_STATE, "client", None)
    if value is None:
        value = BinanceVisionArchiveClient(timeout_seconds=60, retries=3)
        _THREAD_STATE.client = value
    return value


def next_month(month: str) -> str:
    value = datetime.strptime(month, "%Y-%m")
    year, number = value.year, value.month + 1
    if number == 13:
        year += 1
        number = 1
    return f"{year:04d}-{number:02d}"


def launch_tasks(universe: dict, timeframe: str = "5m") -> tuple[MonthlyArchive, ...]:
    if timeframe not in {"1m", "5m"}:
        raise ValueError("timeframe must be 1m or 5m")
    tasks = {
        (row["symbol"], month)
        for row in universe["launches"]
        for month in (row["archive_month"], next_month(row["archive_month"]))
    }
    return tuple(
        MonthlyArchive(symbol, timeframe, month)
        for symbol, month in sorted(tasks, key=lambda item: (item[1], item[0]))
    )


def _download(root: Path, archive: MonthlyArchive) -> dict:
    destination = root / "archives" / archive.timeframe / archive.symbol / archive.filename
    downloaded = _client().download(archive, destination)
    if downloaded is None:
        return {"symbol": archive.symbol, "month": archive.month, "status": "MISSING", "rows": 0}
    rows = list(parse_kline_archive(downloaded))
    timestamps = [row.open_time_ms for row in rows]
    status = "OK" if rows and len(timestamps) == len(set(timestamps)) else "INVALID"
    return {
        "symbol": archive.symbol,
        "month": archive.month,
        "status": status,
        "rows": len(rows),
        "first_open_time_ms": min(timestamps) if timestamps else None,
        "last_open_time_ms": max(timestamps) if timestamps else None,
        "bytes": downloaded.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/launch_event_dataset/launch_universe.json"))
    parser.add_argument("--archive-root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/launch_event_dataset/klines_manifest.json"))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeframe", choices=("1m", "5m"), default="5m")
    args = parser.parse_args()
    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    tasks = launch_tasks(universe, args.timeframe)
    root = ROOT / args.archive_root
    print(f"launch {args.timeframe} tasks: {len(tasks)}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(_download, root, task): task for task in tasks}
        for completed, future in enumerate(as_completed(futures), start=1):
            task = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "symbol": task.symbol, "month": task.month, "status": "ERROR", "rows": 0,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            if completed % 50 == 0:
                print(f"processed {completed}/{len(tasks)}", flush=True)
    results.sort(key=lambda row: (row["month"], row["symbol"]))
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "dataset": f"Binance USD-M launch {args.timeframe} archives",
        "source": "https://data.binance.vision/data/futures/um/monthly/klines/",
        "tasks": len(tasks),
        "status_counts": counts,
        "results": results,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"tasks": len(tasks), "status_counts": counts}), flush=True)
    print(output, flush=True)
    return 0 if not counts.get("ERROR") and not counts.get("INVALID") else 2


if __name__ == "__main__":
    raise SystemExit(main())
