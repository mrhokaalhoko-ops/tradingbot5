"""Causal case-study audit for timestamped human reference calls.

This script never places orders.  It compares what was knowable at the message
timestamp with subsequent 1m OHLC and with the production/scored momentum
detector.  Reference calls are hypotheses, not strategy training labels.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines.ta_engine import TAEngine
from strategy.filter_profiles import FILTER_PROFILES
from strategy.momentum_continuation import candidate_side, detect_momentum_impulse
from trading_v2.data import BinanceUsdMFuturesDataSource, CausalBarStore, normalize_symbol
from trading_v2.indicators import atr, average_volume, ema
from trading_v2.momentum_research import (
    MomentumResearchConfig,
    _production_context,
    apply_research_impulse_policy,
    closed_bars_to_live_frame,
    historical_hot_candidate,
)
from trading_v2.research import GlobalRegimeTimeline


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a timezone")
    return parsed


def _cache_path(cache: Path, symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
    return cache / (
        f"{normalize_symbol(symbol)}_{timeframe}_{int(start.timestamp())}_"
        f"{int(end.timestamp())}.jsonl.gz"
    )


def _load_store(cache: Path, symbol: str, start: datetime, end: datetime) -> CausalBarStore:
    clean = normalize_symbol(symbol)
    unified = f"{clean[:-4]}/USDT"
    source = BinanceUsdMFuturesDataSource()
    bars = {}
    for timeframe in ("1m", "5m", "15m", "1h"):
        path = _cache_path(cache, clean, timeframe, start, end)
        if not path.exists():
            raise FileNotFoundError(f"missing candle cache: {path}")
        bars[timeframe] = source.read_cache(path)
    return CausalBarStore(unified, bars)


def _pct_change(values, periods: int):
    if len(values) <= periods or not values[-periods - 1]:
        return None
    return ((float(values[-1]) / float(values[-periods - 1])) - 1.0) * 100.0


def simulate_reference_outcome(call: dict, bars, *, horizon_hours: int = 6) -> dict:
    timestamp = _time(call["timestamp"])
    horizon = timestamp + timedelta(hours=horizon_hours)
    side = str(call["side"]).upper()
    entry = float(call["entry"])
    stop = float(call["stop"])
    targets = [float(value) for value in call.get("targets") or ()]
    risk = abs(entry - stop)
    forward = [bar for bar in bars if timestamp <= bar.open_time <= horizon]
    target_hits = [None] * len(targets)
    filled_at = None
    stop_at = None
    ambiguous_first_bar = False
    ambiguous_entry_bar = False
    best = entry
    worst = entry

    for bar in forward:
        just_filled = False
        if filled_at is None:
            entry_touched = (
                float(bar.low) <= entry
                if side == "BUY"
                else float(bar.high) >= entry
            )
            if not entry_touched:
                continue
            filled_at = bar.close_time.isoformat()
            just_filled = True

        if side == "BUY":
            best = max(best, float(bar.high))
            worst = min(worst, float(bar.low))
            stop_hit = float(bar.low) <= stop
            hits = [float(bar.high) >= target for target in targets]
        else:
            best = min(best, float(bar.low))
            worst = max(worst, float(bar.high))
            stop_hit = float(bar.high) >= stop
            hits = [float(bar.low) <= target for target in targets]

        if just_filled and (stop_hit or any(hits)):
            ambiguous_entry_bar = True
            break

        if stop_at is None and stop_hit:
            stop_at = bar.close_time.isoformat()
        for index, hit in enumerate(hits):
            if hit and target_hits[index] is None:
                target_hits[index] = bar.close_time.isoformat()
        if stop_hit and hits and hits[0]:
            ambiguous_first_bar = True
        if stop_at is not None or (target_hits and target_hits[-1] is not None):
            break

    if risk <= 0:
        mfe_r = mae_r = None
    elif side == "BUY":
        mfe_r = (best - entry) / risk
        mae_r = (entry - worst) / risk
    else:
        mfe_r = (entry - best) / risk
        mae_r = (worst - entry) / risk
    return {
        "resolution": "1m_ohlc",
        "horizon_hours": horizon_hours,
        "entry_kind_assumption": "LIMIT_AT_REFERENCE_PRICE",
        "filled_at": filled_at,
        "stop_at": stop_at,
        "target_hits": target_hits,
        "ambiguous_entry_bar": ambiguous_entry_bar,
        "ambiguous_stop_and_tp1_same_bar": ambiguous_first_bar,
        "mfe_r": round(mfe_r, 6) if filled_at is not None and mfe_r is not None else None,
        "mae_r": round(mae_r, 6) if filled_at is not None and mae_r is not None else None,
    }


def _analyze_call(call, store, regime, ta_engine, profile, cfg):
    timestamp = _time(call["timestamp"])
    # 1m needs at least 1,441 closed bars for a causal 24h return.  Using the
    # strategy's shorter indicator history here would silently turn that field
    # into unavailable data.
    snapshot = store.snapshot(as_of=timestamp, regime=regime.at(timestamp), history_limit=2000)
    bars_1m = snapshot.closed_bars("1m")
    bars_5m = snapshot.closed_bars("5m")
    bars_15m = snapshot.closed_bars("15m")
    bars_1h = snapshot.closed_bars("1h")
    if not bars_1m or not bars_5m or not bars_15m or not bars_1h:
        raise ValueError(f"insufficient causal bars for {call['symbol']}")

    minute_all = store.bars("1m")
    minute_index = bisect_right([bar.close_time for bar in minute_all], timestamp) - 1
    candidate = historical_hot_candidate(minute_all, minute_index, config=cfg)
    detected_side = candidate_side(candidate) if candidate else None
    context, _ = _production_context(
        store,
        as_of=timestamp,
        ta_engine=ta_engine,
        profile=profile,
    )
    production_impulse = None
    scored_impulse = None
    if candidate and detected_side in {"buy", "sell"} and context:
        production_impulse = detect_momentum_impulse(
            symbol=store.symbol,
            candidate=candidate,
            bars_5m=closed_bars_to_live_frame(bars_5m),
            market_regime={"label": snapshot.regime, "known": snapshot.regime != "UNKNOWN"},
            context_15m=context,
            min_volume_ratio=cfg.min_volume_ratio,
            min_range_atr=cfg.min_range_atr,
            max_range_atr=cfg.max_range_atr,
            max_abs_24h_change_pct=cfg.max_abs_24h_change_pct,
            min_quote_volume=cfg.min_quote_volume,
        )
        scored_impulse = apply_research_impulse_policy(
            production_impulse,
            context_15m=context,
            policy="scored_v2",
        )

    close_1m = [bar.close for bar in bars_1m]
    close_5m = [bar.close for bar in bars_5m]
    close_15m = [bar.close for bar in bars_15m]
    close_1h = [bar.close for bar in bars_1h]
    last_price = float(close_1m[-1])
    volume_base = average_volume(bars_5m[-21:-1], 20) if len(bars_5m) >= 21 else 0.0
    volume_ratio = float(bars_5m[-1].volume) / volume_base if volume_base > 0 else None
    return {
        "reference": call,
        "causal_market": {
            "as_of": timestamp.isoformat(),
            "last_closed_1m_price": last_price,
            "entry_distance_bps": round(
                abs(float(call["entry"]) - last_price) / last_price * 10_000.0,
                3,
            ),
            "change_1m_pct": _pct_change(close_1m, 1),
            "change_5m_pct": _pct_change(close_1m, 5),
            "change_15m_pct": _pct_change(close_1m, 15),
            "change_1h_pct": _pct_change(close_1m, 60),
            "change_24h_pct": _pct_change(close_1m, 1440),
            "atr_5m_pct": atr(bars_5m, 14) / close_5m[-1] * 100.0,
            "volume_ratio_5m": volume_ratio,
            "trend_5m": "UP" if close_5m[-1] > ema(close_5m, 20) else "DOWN",
            "trend_15m": "UP" if close_15m[-1] > ema(close_15m, 20) else "DOWN",
            "trend_1h": "UP" if close_1h[-1] > ema(close_1h, 20) else "DOWN",
            "global_regime": snapshot.regime,
        },
        "hot_detector": candidate,
        "detected_side": detected_side,
        "production_impulse": {
            "qualified": production_impulse.get("qualified"),
            "reason": production_impulse.get("reason"),
            "failed_gates": production_impulse.get("failed_gates"),
        } if production_impulse else None,
        "scored_impulse": {
            "qualified": scored_impulse.get("qualified"),
            "reason": scored_impulse.get("reason"),
            "failed_gates": scored_impulse.get("failed_gates"),
        } if scored_impulse else None,
        "forward": simulate_reference_outcome(call, minute_all),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Causal audit of timestamped reference calls")
    parser.add_argument("--calls", required=True)
    parser.add_argument("--start", type=_time, required=True)
    parser.add_argument("--end", type=_time, required=True)
    parser.add_argument("--cache-dir", default="research_data/binance_usdm")
    parser.add_argument("--output", default="research_data/reference_call_audit.json")
    args = parser.parse_args()

    calls = json.loads((ROOT / args.calls).read_text(encoding="utf-8"))
    cache = ROOT / args.cache_dir
    symbols = list(dict.fromkeys(["BTCUSDT", "ETHUSDT", *(call["symbol"] for call in calls)]))
    stores = {symbol: _load_store(cache, symbol, args.start, args.end) for symbol in symbols}
    regime = GlobalRegimeTimeline(stores["BTCUSDT"], stores["ETHUSDT"])
    ta_engine = TAEngine()
    profile = dict(FILTER_PROFILES["smc_balanced"])
    cfg = MomentumResearchConfig()
    payload = {
        "method": "causal snapshot at message timestamp; 1m OHLC forward audit",
        "limitations": [
            "Telegram timestamps have minute precision only",
            "Displayed decimal placement was inferred then checked against market price",
            "Reference calls are a small selected sample, not independent proof of edge",
        ],
        "cases": [
            _analyze_call(call, stores[normalize_symbol(call["symbol"])], regime, ta_engine, profile, cfg)
            for call in calls
        ],
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
