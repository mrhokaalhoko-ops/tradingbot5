"""Download verified OI/position metrics and funding for the causal universe."""

from __future__ import annotations

import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (
    BinanceVisionArchiveClient,
    DailyMetricsArchive,
    MonthlyFundingArchive,
    parse_funding_archive,
    parse_metrics_archive,
    previous_month,
)


_THREAD_STATE = threading.local()


def _client() -> BinanceVisionArchiveClient:
    client = getattr(_THREAD_STATE, "archive_client", None)
    if client is None:
        client = BinanceVisionArchiveClient(timeout_seconds=60, retries=3)
        _THREAD_STATE.archive_client = client
    return client


def _month_days(month: str) -> tuple[str, ...]:
    value = datetime.strptime(month, "%Y-%m")
    count = calendar.monthrange(value.year, value.month)[1]
    return tuple(
        date(value.year, value.month, day).isoformat()
        for day in range(1, count + 1)
    )


def selected_derivatives_tasks(
    universe: dict,
) -> tuple[tuple[DailyMetricsArchive, ...], tuple[MonthlyFundingArchive, ...]]:
    metrics: set[tuple[str, str]] = set()
    funding: set[tuple[str, str]] = set()
    for month, body in universe["monthly"].items():
        first_day = datetime.strptime(month, "%Y-%m").date()
        warmup_day = (first_day - timedelta(days=1)).isoformat()
        for item in body["symbols"]:
            symbol = str(item["symbol"])
            metrics.add((symbol, warmup_day))
            metrics.update((symbol, day) for day in _month_days(month))
            funding.add((symbol, previous_month(month)))
            funding.add((symbol, month))
    return (
        tuple(DailyMetricsArchive(symbol, day) for symbol, day in sorted(metrics, key=lambda x: (x[1], x[0]))),
        tuple(MonthlyFundingArchive(symbol, month) for symbol, month in sorted(funding, key=lambda x: (x[1], x[0]))),
    )


def _destination(root: Path, archive) -> Path:
    if isinstance(archive, DailyMetricsArchive):
        month = archive.day[:7]
        return root / "archives" / "metrics" / archive.symbol / month / archive.filename
    return root / "archives" / "funding" / archive.symbol / archive.filename


def _download_validate(root: Path, archive) -> dict:
    destination = _destination(root, archive)
    downloaded = _client().download(archive, destination)
    if downloaded is None:
        return {
            "kind": "metrics" if isinstance(archive, DailyMetricsArchive) else "funding",
            "symbol": archive.symbol,
            "period": getattr(archive, "day", getattr(archive, "month", "")),
            "status": "MISSING",
            "rows": 0,
        }
    if isinstance(archive, DailyMetricsArchive):
        rows = list(parse_metrics_archive(downloaded))
        timestamps = [row.create_time_ms for row in rows]
        valid_symbol = all(row.symbol == archive.symbol for row in rows)
        kind = "metrics"
        period = archive.day
    else:
        rows = list(parse_funding_archive(downloaded))
        timestamps = [row.calc_time_ms for row in rows]
        valid_symbol = True
        kind = "funding"
        period = archive.month
    status = "OK" if rows and len(timestamps) == len(set(timestamps)) and valid_symbol else "INVALID"
    return {
        "kind": kind,
        "symbol": archive.symbol,
        "period": period,
        "status": status,
        "rows": len(rows),
        "first_time_ms": min(timestamps) if timestamps else None,
        "last_time_ms": max(timestamps) if timestamps else None,
        "bytes": downloaded.stat().st_size,
        "path": str(downloaded.relative_to(root)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/causal_futures_dataset/universe.json"))
    parser.add_argument("--root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/derivatives_manifest.json"))
    parser.add_argument("--kind", choices=("metrics", "funding", "all"), default="all")
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()

    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    metrics, funding = selected_derivatives_tasks(universe)
    tasks = (
        list(metrics) if args.kind == "metrics"
        else list(funding) if args.kind == "funding"
        else [*funding, *metrics]
    )
    root = ROOT / args.root
    print(f"derivatives archive tasks ({args.kind}): {len(tasks)}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(_download_validate, root, task): task for task in tasks}
        for completed, future in enumerate(as_completed(futures), start=1):
            task = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "kind": "metrics" if isinstance(task, DailyMetricsArchive) else "funding",
                    "symbol": task.symbol,
                    "period": getattr(task, "day", getattr(task, "month", "")),
                    "status": "ERROR",
                    "rows": 0,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
            if completed % 250 == 0:
                counts = {}
                for row in results:
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                print(f"processed {completed}/{len(tasks)} {counts}", flush=True)

    results.sort(key=lambda row: (row["kind"], row["period"], row["symbol"]))
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "dataset": "Binance USD-M causal derivatives archives",
        "sources": {
            "metrics": "https://data.binance.vision/data/futures/um/daily/metrics/",
            "funding": "https://data.binance.vision/data/futures/um/monthly/fundingRate/",
        },
        "kind": args.kind,
        "tasks": len(tasks),
        "status_counts": counts,
        "total_bytes": sum(int(row.get("bytes", 0)) for row in results),
        "results": results,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"tasks": len(tasks), "status_counts": counts, "bytes": payload["total_bytes"]}), flush=True)
    print(output, flush=True)
    return 0 if not counts.get("INVALID") and not counts.get("ERROR") else 2


if __name__ == "__main__":
    raise SystemExit(main())
