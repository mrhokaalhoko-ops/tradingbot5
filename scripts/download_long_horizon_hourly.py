"""Download a small, verified long-horizon USD-M hourly research universe.

The archives are official Binance Vision trade-price klines.  Every retained
ZIP is verified against Binance's published SHA-256 checksum by the shared
archive client.  This dataset is for preliminary offline research only: archive
timestamps do not prove first point-in-time availability or historical tick
filters.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (  # noqa: E402
    BinanceVisionArchiveClient,
    MonthlyArchive,
    parse_kline_archive,
)


DEFAULT_SYMBOLS = (
    "ADAUSDT",
    "BCHUSDT",
    "BNBUSDT",
    "BTCUSDT",
    "DOGEUSDT",
    "ETHUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "SOLUSDT",
    "XRPUSDT",
)

_THREAD_STATE = threading.local()


def _client() -> BinanceVisionArchiveClient:
    client = getattr(_THREAD_STATE, "archive_client", None)
    if client is None:
        client = BinanceVisionArchiveClient()
        _THREAD_STATE.archive_client = client
    return client


def month_range(first_month: str, last_month: str) -> tuple[str, ...]:
    first = datetime.strptime(first_month, "%Y-%m")
    last = datetime.strptime(last_month, "%Y-%m")
    if last < first:
        raise ValueError("last_month precedes first_month")
    rows: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        rows.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(rows)


def archive_tasks(
    symbols: tuple[str, ...], first_month: str, last_month: str
) -> tuple[MonthlyArchive, ...]:
    clean = tuple(sorted({str(symbol).strip().upper() for symbol in symbols if symbol}))
    return tuple(
        MonthlyArchive(symbol=symbol, timeframe="1h", month=month)
        for symbol in clean
        for month in month_range(first_month, last_month)
    )


def _archive_path(root: Path, archive: MonthlyArchive) -> Path:
    return root / "archives" / archive.timeframe / archive.symbol / archive.filename


def _download_one(root: Path, archive: MonthlyArchive) -> dict:
    destination = _archive_path(root, archive)
    downloaded = _client().download(archive, destination)
    if downloaded is None:
        return {**asdict(archive), "status": "NOT_LISTED"}
    rows = list(parse_kline_archive(downloaded))
    opens = [row.open_time_ms for row in rows]
    expected_step_ms = 60 * 60 * 1000
    gaps = sum(
        right - left != expected_step_ms
        for left, right in zip(opens, opens[1:])
    )
    return {
        **asdict(archive),
        "status": "VERIFIED",
        "relative_path": str(downloaded.relative_to(root)),
        "bytes": downloaded.stat().st_size,
        "sha256": _client().sha256(downloaded),
        "rows": len(rows),
        "first_open_time_ms": opens[0] if opens else None,
        "last_open_time_ms": opens[-1] if opens else None,
        "duplicate_open_times": len(opens) - len(set(opens)),
        "internal_step_anomalies": gaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("research_data/long_horizon_hourly"),
    )
    parser.add_argument("--first-month", default="2021-01")
    parser.add_argument("--last-month", default="2026-09")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    root = args.root if args.root.is_absolute() else ROOT / args.root
    tasks = list(archive_tasks(tuple(args.symbols), args.first_month, args.last_month))
    if args.limit > 0:
        tasks = tasks[: args.limit]
    print(f"hourly archive tasks: {len(tasks)}", flush=True)

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as executor:
        futures = {executor.submit(_download_one, root, task): task for task in tasks}
        for completed, future in enumerate(as_completed(futures), start=1):
            records.append(future.result())
            if completed % 50 == 0 or completed == len(tasks):
                print(f"processed {completed}/{len(tasks)}", flush=True)

    records.sort(key=lambda row: (row["symbol"], row["month"]))
    verified = [row for row in records if row["status"] == "VERIFIED"]
    manifest = {
        "dataset_id": "BINANCE_VISION_USDM_TRADE_PRICE_1H_LONG_HORIZON_V1",
        "research_authority": "PRELIMINARY_OFFLINE_ONLY",
        "strict_r2b_eligible": False,
        "source": "https://data.binance.vision/data/futures/um/monthly/klines/",
        "timeframe": "1h",
        "requested_first_month": args.first_month,
        "requested_last_month": args.last_month,
        "symbols": sorted(set(args.symbols)),
        "tasks": len(tasks),
        "verified_archives": len(verified),
        "not_listed_archives": len(records) - len(verified),
        "bytes": sum(int(row.get("bytes", 0)) for row in verified),
        "limitations": [
            "Archive close_time is not proof of first point-in-time availability.",
            "Historical PRICE_FILTER.tickSize intervals are not supplied.",
            "The fixed major-symbol universe has survivorship and listing-history limitations.",
        ],
        "archives": records,
    }
    root.mkdir(parents=True, exist_ok=True)
    output = root / "manifest.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
