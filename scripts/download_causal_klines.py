"""Download the monthly 5m partitions selected by the causal universe."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (
    ArchiveSummary,
    BinanceVisionArchiveClient,
    MonthlyArchive,
    expected_bars_in_month,
    previous_month,
    summarize_archive,
)


_THREAD_STATE = threading.local()


def _client() -> BinanceVisionArchiveClient:
    client = getattr(_THREAD_STATE, "archive_client", None)
    if client is None:
        client = BinanceVisionArchiveClient()
        _THREAD_STATE.archive_client = client
    return client


def selected_tasks(universe: dict) -> tuple[MonthlyArchive, ...]:
    tasks: set[tuple[str, str]] = set()
    for target_month, row in universe["monthly"].items():
        # The previous month is retained for warm-up (24h returns, EMA and ATR)
        # while the target month is the only period used to emit events.
        months = {target_month, previous_month(target_month)}
        for item in row["symbols"]:
            for month in months:
                tasks.add((item["symbol"], month))
    return tuple(
        MonthlyArchive(symbol, "5m", month)
        for symbol, month in sorted(tasks, key=lambda item: (item[1], item[0]))
    )


def _archive_path(root: Path, archive: MonthlyArchive) -> Path:
    return root / "archives" / archive.timeframe / archive.symbol / archive.filename


def _summary_path(root: Path, archive: MonthlyArchive) -> Path:
    return root / "summaries" / archive.timeframe / archive.month / f"{archive.symbol}.json"


def _download_and_summarize(root: Path, archive: MonthlyArchive) -> ArchiveSummary | None:
    output = _summary_path(root, archive)
    if output.exists() and _archive_path(root, archive).exists():
        return ArchiveSummary(**json.loads(output.read_text(encoding="utf-8")))
    downloaded = _client().download(archive, _archive_path(root, archive))
    if downloaded is None:
        return None
    summary = summarize_archive(downloaded, archive)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    temporary.replace(output)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/causal_futures_dataset/universe.json"))
    parser.add_argument("--root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/klines_manifest.json"))
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    root = ROOT / args.root
    tasks = selected_tasks(universe)
    print(f"5m archive tasks: {len(tasks)}", flush=True)
    summaries: list[ArchiveSummary] = []
    missing: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_download_and_summarize, root, archive): archive
            for archive in tasks
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            archive = futures[future]
            summary = future.result()
            if summary is None:
                missing.append(asdict(archive))
            else:
                summaries.append(summary)
            if completed % 100 == 0:
                print(f"processed {completed}/{len(tasks)}", flush=True)

    coverage = []
    for row in summaries:
        expected = expected_bars_in_month(row.month, 5)
        coverage.append({
            "symbol": row.symbol,
            "month": row.month,
            "rows": row.rows,
            "expected_rows": expected,
            "coverage": row.rows / expected if expected else 0.0,
            "duplicate_free": row.rows == len({
                item.open_time_ms
                for item in __import__("trading_v2.binance_archive", fromlist=["parse_kline_archive"]).parse_kline_archive(
                    _archive_path(root, MonthlyArchive(row.symbol, "5m", row.month))
                )
            }),
        })
    payload = {
        "dataset": "Binance USD-M Futures causal 5m kline partitions",
        "source": "https://data.binance.vision/data/futures/um/monthly/klines/",
        "universe": str(args.universe),
        "tasks": len(tasks),
        "downloaded": len(summaries),
        "missing_archives": missing,
        "coverage": coverage,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output)
    print(f"downloaded: {len(summaries)} | missing: {len(missing)}", flush=True)
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
