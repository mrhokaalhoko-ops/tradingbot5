"""Build monthly, lookahead-safe research features from verified 5m archives."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.binance_archive import (
    MonthlyArchive,
    iter_months,
    parse_kline_archive,
    previous_month,
)
from trading_v2.causal_features import (
    add_forward_labels,
    add_structure_features,
    add_timeframe_features,
    aggregate_timeframe,
    valid_observation_mask,
)
from trading_v2.indicators import atr, ema
from trading_v2.models import Bar
from trading_v2.regime import classify_global_regime


def _archive_path(root: Path, symbol: str, month: str) -> Path:
    archive = MonthlyArchive(symbol, "5m", month)
    return root / "archives" / "5m" / symbol / archive.filename


def load_symbol_frame(root: Path, symbol: str, months: list[str]) -> pd.DataFrame:
    rows = []
    for month in months:
        path = _archive_path(root, symbol, month)
        if not path.exists():
            continue
        for row in parse_kline_archive(path):
            rows.append({
                "open_time": pd.Timestamp(row.open_time_ms, unit="ms", tz="UTC"),
                "close_time": pd.Timestamp(row.close_time_ms, unit="ms", tz="UTC"),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.base_volume,
                "quote_volume": row.quote_volume,
                "trades": row.trades,
                "taker_buy_base_volume": row.taker_buy_base_volume,
                "taker_buy_quote_volume": row.taker_buy_quote_volume,
            })
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).sort_values("open_time")
    return frame.drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)


def _as_bars(frame: pd.DataFrame, symbol: str) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            symbol=symbol,
            timeframe="1h",
            open_time=row.open_time.to_pydatetime(),
            close_time=row.close_time.to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in frame.sort_values("close_time").itertuples()
    )


def _bar_metrics(bars: tuple[Bar, ...]) -> tuple[int, float, float]:
    closes = [bar.close for bar in bars]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    six_hour_return = closes[-1] / closes[-7] - 1.0
    direction = 1 if closes[-1] > fast > slow else -1 if closes[-1] < fast < slow else 0
    atr_pct = atr(bars, 14) / closes[-1]
    return direction, six_hour_return, atr_pct


def build_regime_timeline(btc: pd.DataFrame, eth: pd.DataFrame) -> pd.DataFrame:
    btc_bars = _as_bars(btc, "BTC/USDT")
    eth_bars = _as_bars(eth, "ETH/USDT")
    btc_by_time = {bar.close_time: index for index, bar in enumerate(btc_bars)}
    eth_by_time = {bar.close_time: index for index, bar in enumerate(eth_bars)}
    common = sorted(set(btc_by_time).intersection(eth_by_time))
    rows = []
    for timestamp in common:
        btc_index, eth_index = btc_by_time[timestamp], eth_by_time[timestamp]
        btc_history = btc_bars[max(0, btc_index - 79):btc_index + 1]
        eth_history = eth_bars[max(0, eth_index - 79):eth_index + 1]
        regime = classify_global_regime(btc_history, eth_history)
        if len(btc_history) < 51 or len(eth_history) < 51:
            b = e = (0, float("nan"), float("nan"))
        else:
            b = _bar_metrics(btc_history)
            e = _bar_metrics(eth_history)
        rows.append({
            "close_time": timestamp,
            "global_regime": regime if len(btc_history) >= 51 and len(eth_history) >= 51 else "UNKNOWN",
            "btc_direction": int(b[0]),
            "eth_direction": int(e[0]),
            "btc_six_hour_return_pct": float(b[1] * 100) if not pd.isna(b[1]) else None,
            "eth_six_hour_return_pct": float(e[1] * 100) if not pd.isna(e[1]) else None,
        })
    return pd.DataFrame(rows)


def enrich_symbol_frame(frame: pd.DataFrame, regime_timeline: pd.DataFrame) -> pd.DataFrame:
    base = add_structure_features(add_timeframe_features(frame))
    base = add_forward_labels(base)
    m15 = add_timeframe_features(aggregate_timeframe(frame, 15), prefix="15m")
    m60 = add_timeframe_features(aggregate_timeframe(frame, 60), prefix="1h")
    m15_columns = ["close_time"] + [column for column in m15.columns if column.startswith("15m_")]
    m60_columns = ["close_time"] + [column for column in m60.columns if column.startswith("1h_")]
    base = pd.merge_asof(
        base.sort_values("close_time"),
        m15[m15_columns].sort_values("close_time"),
        on="close_time",
        direction="backward",
    )
    base = pd.merge_asof(
        base.sort_values("close_time"),
        m60[m60_columns].sort_values("close_time"),
        on="close_time",
        direction="backward",
    )
    if not regime_timeline.empty:
        base = pd.merge_asof(
            base.sort_values("close_time"),
            regime_timeline.sort_values("close_time"),
            on="close_time",
            direction="backward",
        )
    else:
        base["global_regime"] = "UNKNOWN"
    base["base_symbol"] = frame.iloc[0].get("symbol", "") if not frame.empty else ""
    return base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, default=Path("research_data/causal_futures_dataset/universe.json"))
    parser.add_argument("--root", type=Path, default=Path("research_data/causal_futures_dataset"))
    parser.add_argument("--output-root", type=Path, default=Path("research_data/causal_futures_dataset/features"))
    parser.add_argument("--manifest", type=Path, default=Path("research_data/causal_futures_dataset/features_manifest.json"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    universe = json.loads((ROOT / args.universe).read_text(encoding="utf-8"))
    root = ROOT / args.root
    output_root = ROOT / args.output_root
    target_months = list(universe["monthly"])
    months_by_symbol: dict[str, set[str]] = {}
    for target in target_months:
        for item in universe["monthly"][target]["symbols"]:
            months_by_symbol.setdefault(item["symbol"], set()).update(
                {target, previous_month(target)}
            )
    all_symbols = sorted(months_by_symbol)
    print(f"feature symbols: {len(all_symbols)} | months: {target_months[0]}..{target_months[-1]}", flush=True)

    anchor_frames = {
        symbol: load_symbol_frame(root, symbol, sorted(months_by_symbol[symbol]))
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    btc = anchor_frames.get("BTCUSDT", pd.DataFrame())
    eth = anchor_frames.get("ETHUSDT", pd.DataFrame())
    if btc.empty or eth.empty:
        raise RuntimeError("BTCUSDT and ETHUSDT warm-up data are required for regimes")
    regime_timeline = build_regime_timeline(
        aggregate_timeframe(btc, 60), aggregate_timeframe(eth, 60)
    )
    records = []
    for symbol in all_symbols:
        frame = anchor_frames.get(symbol)
        if frame is None:
            frame = load_symbol_frame(root, symbol, sorted(months_by_symbol[symbol]))
        if frame.empty:
            records.append({"symbol": symbol, "status": "MISSING"})
            continue
        enriched = enrich_symbol_frame(frame, regime_timeline)
        for month in target_months:
            selected = [
                item for item in universe["monthly"][month]["symbols"]
                if item["symbol"] == symbol
            ]
            if not selected:
                continue
            target = enriched[enriched["open_time"].dt.strftime("%Y-%m") == month].copy()
            if target.empty:
                records.append({"symbol": symbol, "month": month, "status": "NO_TARGET_BARS"})
                continue
            target["symbol"] = symbol
            target["target_month"] = month
            target["selection_rank"] = int(selected[0]["rank"])
            target["prior_month_quote_volume"] = float(selected[0]["prior_month_quote_volume"])
            target["observation_valid"] = valid_observation_mask(target)
            destination = output_root / month / f"{symbol}.parquet"
            if not destination.exists() or args.force:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".parquet.tmp")
                target.to_parquet(temporary, index=False, compression="zstd")
                temporary.replace(destination)
            records.append({
                "symbol": symbol,
                "month": month,
                "status": "OK",
                "rows": int(len(target)),
                "valid_rows": int(target["observation_valid"].sum()),
                "first_open_time": target["open_time"].min().isoformat(),
                "last_open_time": target["open_time"].max().isoformat(),
                "output": str(destination),
            })

    payload = {
        "dataset": "Causal Binance USD-M Futures feature dataset",
        "source": universe["source"],
        "universe": str(args.universe),
        "feature_definition": {
            "decision": "each row uses the just-closed 5m bar; higher timeframes are joined only after their close",
            "forward_labels": "future 1/3/12/48 5m closes and extrema, excluded from model inputs",
            "regime": "closed BTC/ETH 1h bars using the small deterministic global classifier",
            "costs": "no PnL is claimed by this feature build; execution replay supplies costs separately",
        },
        "partitions": records,
        "regime_rows": int(len(regime_timeline)),
    }
    manifest = ROOT / args.manifest
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(manifest)
    print(manifest, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
