"""Causal research kernel for fast altcoin event entries.

This module has no exchange client and no order API.  It only turns historical
closed-bar rows into a replay ledger under the frozen protocol documented at
the repository root.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log1p
from typing import Iterable

import numpy as np
import pandas as pd


MAJOR_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
}


@dataclass(frozen=True)
class ActiveAltcoinConfig:
    min_abs_5m_pct: float = 1.0
    min_abs_20m_pct: float = 2.0
    min_volume_ratio: float = 1.5
    min_quote_volume_5m: float = 250_000.0
    min_body_fraction: float = 0.50
    directional_close_boundary: float = 0.65
    max_candidates_per_time: int = 2
    signal_cooldown_minutes: int = 60
    pullback_atr: float = 0.25
    reclaim_distance_atr: float = 0.10
    hard_invalidation_atr: float = 1.50
    pullback_wait_bars: int = 6
    min_risk_atr: float = 0.50
    max_risk_atr: float = 2.00
    stop_buffer_atr: float = 0.10
    target_r: float = 1.50
    holding_bars: int = 12
    round_trip_cost_bps: float = 12.0
    max_open_positions: int = 5


def _side(row: pd.Series) -> str:
    return "LONG" if float(row["return_1_pct"]) > 0 else "SHORT"


def select_hot_events(
    frame: pd.DataFrame,
    config: ActiveAltcoinConfig = ActiveAltcoinConfig(),
) -> pd.DataFrame:
    """Select causal hot-altcoin events with cross-sectional capacity limits."""

    if frame.empty:
        return frame.copy()
    rows = frame.copy()
    rows["close_time"] = pd.to_datetime(rows["close_time"], utc=True)
    signed_5m = rows["return_1_pct"].astype(float)
    signed_20m = rows["return_4_pct"].astype(float)
    candle_range = rows["high"].astype(float) - rows["low"].astype(float)
    rows["body_fraction"] = (
        (rows["close"].astype(float) - rows["open"].astype(float)).abs()
        / candle_range.where(candle_range.gt(0))
    ).fillna(0.0)
    same_direction = np.sign(signed_5m) == np.sign(signed_20m)
    directional_close = np.where(
        signed_5m > 0,
        rows["close_location"].astype(float) >= config.directional_close_boundary,
        rows["close_location"].astype(float) <= 1.0 - config.directional_close_boundary,
    )
    eligible = rows[
        rows["observation_valid"].fillna(False)
        & ~rows["symbol"].isin(MAJOR_SYMBOLS)
        & signed_5m.abs().ge(config.min_abs_5m_pct)
        & signed_20m.abs().ge(config.min_abs_20m_pct)
        & same_direction
        & rows["volume_ratio"].astype(float).ge(config.min_volume_ratio)
        & rows["quote_volume"].astype(float).ge(config.min_quote_volume_5m)
        & rows["body_fraction"].ge(config.min_body_fraction)
        & directional_close
        & rows["atr"].astype(float).gt(0)
    ].copy()
    if eligible.empty:
        return eligible
    eligible["side"] = eligible.apply(_side, axis=1)
    eligible["event_score"] = (
        eligible["return_1_pct"].abs()
        + 0.5 * eligible["return_4_pct"].abs()
        + eligible["volume_ratio"].clip(lower=0).map(log1p)
    )
    eligible = (
        eligible.sort_values(["close_time", "event_score"], ascending=[True, False])
        .groupby("close_time", sort=True, as_index=False)
        .head(config.max_candidates_per_time)
    )
    accepted = []
    cooldown_until: dict[str, pd.Timestamp] = {}
    cooldown = pd.Timedelta(minutes=config.signal_cooldown_minutes)
    for index, row in eligible.sort_values(["close_time", "event_score"], ascending=[True, False]).iterrows():
        symbol = str(row["symbol"])
        timestamp = row["close_time"]
        if timestamp < cooldown_until.get(symbol, pd.Timestamp.min.tz_localize("UTC")):
            continue
        accepted.append(index)
        cooldown_until[symbol] = timestamp + cooldown
    return eligible.loc[accepted].sort_values(["close_time", "event_score"], ascending=[True, False])


def _bar_outcome(side: str, bar: pd.Series, stop: float, target: float) -> str | None:
    if side == "LONG":
        stop_hit = float(bar["low"]) <= stop
        target_hit = float(bar["high"]) >= target
    else:
        stop_hit = float(bar["high"]) >= stop
        target_hit = float(bar["low"]) <= target
    if stop_hit and target_hit:
        return "AMBIGUOUS_AS_LOSS"
    if stop_hit:
        return "SL_FIRST"
    if target_hit:
        return "TP_FIRST"
    return None


def _finish_trade(
    *,
    event: pd.Series,
    bars: pd.DataFrame,
    entry_index: int,
    entry: float,
    stop: float,
    target: float,
    variant: str,
    config: ActiveAltcoinConfig,
) -> dict:
    side = str(event["side"])
    risk = abs(entry - stop)
    direction = 1.0 if side == "LONG" else -1.0
    path = bars.iloc[entry_index: entry_index + config.holding_bars]
    mfe_r = 0.0
    mae_r = 0.0
    status = "TIME_EXIT"
    exit_price = float(path.iloc[-1]["close"])
    exited_at = path.iloc[-1]["close_time"]
    for _, bar in path.iterrows():
        if side == "LONG":
            mfe_r = max(mfe_r, (float(bar["high"]) - entry) / risk)
            mae_r = max(mae_r, (entry - float(bar["low"])) / risk)
        else:
            mfe_r = max(mfe_r, (entry - float(bar["low"])) / risk)
            mae_r = max(mae_r, (float(bar["high"]) - entry) / risk)
        outcome = _bar_outcome(side, bar, stop, target)
        if outcome:
            status = outcome
            exit_price = stop if outcome != "TP_FIRST" else target
            exited_at = bar["close_time"]
            break
    gross_r = direction * (exit_price - entry) / risk
    if status == "AMBIGUOUS_AS_LOSS":
        gross_r = -1.0
    cost_r = (config.round_trip_cost_bps / 10_000.0 * entry) / risk
    return {
        "variant": variant,
        "symbol": str(event["symbol"]),
        "side": side,
        "signal_time": event["close_time"],
        "filled_at": bars.iloc[entry_index]["open_time"],
        "exited_at": exited_at,
        "entry": entry,
        "stop": stop,
        "target": target,
        "status": status,
        "gross_r": gross_r,
        "net_r": gross_r - cost_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "global_regime": event.get("global_regime", "UNKNOWN"),
        "event_score": float(event["event_score"]),
        "return_5m_pct": float(event["return_1_pct"]),
        "return_20m_pct": float(event["return_4_pct"]),
        "volume_ratio": float(event["volume_ratio"]),
        "quote_volume_5m": float(event["quote_volume"]),
        "atr_pct": float(event["atr"]) / float(event["close"]) * 100.0,
        "body_fraction": float(event["body_fraction"]),
        "close_location": float(event["close_location"]),
    }


def replay_event(
    event: pd.Series,
    symbol_bars: pd.DataFrame,
    variant: str,
    config: ActiveAltcoinConfig = ActiveAltcoinConfig(),
) -> dict:
    """Replay one frozen event; unfilled/invalid rows remain in the ledger."""

    bars = symbol_bars.sort_values("open_time").reset_index(drop=True)
    locations = bars.index[bars["close_time"].eq(event["close_time"])].tolist()
    if not locations:
        return {"variant": variant, "symbol": event["symbol"], "side": event["side"], "signal_time": event["close_time"], "status": "MISSING_SIGNAL_BAR"}
    signal_index = locations[0]
    atr = float(event["atr"])
    side = str(event["side"])
    if variant == "IMMEDIATE_CONTINUATION":
        entry_index = signal_index + 1
        if entry_index + config.holding_bars > len(bars):
            return {"variant": variant, "symbol": event["symbol"], "side": side, "signal_time": event["close_time"], "status": "NO_FORWARD_DATA"}
        entry = float(bars.iloc[entry_index]["open"])
        stop = entry - atr if side == "LONG" else entry + atr
        target = entry + config.target_r * atr if side == "LONG" else entry - config.target_r * atr
        return _finish_trade(event=event, bars=bars, entry_index=entry_index, entry=entry, stop=stop, target=target, variant=variant, config=config)
    if variant != "PULLBACK_RECLAIM":
        raise ValueError(f"unknown variant: {variant}")

    event_close = float(event["close"])
    touched = False
    extremes: list[float] = []
    for check_index in range(signal_index + 1, min(len(bars) - 1, signal_index + 1 + config.pullback_wait_bars)):
        bar = bars.iloc[check_index]
        if side == "LONG":
            if float(bar["low"]) <= event_close - config.hard_invalidation_atr * atr:
                return {"variant": variant, "symbol": event["symbol"], "side": side, "signal_time": event["close_time"], "status": "HARD_INVALIDATED"}
            touched = touched or float(bar["low"]) <= event_close - config.pullback_atr * atr
            extremes.append(float(bar["low"]))
            reclaimed = (
                touched
                and float(bar["close"]) > float(bar["open"])
                and float(bar["close_location"]) >= config.directional_close_boundary
                and float(bar["close"]) >= event_close - config.reclaim_distance_atr * atr
            )
        else:
            if float(bar["high"]) >= event_close + config.hard_invalidation_atr * atr:
                return {"variant": variant, "symbol": event["symbol"], "side": side, "signal_time": event["close_time"], "status": "HARD_INVALIDATED"}
            touched = touched or float(bar["high"]) >= event_close + config.pullback_atr * atr
            extremes.append(float(bar["high"]))
            reclaimed = (
                touched
                and float(bar["close"]) < float(bar["open"])
                and float(bar["close_location"]) <= 1.0 - config.directional_close_boundary
                and float(bar["close"]) <= event_close + config.reclaim_distance_atr * atr
            )
        if not reclaimed:
            continue
        entry_index = check_index + 1
        if entry_index + config.holding_bars > len(bars):
            break
        entry = float(bars.iloc[entry_index]["open"])
        stop = (min(extremes) - config.stop_buffer_atr * atr) if side == "LONG" else (max(extremes) + config.stop_buffer_atr * atr)
        risk = entry - stop if side == "LONG" else stop - entry
        if not config.min_risk_atr * atr <= risk <= config.max_risk_atr * atr:
            return {"variant": variant, "symbol": event["symbol"], "side": side, "signal_time": event["close_time"], "status": "INVALID_RISK"}
        target = entry + config.target_r * risk if side == "LONG" else entry - config.target_r * risk
        return _finish_trade(event=event, bars=bars, entry_index=entry_index, entry=entry, stop=stop, target=target, variant=variant, config=config)
    return {"variant": variant, "symbol": event["symbol"], "side": side, "signal_time": event["close_time"], "status": "UNCONFIRMED"}


def enforce_capacity(records: Iterable[dict], max_open_positions: int = 5) -> tuple[list[dict], int]:
    """Admit chronologically filled records under the frozen portfolio cap."""

    terminal = [row for row in records if row.get("filled_at") is not None]
    passive = [row for row in records if row.get("filled_at") is None]
    active: list[tuple[pd.Timestamp, str]] = []
    accepted: list[dict] = []
    rejected = 0
    for row in sorted(terminal, key=lambda item: (item["filled_at"], item["symbol"])):
        filled_at = pd.Timestamp(row["filled_at"])
        active = [item for item in active if item[0] > filled_at]
        if len(active) >= max_open_positions or any(symbol == row["symbol"] for _, symbol in active):
            rejected += 1
            continue
        accepted.append(row)
        active.append((pd.Timestamp(row["exited_at"]), str(row["symbol"])))
    return passive + accepted, rejected


def summarize_records(records: Iterable[dict]) -> dict:
    rows = list(records)
    realized = [row for row in rows if row.get("net_r") is not None]
    if not realized:
        return {"positions": 0, "expectancy_r": None, "profit_factor": None, "max_drawdown_r": None}
    values = np.asarray([float(row["net_r"]) for row in realized], dtype=float)
    gains = float(values[values > 0].sum())
    losses = abs(float(values[values <= 0].sum()))
    equity = np.cumsum(values)
    drawdown = np.maximum.accumulate(np.insert(equity, 0, 0.0))[1:] - equity
    sides = {}
    for side in ("LONG", "SHORT"):
        side_values = [float(row["net_r"]) for row in realized if row["side"] == side]
        sides[side] = {"positions": len(side_values), "expectancy_r": float(np.mean(side_values)) if side_values else None}
    statuses = pd.Series([row["status"] for row in rows]).value_counts().to_dict()
    return {
        "positions": len(realized),
        "expectancy_r": float(values.mean()),
        "median_r": float(np.median(values)),
        "win_rate": float((values > 0).mean()),
        "profit_factor": gains / losses if losses else (float("inf") if gains else None),
        "total_r": float(values.sum()),
        "max_drawdown_r": float(drawdown.max()) if len(drawdown) else 0.0,
        "sides": sides,
        "statuses": {str(key): int(value) for key, value in statuses.items()},
    }
