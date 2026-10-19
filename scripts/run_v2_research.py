"""Download public futures candles and run the execution-free V2 replay.

Example:
    python scripts/run_v2_research.py --symbols BTCUSDT ETHUSDT SOLUSDT \
        --start 2026-01-01T00:00:00Z --end 2026-04-01T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.data import BinanceUsdMFuturesDataSource, CausalBarStore, normalize_symbol
from trading_v2.evaluation import (
    enforce_portfolio_capacity,
    summarize,
    summarize_splits,
    summarize_time_splits,
)
from trading_v2.research import GlobalRegimeTimeline, evaluate_strategy
from trading_v2.strategies import (
    BreakoutContinuationStrategy,
    PullbackReclaimStrategy,
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a timezone")
    return parsed


def _load(
    source: BinanceUsdMFuturesDataSource,
    cache: Path,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
):
    key = f"{normalize_symbol(symbol)}_{timeframe}_{int(start.timestamp())}_{int(end.timestamp())}.jsonl.gz"
    path = cache / key
    if path.exists():
        return source.read_cache(path)
    bars = source.fetch(symbol=symbol, timeframe=timeframe, start=start, end=end)
    source.write_cache(path, bars)
    return bars


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal Binance USD-M V2 research")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", type=_time, required=True)
    parser.add_argument("--end", type=_time, required=True)
    parser.add_argument("--cache-dir", default="research_data/binance_usdm")
    parser.add_argument("--output", default="research_data/v2_research_result.json")
    parser.add_argument("--max-open-positions", type=int, default=5)
    args = parser.parse_args()

    source = BinanceUsdMFuturesDataSource()
    cache = ROOT / args.cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    timeframes = ("1m", "5m", "15m", "1h")
    symbols = list(dict.fromkeys(["BTCUSDT", "ETHUSDT", *args.symbols]))
    stores = {}
    for symbol in symbols:
        unified = f"{normalize_symbol(symbol)[:-4]}/USDT"
        stores[normalize_symbol(symbol)] = CausalBarStore(
            unified,
            {
                timeframe: _load(source, cache, symbol, timeframe, args.start, args.end)
                for timeframe in timeframes
            },
        )

    regime = GlobalRegimeTimeline(stores["BTCUSDT"], stores["ETHUSDT"])
    strategies = (
        PullbackReclaimStrategy(),
        BreakoutContinuationStrategy(),
    )
    results = {}
    portfolio_rows = {strategy.strategy_id: [] for strategy in strategies}
    for symbol in args.symbols:
        symbol_results = {}
        for strategy in strategies:
            rows = evaluate_strategy(
                strategy,
                stores[normalize_symbol(symbol)],
                start=args.start,
                end=args.end,
                regime_at=regime.at,
            )
            symbol_results[strategy.strategy_id] = summarize_splits(rows)
            portfolio_rows[strategy.strategy_id].extend(rows)
        results[symbol] = symbol_results

    portfolio = {}
    for strategy_id, rows in portfolio_rows.items():
        raw_rows = tuple(rows)
        selection = enforce_portfolio_capacity(
            raw_rows,
            max_open_positions=args.max_open_positions,
        )
        portfolio[strategy_id] = {
            "raw_all": summarize(raw_rows),
            "raw_time_splits": summarize_time_splits(
                raw_rows,
                start=args.start,
                end=args.end,
            ),
            "capacity_adjusted_all": summarize(selection.records),
            "capacity_adjusted_time_splits": summarize_time_splits(
                selection.records,
                start=args.start,
                end=args.end,
            ),
            "max_open_positions": args.max_open_positions,
            "rejected_capacity": selection.rejected_capacity,
            "rejected_symbol_busy": selection.rejected_symbol_busy,
        }
    results["_portfolio"] = portfolio
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
