"""Build a lookahead-safe monthly USD-M Futures liquidity universe."""

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
    iter_months,
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


def select_monthly_universe(
    summaries: list[ArchiveSummary],
    *,
    size: int,
    minimum_coverage: float,
) -> list[ArchiveSummary]:
    eligible = []
    for row in summaries:
        expected = expected_bars_in_month(row.month, 60)
        coverage = row.rows / expected if expected else 0.0
        if row.quote_volume > 0 and coverage >= minimum_coverage:
            eligible.append(row)
    return sorted(eligible, key=lambda row: (-row.quote_volume, row.symbol))[:size]


def _archive_path(root: Path, archive: MonthlyArchive) -> Path:
    return root / "archives" / archive.timeframe / archive.symbol / archive.filename


def _summary_path(root: Path, archive: MonthlyArchive) -> Path:
    return root / "summaries" / archive.timeframe / archive.month / f"{archive.symbol}.json"


def _load_or_build_summary(root: Path, archive: MonthlyArchive) -> ArchiveSummary | None:
    output = _summary_path(root, archive)
    if output.exists():
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


def _list_archives(symbol: str) -> tuple[MonthlyArchive, ...]:
    return _client().list_monthly_archives(symbol, "1h")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--minimum-coverage", type=float, default=0.75)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output", type=Path, default=Path("research_data/causal_futures_dataset/universe.json"))
    args = parser.parse_args()
    if args.size < 2:
        raise ValueError("universe size must be at least two")
    if not 0 < args.minimum_coverage <= 1:
        raise ValueError("minimum coverage must be in (0, 1]")

    root = ROOT / args.root
    output = ROOT / args.output
    target_months = iter_months(args.start_month, args.end_month)
    liquidity_months = {previous_month(month) for month in target_months}
    index_client = BinanceVisionArchiveClient()
    symbols = index_client.list_symbols()
    print(f"archive symbols ending USDT: {len(symbols)}", flush=True)

    available: list[MonthlyArchive] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_list_archives, symbol): symbol
            for symbol in symbols
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            rows = future.result()
            available.extend(row for row in rows if row.month in liquidity_months)
            if completed % 100 == 0:
                print(f"indexed {completed}/{len(symbols)} symbols", flush=True)
    print(f"available 1h symbol-month archives: {len(available)}", flush=True)

    summaries: list[ArchiveSummary] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_load_or_build_summary, root, archive): archive
            for archive in available
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            summary = future.result()
            if summary is not None:
                summaries.append(summary)
            if completed % 100 == 0:
                print(f"summarized {completed}/{len(available)} archives", flush=True)

    by_month: dict[str, list[ArchiveSummary]] = {}
    for row in summaries:
        by_month.setdefault(row.month, []).append(row)
    monthly = {}
    for target in target_months:
        observed = previous_month(target)
        chosen = select_monthly_universe(
            by_month.get(observed, []),
            size=args.size,
            minimum_coverage=args.minimum_coverage,
        )
        monthly[target] = {
            "selection_observed_month": observed,
            "eligible_symbols": len([
                row for row in by_month.get(observed, [])
                if row.rows / expected_bars_in_month(observed, 60) >= args.minimum_coverage
                and row.quote_volume > 0
            ]),
            "symbols": [
                {
                    "rank": rank,
                    "symbol": row.symbol,
                    "prior_month_quote_volume": row.quote_volume,
                    "prior_month_rows": row.rows,
                }
                for rank, row in enumerate(chosen, start=1)
            ],
        }

    payload = {
        "dataset": "Binance USD-M Futures causal liquidity universe",
        "source": "https://data.binance.vision/data/futures/um/monthly/klines/",
        "start_month": args.start_month,
        "end_month": args.end_month,
        "selection_rule": (
            f"top {args.size} USDT archives by previous full-month quote volume; "
            f"minimum prior-month 1h coverage {args.minimum_coverage:.0%}"
        ),
        "archive_symbol_count": len(symbols),
        "monthly": monthly,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output)
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
