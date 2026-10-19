"""Cross-sectional continuation event study; never submits orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_v2.data import BinanceUsdMFuturesDataSource, CausalBarStore, normalize_symbol
from trading_v2.event_study import (
    DirectionalEvent,
    calendar_split_events,
    summarize_events,
    summarize_feature_buckets,
)
from trading_v2.indicators import atr, average_volume, ema
from trading_v2.models import Side
from trading_v2.research import GlobalRegimeTimeline


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a timezone")
    return parsed


def _load(source, cache, symbol, timeframe, start, end):
    key = (
        f"{normalize_symbol(symbol)}_{timeframe}_{int(start.timestamp())}_"
        f"{int(end.timestamp())}.jsonl.gz"
    )
    path = cache / key
    if not path.exists():
        bars = source.fetch(symbol=symbol, timeframe=timeframe, start=start, end=end)
        source.write_cache(path, bars)
    return source.read_cache(path)


def _bucket(value, boundaries, labels):
    for boundary, label in zip(boundaries, labels):
        if value < boundary:
            return label
    return labels[-1]


def is_sample_boundary(close_time: datetime, sample_minutes: int) -> bool:
    """Binance candles close at ``...:59.999``; align to the next minute."""
    aligned_minute = int(
        (close_time + timedelta(milliseconds=1)).timestamp() // 60
    )
    return aligned_minute % sample_minutes == 0


def _candidate_features(store, as_of):
    snapshot = store.snapshot(as_of=as_of, history_limit=300)
    bars_5m = snapshot.closed_bars("5m")
    bars_15m = snapshot.closed_bars("15m")
    bars_1h = snapshot.closed_bars("1h")
    if len(bars_5m) < 289 or len(bars_15m) < 21 or len(bars_1h) < 21:
        return None
    close_5m = [float(bar.close) for bar in bars_5m]
    close_15m = [float(bar.close) for bar in bars_15m]
    close_1h = [float(bar.close) for bar in bars_1h]
    current = close_5m[-1]
    volatility = float(atr(bars_5m, 14))
    volume_base = average_volume(bars_5m[-21:-1], 20)
    return {
        "current": current,
        "return_5m_pct": (current / close_5m[-2] - 1.0) * 100.0,
        "return_1h_pct": (current / close_5m[-13] - 1.0) * 100.0,
        "return_4h_pct": (current / close_5m[-49] - 1.0) * 100.0,
        "return_24h_pct": (current / close_5m[-289] - 1.0) * 100.0,
        "atr_pct": volatility / current * 100.0 if current > 0 else 0.0,
        "atr": volatility,
        "volume_ratio": float(bars_5m[-1].volume) / volume_base if volume_base > 0 else 0.0,
        "ema_5m": float(ema(close_5m, 20)),
        "up_5m": current > ema(close_5m, 20),
        "up_15m": close_15m[-1] > ema(close_15m, 20),
        "up_1h": close_1h[-1] > ema(close_1h, 20),
    }


def _make_event(store, as_of, side, raw, regime, *, horizon_hours, costs_bps, rank_pct):
    horizon_end = as_of + timedelta(hours=horizon_hours)
    forward = store.between("5m", as_of, horizon_end)
    if not forward or forward[-1].close_time < horizon_end:
        return None
    entry = float(forward[0].open)
    exit_price = float(forward[-1].close)
    direction = 1.0 if side is Side.BUY else -1.0
    gross_bps = direction * (exit_price / entry - 1.0) * 10_000.0
    if side is Side.BUY:
        mfe_pct = (max(float(bar.high) for bar in forward) / entry - 1.0) * 100.0
        mae_pct = (1.0 - min(float(bar.low) for bar in forward) / entry) * 100.0
        trend_aligned = raw["up_5m"] and raw["up_15m"] and raw["up_1h"]
    else:
        mfe_pct = (1.0 - min(float(bar.low) for bar in forward) / entry) * 100.0
        mae_pct = (max(float(bar.high) for bar in forward) / entry - 1.0) * 100.0
        trend_aligned = not raw["up_5m"] and not raw["up_15m"] and not raw["up_1h"]
    signed_1h = direction * float(raw["return_1h_pct"])
    signed_ema_atr = (
        direction * (float(raw["current"]) - float(raw["ema_5m"])) / float(raw["atr"])
        if float(raw["atr"]) > 0
        else 0.0
    )
    aligned_regimes = (
        {"MILD_RISK_ON", "STRONG_RISK_ON"}
        if side is Side.BUY
        else {"MILD_RISK_OFF", "STRONG_RISK_OFF"}
    )
    opposite_regimes = (
        {"MILD_RISK_OFF", "STRONG_RISK_OFF"}
        if side is Side.BUY
        else {"MILD_RISK_ON", "STRONG_RISK_ON"}
    )
    regime_relation = (
        "ALIGNED" if regime in aligned_regimes
        else "OPPOSED" if regime in opposite_regimes
        else "RANGE_OR_UNKNOWN"
    )
    features = {
        "global_regime": regime,
        "return_24h_pct": float(raw["return_24h_pct"]),
        "return_1h_pct": float(raw["return_1h_pct"]),
        "volume_ratio": float(raw["volume_ratio"]),
        "atr_pct": float(raw["atr_pct"]),
        "ema_distance_atr": signed_ema_atr,
        "abs_24h_bucket": _bucket(
            abs(float(raw["return_24h_pct"])),
            (10.0, 20.0, 40.0, float("inf")),
            ("05_10", "10_20", "20_40", "40_PLUS"),
        ),
        "signed_1h_bucket": _bucket(
            signed_1h,
            (0.0, 2.0, 5.0, float("inf")),
            ("NEGATIVE", "00_02", "02_05", "05_PLUS"),
        ),
        "ema_distance_atr_bucket": _bucket(
            signed_ema_atr,
            (0.0, 1.0, 3.0, float("inf")),
            ("BEHIND_EMA", "00_01", "01_03", "03_PLUS"),
        ),
        "volume_bucket": _bucket(
            float(raw["volume_ratio"]),
            (0.75, 1.5, float("inf")),
            ("LOW", "NORMAL", "HIGH"),
        ),
        "trend_alignment": "ALIGNED" if trend_aligned else "MIXED",
        "regime_relation": regime_relation,
        "rank_bucket": "EXTREME" if rank_pct <= 0.10 or rank_pct >= 0.90 else "OUTER_20PCT",
    }
    return DirectionalEvent(
        symbol=store.symbol,
        side=side,
        timestamp=as_of,
        entry_price=entry,
        exit_price=exit_price,
        directional_gross_bps=gross_bps,
        directional_net_bps=gross_bps - costs_bps,
        opposite_net_bps=-gross_bps - costs_bps,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        features=features,
    )


def _event_payload(event):
    return {
        "symbol": event.symbol,
        "side": event.side.value,
        "timestamp": event.timestamp.isoformat(),
        "entry_price": event.entry_price,
        "exit_price": event.exit_price,
        "directional_gross_bps": event.directional_gross_bps,
        "directional_net_bps": event.directional_net_bps,
        "opposite_net_bps": event.opposite_net_bps,
        "mfe_pct": event.mfe_pct,
        "mae_pct": event.mae_pct,
        "features": dict(event.features),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-sectional causal event study")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", type=_time, required=True)
    parser.add_argument("--end", type=_time, required=True)
    parser.add_argument("--cache-dir", default="research_data/binance_usdm")
    parser.add_argument("--output", default="research_data/cross_sectional_event_study.json")
    parser.add_argument("--sample-minutes", type=int, default=60)
    parser.add_argument("--horizon-hours", type=int, default=4)
    parser.add_argument("--cooldown-hours", type=int, default=4)
    parser.add_argument("--outer-fraction", type=float, default=0.20)
    parser.add_argument("--min-abs-24h-pct", type=float, default=5.0)
    parser.add_argument("--costs-bps", type=float, default=12.0)
    args = parser.parse_args()

    if not 0 < args.outer_fraction <= 0.5:
        raise ValueError("outer-fraction must be in (0, 0.5]")
    if args.sample_minutes < 5 or args.sample_minutes % 5:
        raise ValueError("sample-minutes must be a positive multiple of 5")
    source = BinanceUsdMFuturesDataSource()
    cache = ROOT / args.cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    symbols = list(dict.fromkeys(["BTCUSDT", "ETHUSDT", *args.symbols]))
    stores = {}
    for symbol in symbols:
        clean = normalize_symbol(symbol)
        stores[clean] = CausalBarStore(
            f"{clean[:-4]}/USDT",
            {
                timeframe: _load(source, cache, clean, timeframe, args.start, args.end)
                for timeframe in ("5m", "15m", "1h")
            },
        )
    regime = GlobalRegimeTimeline(stores["BTCUSDT"], stores["ETHUSDT"])
    schedule = [
        bar.close_time
        for bar in stores["BTCUSDT"].bars("5m")
        if args.start <= bar.close_time <= args.end
        and is_sample_boundary(bar.close_time, args.sample_minutes)
    ]
    tradable = [normalize_symbol(symbol) for symbol in args.symbols]
    cooldown_until = {}
    events = []
    for as_of in schedule:
        rows = []
        for symbol in tradable:
            raw = _candidate_features(stores[symbol], as_of)
            if raw is not None:
                rows.append((symbol, raw))
        if len(rows) < 2:
            continue
        ranked = sorted(rows, key=lambda item: item[1]["return_24h_pct"])
        count = max(1, int(math.ceil(len(ranked) * args.outer_fraction)))
        selections = []
        for rank, (symbol, raw) in enumerate(ranked):
            rank_pct = rank / max(1, len(ranked) - 1)
            if rank < count and raw["return_24h_pct"] <= -args.min_abs_24h_pct:
                selections.append((symbol, Side.SELL, raw, rank_pct))
            elif rank >= len(ranked) - count and raw["return_24h_pct"] >= args.min_abs_24h_pct:
                selections.append((symbol, Side.BUY, raw, rank_pct))
        for symbol, side, raw, rank_pct in selections:
            if cooldown_until.get(symbol) and as_of < cooldown_until[symbol]:
                continue
            event = _make_event(
                stores[symbol],
                as_of,
                side,
                raw,
                regime.at(as_of),
                horizon_hours=args.horizon_hours,
                costs_bps=args.costs_bps,
                rank_pct=rank_pct,
            )
            if event is not None:
                events.append(event)
                cooldown_until[symbol] = as_of + timedelta(hours=args.cooldown_hours)

    splits = calendar_split_events(events, start=args.start, end=args.end)
    feature_names = (
        "global_regime",
        "abs_24h_bucket",
        "signed_1h_bucket",
        "ema_distance_atr_bucket",
        "volume_bucket",
        "trend_alignment",
        "regime_relation",
        "rank_bucket",
    )
    payload = {
        "method": {
            "study_start": args.start.isoformat(),
            "study_end": args.end.isoformat(),
            "symbols": tradable,
            "entry": "next 5m open",
            "exit": f"mark-to-market after {args.horizon_hours}h",
            "costs_bps": args.costs_bps,
            "episode_cooldown_hours": args.cooldown_hours,
            "sample_minutes": args.sample_minutes,
            "outer_fraction": args.outer_fraction,
            "min_abs_24h_pct": args.min_abs_24h_pct,
            "note": "Directional event study only; not a stop/target trading strategy.",
        },
        "summary": summarize_events(events),
        "splits": {
            name: {
                "summary": summarize_events(rows),
                "feature_buckets": {
                    feature: summarize_feature_buckets(rows, feature)
                    for feature in feature_names
                },
            }
            for name, rows in splits.items()
        },
        "events": [_event_payload(event) for event in events],
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output)
    print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
