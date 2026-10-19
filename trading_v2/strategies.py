"""Pure candidate strategies for causal research; never submit orders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from .indicators import atr, average_volume, close_location, ema
from .models import EntryKind, MarketSnapshot, Side, TradePlan


def _identifier(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _trend_side(snapshot: MarketSnapshot) -> Side | None:
    bars_1h = snapshot.closed_bars("1h")
    bars_15m = snapshot.closed_bars("15m")
    if len(bars_1h) < 51 or len(bars_15m) < 51:
        return None
    one_hour = [bar.close for bar in bars_1h]
    fifteen = [bar.close for bar in bars_15m]
    bullish = (
        one_hour[-1] > ema(one_hour, 20) > ema(one_hour, 50)
        and fifteen[-1] > ema(fifteen, 20) > ema(fifteen, 50)
    )
    bearish = (
        one_hour[-1] < ema(one_hour, 20) < ema(one_hour, 50)
        and fifteen[-1] < ema(fifteen, 20) < ema(fifteen, 50)
    )
    if bullish and snapshot.regime in {"MILD_RISK_ON", "STRONG_RISK_ON"}:
        return Side.BUY
    if bearish and snapshot.regime in {"MILD_RISK_OFF", "STRONG_RISK_OFF"}:
        return Side.SELL
    return None


def _nearest_structural_target(snapshot: MarketSnapshot, side: Side, entry: float) -> float | None:
    bars = snapshot.closed_bars("15m")[-80:-1]
    if len(bars) < 10:
        return None
    candidates = []
    for index in range(2, len(bars) - 2):
        bar = bars[index]
        # A pivot becomes known only after two candles close on its right.  A
        # later touch consumes that liquidity; reusing it as a future target
        # would manufacture market room that no longer exists.
        later_after_confirmation = bars[index + 3:]
        if side is Side.BUY and bar.high == max(item.high for item in bars[index - 2:index + 3]):
            consumed = any(item.high >= bar.high for item in later_after_confirmation)
            if bar.high > entry and not consumed:
                candidates.append(float(bar.high))
        if side is Side.SELL and bar.low == min(item.low for item in bars[index - 2:index + 3]):
            consumed = any(item.low <= bar.low for item in later_after_confirmation)
            if bar.low < entry and not consumed:
                candidates.append(float(bar.low))
    return min(candidates) if side is Side.BUY and candidates else (
        max(candidates) if side is Side.SELL and candidates else None
    )


def _build_plan(
    snapshot: MarketSnapshot,
    *,
    strategy_id: str,
    side: Side,
    stop: float,
    target: float,
    thesis: str,
    score: float,
    anchor: object,
    min_rr: float,
) -> TradePlan | None:
    entry = float(snapshot.closed_bars("5m")[-1].close)
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward / risk < min_rr:
        return None
    setup_id = _identifier("setup", strategy_id, snapshot.symbol, side.value, anchor)
    plan_id = _identifier("plan", setup_id, snapshot.as_of.isoformat())
    return TradePlan(
        plan_id=plan_id,
        strategy_id=strategy_id,
        setup_id=setup_id,
        symbol=snapshot.symbol,
        side=side,
        created_at=snapshot.as_of,
        valid_until=snapshot.as_of + timedelta(minutes=10),
        exit_by=snapshot.as_of + timedelta(hours=6),
        entry_kind=EntryKind.MARKET_NEXT_OPEN,
        entry=entry,
        stop=float(stop),
        target=float(target),
        thesis=thesis,
        score=float(score),
        evidence={
            "regime": snapshot.regime,
            "reference_entry": entry,
            "structural_target": float(target),
            "planned_rr": reward / risk,
        },
    )


def _volatility_stop(side: Side, entry: float, structural_stop: float, volatility: float) -> float:
    """Keep market-entry costs small relative to the planned risk distance."""
    minimum_risk = max(1.20 * float(volatility), 0.0025 * float(entry))
    if side is Side.BUY:
        return min(float(structural_stop), float(entry) - minimum_risk)
    return max(float(structural_stop), float(entry) + minimum_risk)


@dataclass(frozen=True)
class PullbackReclaimStrategy:
    """Trend-aligned 5m reclaim with a pre-existing 15m liquidity target."""

    strategy_id: str = "v2_pullback_reclaim_1"
    min_rr: float = 1.5

    def evaluate(self, snapshot: MarketSnapshot) -> tuple[TradePlan, ...]:
        side = _trend_side(snapshot)
        bars = snapshot.closed_bars("5m")
        if side is None or len(bars) < 31:
            return ()
        trigger = bars[-1]
        closes = [bar.close for bar in bars]
        fast = ema(closes, 20)
        volatility = atr(bars, 14)
        location = close_location(trigger)
        recent = bars[-9:]
        if side is Side.BUY:
            confirmed = trigger.low <= fast and trigger.close > fast and trigger.close > trigger.open and location >= 0.65
            stop = min(bar.low for bar in recent) - 0.15 * volatility
        else:
            confirmed = trigger.high >= fast and trigger.close < fast and trigger.close < trigger.open and location <= 0.35
            stop = max(bar.high for bar in recent) + 0.15 * volatility
        if not confirmed:
            return ()
        target = _nearest_structural_target(snapshot, side, trigger.close)
        if target is None:
            return ()
        stop = _volatility_stop(side, trigger.close, stop, volatility)
        plan = _build_plan(
            snapshot,
            strategy_id=self.strategy_id,
            side=side,
            stop=stop,
            target=target,
            thesis="trend-aligned pullback, closed 5m reclaim, real 15m liquidity target",
            score=location if side is Side.BUY else 1.0 - location,
            anchor=bars[-2].open_time.isoformat(),
            min_rr=self.min_rr,
        )
        return (plan,) if plan else ()


@dataclass(frozen=True)
class BreakoutContinuationStrategy:
    """Fresh 5m expansion toward an untouched 15m structural target."""

    strategy_id: str = "v2_breakout_continuation_1"
    min_rr: float = 1.5
    min_volume_ratio: float = 1.5

    def evaluate(self, snapshot: MarketSnapshot) -> tuple[TradePlan, ...]:
        side = _trend_side(snapshot)
        bars = snapshot.closed_bars("5m")
        if side is None or len(bars) < 31:
            return ()
        trigger = bars[-1]
        prior = bars[-21:-1]
        volatility = atr(bars, 14)
        volume_base = average_volume(bars[-21:-1], 20)
        volume_ratio = trigger.volume / volume_base if volume_base > 0 else 0.0
        body = abs(trigger.close - trigger.open)
        if not 0.5 * volatility <= body <= 1.8 * volatility or volume_ratio < self.min_volume_ratio:
            return ()
        if side is Side.BUY:
            confirmed = trigger.close > max(bar.high for bar in prior) and close_location(trigger) >= 0.70
            stop = min(bar.low for bar in bars[-6:]) - 0.10 * volatility
        else:
            confirmed = trigger.close < min(bar.low for bar in prior) and close_location(trigger) <= 0.30
            stop = max(bar.high for bar in bars[-6:]) + 0.10 * volatility
        if not confirmed:
            return ()
        target = _nearest_structural_target(snapshot, side, trigger.close)
        if target is None:
            return ()
        stop = _volatility_stop(side, trigger.close, stop, volatility)
        plan = _build_plan(
            snapshot,
            strategy_id=self.strategy_id,
            side=side,
            stop=stop,
            target=target,
            thesis="fresh 5m displacement with volume toward real 15m liquidity",
            score=min(1.0, volume_ratio / 3.0),
            anchor=trigger.open_time.isoformat(),
            min_rr=self.min_rr,
        )
        return (plan,) if plan else ()
