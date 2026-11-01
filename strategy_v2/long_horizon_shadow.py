"""Pure state transitions for the standalone BTC/ETH trend shadow.

The caller supplies completed 4h trade-price bars and a collector observation
time.  No network, storage, exchange, or runtime module is imported here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import math

import pandas as pd

from strategy_v2.long_horizon_trend import (
    LongHorizonTrendPolicy,
    _adverse_price,
    _make_trade,
    add_causal_indicators,
    breakout_side,
)


def new_symbol_state(symbol: str, policy: LongHorizonTrendPolicy) -> dict:
    return {
        "schema_version": 1,
        "symbol": str(symbol).upper(),
        "policy_hash": policy.policy_hash,
        "last_processed_open_time": None,
        "pending_entry": None,
        "pending_exit_reason": None,
        "position": None,
        "completed_trades": 0,
    }


def _event(kind: str, symbol: str, bar, observed_at, policy_hash: str, **details) -> dict:
    identity = {
        "kind": kind,
        "symbol": symbol,
        "bar_open_time": pd.Timestamp(bar.open_time).isoformat(),
        "policy_hash": policy_hash,
        "details_identity": {
            key: details.get(key)
            for key in ("side", "signal_time", "entry_time", "exit_time", "reason")
            if key in details
        },
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return {
        "event_id": "lhts_" + sha256(encoded.encode("utf-8")).hexdigest()[:24],
        "event_type": kind,
        "symbol": symbol,
        "bar_open_time": pd.Timestamp(bar.open_time).isoformat(),
        "bar_close_time": pd.Timestamp(bar.close_time).isoformat(),
        "collector_observed_at": pd.Timestamp(observed_at).isoformat(),
        "policy_hash": policy_hash,
        "execution_authority": False,
        **details,
    }


def advance_symbol_state(
    state: dict,
    completed_four_hour_bars: pd.DataFrame,
    *,
    observed_at,
    policy: LongHorizonTrendPolicy = LongHorizonTrendPolicy(),
    maximum_signal_delay_seconds: float = 120.0,
) -> tuple[dict, tuple[dict, ...]]:
    """Advance one persisted shadow state without retrospective signals."""
    if not math.isfinite(float(maximum_signal_delay_seconds)) or maximum_signal_delay_seconds < 0:
        raise ValueError("maximum signal delay must be finite and non-negative")
    current = deepcopy(state)
    if current.get("policy_hash") != policy.policy_hash:
        raise ValueError("state policy hash does not match the active policy")
    rows = add_causal_indicators(completed_four_hour_bars, policy)
    if rows.empty:
        return current, ()
    opens = pd.to_datetime(rows["open_time"], utc=True)
    if opens.duplicated().any() or not opens.is_monotonic_increasing:
        raise ValueError("completed 4h bars must be unique and ordered")
    if len(opens) > 1 and not opens.diff().dropna().eq(pd.Timedelta(hours=4)).all():
        raise ValueError("completed 4h bars must be contiguous")
    closes = pd.to_datetime(rows["close_time"], utc=True)
    if not closes.eq(opens + pd.Timedelta(hours=4)).all():
        raise ValueError("completed 4h bar close times are invalid")
    observed = pd.Timestamp(observed_at)
    if observed.tzinfo is None:
        observed = observed.tz_localize("UTC")
    else:
        observed = observed.tz_convert("UTC")

    last_value = current.get("last_processed_open_time")
    if last_value is None:
        latest = rows.iloc[-1]
        current["last_processed_open_time"] = pd.Timestamp(latest.open_time).isoformat()
        return current, (
            _event(
                "WARMUP_ESTABLISHED",
                current["symbol"],
                latest,
                observed,
                policy.policy_hash,
                reason="HISTORICAL_BARS_ARE_CONTEXT_ONLY",
            ),
        )

    last = pd.Timestamp(last_value)
    if last < opens.iloc[0]:
        raise ValueError("persisted state predates the supplied causal history window")
    new_indices = [
        index for index, row in rows.iterrows() if pd.Timestamp(row.open_time) > last
    ]
    events: list[dict] = []
    for serial, index in enumerate(new_indices):
        bar = rows.iloc[index]
        is_latest_new_bar = serial == len(new_indices) - 1
        stopped_this_bar = False

        if current.get("pending_exit_reason") and current.get("position"):
            position = current["position"]
            exit_price = _adverse_price(
                float(bar.open),
                position["side"],
                policy.market_exit_slippage_bps,
                entry=False,
            )
            trade = _make_trade(
                symbol=current["symbol"],
                side=position["side"],
                signal_time=position["signal_time"],
                entry_time=position["entry_time"],
                exit_time=bar.open_time,
                entry_price=float(position["entry_price"]),
                initial_stop=float(position["initial_stop"]),
                exit_price=exit_price,
                exit_reason=str(current["pending_exit_reason"]),
                policy=policy,
            )
            events.append(
                _event(
                    "SHADOW_EXIT",
                    current["symbol"],
                    bar,
                    observed,
                    policy.policy_hash,
                    exit_time=trade.exit_time,
                    reason=trade.exit_reason,
                    trade=asdict(trade),
                )
            )
            current["position"] = None
            current["pending_exit_reason"] = None
            current["completed_trades"] = int(current.get("completed_trades", 0)) + 1

        if current.get("pending_entry") and current.get("position") is None:
            pending = current["pending_entry"]
            side = pending["side"]
            entry_price = _adverse_price(
                float(bar.open), side, policy.entry_slippage_bps, entry=True
            )
            atr = float(pending["atr"])
            stop = (
                entry_price - policy.initial_stop_atr * atr
                if side == "LONG"
                else entry_price + policy.initial_stop_atr * atr
            )
            current["position"] = {
                "side": side,
                "signal_time": pending["signal_time"],
                "entry_time": pd.Timestamp(bar.open_time).isoformat(),
                "entry_price": entry_price,
                "initial_stop": stop,
                "stop": stop,
                "favorable_close": entry_price,
            }
            current["pending_entry"] = None
            events.append(
                _event(
                    "SHADOW_ENTRY",
                    current["symbol"],
                    bar,
                    observed,
                    policy.policy_hash,
                    side=side,
                    signal_time=pending["signal_time"],
                    entry_time=pd.Timestamp(bar.open_time).isoformat(),
                    entry_price=entry_price,
                    initial_stop=stop,
                )
            )

        position = current.get("position")
        if position is not None:
            side = position["side"]
            stop = float(position["stop"])
            stop_hit = float(bar.low) <= stop if side == "LONG" else float(bar.high) >= stop
            if stop_hit:
                exit_price = _adverse_price(
                    stop, side, policy.stop_slippage_bps, entry=False
                )
                trade = _make_trade(
                    symbol=current["symbol"],
                    side=side,
                    signal_time=position["signal_time"],
                    entry_time=position["entry_time"],
                    exit_time=bar.close_time,
                    entry_price=float(position["entry_price"]),
                    initial_stop=float(position["initial_stop"]),
                    exit_price=exit_price,
                    exit_reason="ATR_STOP",
                    policy=policy,
                )
                events.append(
                    _event(
                        "SHADOW_EXIT",
                        current["symbol"],
                        bar,
                        observed,
                        policy.policy_hash,
                        exit_time=trade.exit_time,
                        reason=trade.exit_reason,
                        trade=asdict(trade),
                    )
                )
                current["position"] = None
                current["completed_trades"] = int(current.get("completed_trades", 0)) + 1
                stopped_this_bar = True
            else:
                atr = float(bar.atr)
                if side == "LONG":
                    position["favorable_close"] = max(
                        float(position["favorable_close"]), float(bar.close)
                    )
                    candidate = float(position["favorable_close"]) - policy.trailing_stop_atr * atr
                    position["stop"] = max(stop, candidate)
                    channel_exit = float(bar.close) < float(bar.exit_low)
                else:
                    position["favorable_close"] = min(
                        float(position["favorable_close"]), float(bar.close)
                    )
                    candidate = float(position["favorable_close"]) + policy.trailing_stop_atr * atr
                    position["stop"] = min(stop, candidate)
                    channel_exit = float(bar.close) > float(bar.exit_high)
                if channel_exit:
                    current["pending_exit_reason"] = "DONCHIAN_EXIT"
                    events.append(
                        _event(
                            "EXIT_SCHEDULED",
                            current["symbol"],
                            bar,
                            observed,
                            policy.policy_hash,
                            side=side,
                            reason="DONCHIAN_EXIT",
                        )
                    )

        decision_delay = (observed - pd.Timestamp(bar.close_time)).total_seconds()
        signal_allowed = (
            is_latest_new_bar
            and 0 <= decision_delay <= float(maximum_signal_delay_seconds)
        )
        if (
            not stopped_this_bar
            and current.get("position") is None
            and current.get("pending_entry") is None
        ):
            side = breakout_side(rows, index) if signal_allowed else None
            if side is not None:
                current["pending_entry"] = {
                    "side": side,
                    "signal_time": pd.Timestamp(bar.close_time).isoformat(),
                    "atr": float(bar.atr),
                }
                events.append(
                    _event(
                        "SHADOW_SIGNAL",
                        current["symbol"],
                        bar,
                        observed,
                        policy.policy_hash,
                        side=side,
                        signal_time=pd.Timestamp(bar.close_time).isoformat(),
                        scheduled_entry_open_time=pd.Timestamp(bar.close_time).isoformat(),
                        decision_delay_seconds=decision_delay,
                        close=float(bar.close),
                        entry_high=float(bar.entry_high),
                        entry_low=float(bar.entry_low),
                        atr=float(bar.atr),
                    )
                )
            elif is_latest_new_bar and decision_delay < 0:
                events.append(
                    _event(
                        "CLOCK_ANOMALY",
                        current["symbol"],
                        bar,
                        observed,
                        policy.policy_hash,
                        reason="COLLECTOR_TIME_PRECEDES_BAR_CLOSE",
                        decision_delay_seconds=decision_delay,
                    )
                )
            elif is_latest_new_bar and decision_delay > maximum_signal_delay_seconds:
                events.append(
                    _event(
                        "LATE_OBSERVATION",
                        current["symbol"],
                        bar,
                        observed,
                        policy.policy_hash,
                        reason="SIGNAL_CREATION_SUPPRESSED",
                        decision_delay_seconds=decision_delay,
                    )
                )
        current["last_processed_open_time"] = pd.Timestamp(bar.open_time).isoformat()
    return current, tuple(events)
