"""Causal, execution-free long-horizon trend research primitives.

This module deliberately has no exchange, runtime, order, or position-manager
imports.  It evaluates one frozen Donchian/ATR hypothesis on completed 4h bars.
The output is preliminary offline evidence, never trading authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class LongHorizonTrendPolicy:
    policy_id: str = "DONCHIAN_20D_10D_ATR_TRAIL_V1"
    entry_lookback_bars: int = 120
    exit_lookback_bars: int = 60
    atr_bars: int = 30
    initial_stop_atr: float = 2.0
    trailing_stop_atr: float = 3.0
    entry_slippage_bps: float = 3.0
    market_exit_slippage_bps: float = 3.0
    stop_slippage_bps: float = 8.0
    fee_bps_per_side: float = 5.0
    funding_drag_bps_per_8h: float = 1.0

    def __post_init__(self):
        if self.entry_lookback_bars <= self.exit_lookback_bars:
            raise ValueError("entry lookback must exceed exit lookback")
        if min(self.exit_lookback_bars, self.atr_bars) < 2:
            raise ValueError("lookbacks must be at least two bars")
        numeric = (
            self.initial_stop_atr,
            self.trailing_stop_atr,
            self.entry_slippage_bps,
            self.market_exit_slippage_bps,
            self.stop_slippage_bps,
            self.fee_bps_per_side,
            self.funding_drag_bps_per_8h,
        )
        if not all(math.isfinite(value) and value >= 0 for value in numeric):
            raise ValueError("policy numeric values must be finite and non-negative")
        if self.initial_stop_atr <= 0 or self.trailing_stop_atr <= 0:
            raise ValueError("stop multiples must be positive")

    @property
    def policy_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TrendTrade:
    symbol: str
    side: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_price: float
    initial_stop: float
    exit_price: float
    exit_reason: str
    risk: float
    gross_r: float
    fee_drag_r: float
    funding_drag_r: float
    net_r: float
    holding_hours: float
    policy_hash: str
    execution_authority: bool = False


def aggregate_hourly_to_4h(hourly: pd.DataFrame) -> pd.DataFrame:
    required = {"open_time", "open", "high", "low", "close", "volume"}
    missing = required - set(hourly.columns)
    if missing:
        raise ValueError(f"missing hourly columns: {sorted(missing)}")
    rows = hourly.copy()
    rows["open_time"] = pd.to_datetime(rows["open_time"], utc=True)
    rows = rows.sort_values("open_time").drop_duplicates("open_time", keep=False)
    rows["bucket"] = rows["open_time"].dt.floor("4h")
    grouped = rows.groupby("bucket", sort=True)
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        source_bars=("open_time", "size"),
        first_source_open=("open_time", "min"),
        last_source_open=("open_time", "max"),
    ).reset_index(names="open_time")
    complete = (
        result["source_bars"].eq(4)
        & result["first_source_open"].eq(result["open_time"])
        & result["last_source_open"].eq(result["open_time"] + pd.Timedelta(hours=3))
    )
    result = result.loc[complete].copy()
    result["close_time"] = result["open_time"] + pd.Timedelta(hours=4)
    return result[["open_time", "close_time", "open", "high", "low", "close", "volume"]]


def add_causal_indicators(frame: pd.DataFrame, policy: LongHorizonTrendPolicy) -> pd.DataFrame:
    rows = frame.sort_values("open_time").reset_index(drop=True).copy()
    previous_close = rows["close"].shift(1)
    true_range = pd.concat(
        [
            rows["high"] - rows["low"],
            (rows["high"] - previous_close).abs(),
            (rows["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    rows["atr"] = true_range.rolling(policy.atr_bars, min_periods=policy.atr_bars).mean()
    rows["entry_high"] = rows["high"].shift(1).rolling(
        policy.entry_lookback_bars, min_periods=policy.entry_lookback_bars
    ).max()
    rows["entry_low"] = rows["low"].shift(1).rolling(
        policy.entry_lookback_bars, min_periods=policy.entry_lookback_bars
    ).min()
    rows["exit_high"] = rows["high"].shift(1).rolling(
        policy.exit_lookback_bars, min_periods=policy.exit_lookback_bars
    ).max()
    rows["exit_low"] = rows["low"].shift(1).rolling(
        policy.exit_lookback_bars, min_periods=policy.exit_lookback_bars
    ).min()
    return rows


def _adverse_price(price: float, side: str, bps: float, *, entry: bool) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    sign = direction if entry else -direction
    return float(price) * (1.0 + sign * float(bps) / 10_000.0)


def _make_trade(
    *,
    symbol: str,
    side: str,
    signal_time,
    entry_time,
    exit_time,
    entry_price: float,
    initial_stop: float,
    exit_price: float,
    exit_reason: str,
    policy: LongHorizonTrendPolicy,
) -> TrendTrade:
    risk = abs(entry_price - initial_stop)
    direction = 1.0 if side == "LONG" else -1.0
    gross_r = direction * (exit_price - entry_price) / risk
    notional_r = entry_price / risk
    fee_drag_r = 2.0 * policy.fee_bps_per_side / 10_000.0 * notional_r
    holding_hours = max(
        0.0,
        (pd.Timestamp(exit_time) - pd.Timestamp(entry_time)).total_seconds() / 3600.0,
    )
    funding_drag_r = (
        holding_hours / 8.0
        * policy.funding_drag_bps_per_8h
        / 10_000.0
        * notional_r
    )
    return TrendTrade(
        symbol=symbol,
        side=side,
        signal_time=pd.Timestamp(signal_time).isoformat(),
        entry_time=pd.Timestamp(entry_time).isoformat(),
        exit_time=pd.Timestamp(exit_time).isoformat(),
        entry_price=entry_price,
        initial_stop=initial_stop,
        exit_price=exit_price,
        exit_reason=exit_reason,
        risk=risk,
        gross_r=gross_r,
        fee_drag_r=fee_drag_r,
        funding_drag_r=funding_drag_r,
        net_r=gross_r - fee_drag_r - funding_drag_r,
        holding_hours=holding_hours,
        policy_hash=policy.policy_hash,
    )


def simulate_symbol(
    symbol: str,
    four_hour_bars: pd.DataFrame,
    policy: LongHorizonTrendPolicy = LongHorizonTrendPolicy(),
) -> tuple[TrendTrade, ...]:
    """Simulate one symbol with decisions only after completed bars.

    A breakout observed at bar ``i`` enters at bar ``i+1`` open.  Stop and
    channel updates derived from bar ``i`` are first effective on bar ``i+1``.
    Open trades at the end of the dataset are omitted rather than force-closed.
    """

    rows = add_causal_indicators(four_hour_bars, policy)
    trades: list[TrendTrade] = []
    position: dict | None = None
    pending_entry: dict | None = None
    pending_exit_reason: str | None = None

    for index, bar in rows.iterrows():
        if pending_exit_reason and position is not None:
            exit_price = _adverse_price(
                float(bar.open), position["side"], policy.market_exit_slippage_bps, entry=False
            )
            trades.append(
                _make_trade(
                    symbol=symbol,
                    side=position["side"],
                    signal_time=position["signal_time"],
                    entry_time=position["entry_time"],
                    exit_time=bar.open_time,
                    entry_price=position["entry_price"],
                    initial_stop=position["initial_stop"],
                    exit_price=exit_price,
                    exit_reason=pending_exit_reason,
                    policy=policy,
                )
            )
            position = None
            pending_exit_reason = None

        if pending_entry is not None and position is None:
            side = pending_entry["side"]
            entry_price = _adverse_price(
                float(bar.open), side, policy.entry_slippage_bps, entry=True
            )
            atr = float(pending_entry["atr"])
            stop = (
                entry_price - policy.initial_stop_atr * atr
                if side == "LONG"
                else entry_price + policy.initial_stop_atr * atr
            )
            position = {
                "side": side,
                "signal_time": pending_entry["signal_time"],
                "entry_time": bar.open_time,
                "entry_price": entry_price,
                "initial_stop": stop,
                "stop": stop,
                "favorable_close": entry_price,
            }
            pending_entry = None

        if position is not None:
            side = position["side"]
            stop = float(position["stop"])
            stop_hit = float(bar.low) <= stop if side == "LONG" else float(bar.high) >= stop
            if stop_hit:
                exit_price = _adverse_price(stop, side, policy.stop_slippage_bps, entry=False)
                trades.append(
                    _make_trade(
                        symbol=symbol,
                        side=side,
                        signal_time=position["signal_time"],
                        entry_time=position["entry_time"],
                        exit_time=bar.close_time,
                        entry_price=position["entry_price"],
                        initial_stop=position["initial_stop"],
                        exit_price=exit_price,
                        exit_reason="ATR_STOP",
                        policy=policy,
                    )
                )
                position = None
                continue

            if side == "LONG":
                position["favorable_close"] = max(
                    float(position["favorable_close"]), float(bar.close)
                )
                channel_exit = float(bar.close) < float(bar.exit_low)
                candidate_stop = float(position["favorable_close"]) - policy.trailing_stop_atr * float(bar.atr)
                position["stop"] = max(stop, candidate_stop)
            else:
                position["favorable_close"] = min(
                    float(position["favorable_close"]), float(bar.close)
                )
                channel_exit = float(bar.close) > float(bar.exit_high)
                candidate_stop = float(position["favorable_close"]) + policy.trailing_stop_atr * float(bar.atr)
                position["stop"] = min(stop, candidate_stop)
            if channel_exit:
                pending_exit_reason = "DONCHIAN_EXIT"
            continue

        if pending_entry is not None or index + 1 >= len(rows):
            continue
        required = (bar.atr, bar.entry_high, bar.entry_low)
        if not all(pd.notna(value) and math.isfinite(float(value)) for value in required):
            continue
        previous_close = float(rows.iloc[index - 1].close) if index > 0 else float("nan")
        previous_high = float(rows.iloc[index - 1].entry_high) if index > 0 else float("nan")
        previous_low = float(rows.iloc[index - 1].entry_low) if index > 0 else float("nan")
        long_break = float(bar.close) > float(bar.entry_high) and not (
            math.isfinite(previous_close) and math.isfinite(previous_high) and previous_close > previous_high
        )
        short_break = float(bar.close) < float(bar.entry_low) and not (
            math.isfinite(previous_close) and math.isfinite(previous_low) and previous_close < previous_low
        )
        if long_break ^ short_break:
            pending_entry = {
                "side": "LONG" if long_break else "SHORT",
                "signal_time": bar.close_time,
                "atr": float(bar.atr),
            }
    return tuple(trades)


def apply_portfolio_capacity(
    trades: Iterable[TrendTrade], max_open_positions: int
) -> tuple[TrendTrade, ...]:
    if max_open_positions < 1:
        raise ValueError("max_open_positions must be positive")
    accepted: list[TrendTrade] = []
    for trade in sorted(trades, key=lambda row: (row.entry_time, row.symbol, row.side)):
        entry = pd.Timestamp(trade.entry_time)
        active = [row for row in accepted if pd.Timestamp(row.exit_time) > entry]
        if len(active) < max_open_positions:
            accepted.append(trade)
    return tuple(sorted(accepted, key=lambda row: (row.exit_time, row.symbol)))
