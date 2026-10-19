from datetime import datetime, timedelta, timezone

from trading_v2.models import Bar, MarketSnapshot, Side
from trading_v2.regime import classify_global_regime
from trading_v2.strategies import (
    PullbackReclaimStrategy,
    _nearest_structural_target,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _series(timeframe, count, *, step, start=100.0, special_high=None):
    minutes = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
    bars = []
    for index in range(count):
        price = start + index * step
        high = price + 0.6
        if special_high is not None and index == count - 10:
            high = special_high
        bars.append(Bar(
            symbol="TEST/USDT",
            timeframe=timeframe,
            open_time=T0 + timedelta(minutes=index * minutes),
            close_time=T0 + timedelta(minutes=(index + 1) * minutes),
            open=price - 0.1,
            high=high,
            low=price - 0.6,
            close=price,
            volume=100 + index,
        ))
    return bars


def test_global_regime_requires_absolute_alignment_of_btc_and_eth():
    bullish_btc = tuple(_series("1h", 60, step=0.5))
    bullish_eth = tuple(_series("1h", 60, step=0.4))
    bearish_eth = tuple(_series("1h", 60, step=-0.4, start=150))
    assert classify_global_regime(bullish_btc, bullish_eth) in {
        "MILD_RISK_ON",
        "STRONG_RISK_ON",
    }
    assert classify_global_regime(bullish_btc, bearish_eth) == "RANGE"


def test_pullback_reclaim_builds_complete_plan_with_real_structural_target():
    bars_1h = _series("1h", 60, step=0.5)
    bars_15m = _series("15m", 60, step=0.2, special_high=125.0)
    bars_5m = _series("5m", 30, step=0.1, start=105.0)
    latest_open = T0 + timedelta(minutes=30 * 5)
    bars_5m.append(Bar(
        symbol="TEST/USDT",
        timeframe="5m",
        open_time=latest_open,
        close_time=latest_open + timedelta(minutes=5),
        open=107.6,
        high=108.4,
        low=106.8,
        close=108.2,
        volume=250,
    ))
    as_of = max(bar.close_time for bar in bars_1h)
    snapshot = MarketSnapshot(
        symbol="TEST/USDT",
        as_of=as_of,
        bars={"1h": bars_1h, "15m": bars_15m, "5m": bars_5m},
        regime="STRONG_RISK_ON",
    )

    plans = PullbackReclaimStrategy().evaluate(snapshot)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.side is Side.BUY
    assert plan.stop < plan.entry < plan.target
    assert plan.target == 125.0
    assert plan.planned_rr >= 1.5
    assert plan.evidence["structural_target"] == 125.0


def test_pullback_reclaim_does_not_short_inside_risk_on_regime():
    bars_1h = _series("1h", 60, step=-0.5, start=150)
    bars_15m = _series("15m", 60, step=-0.2, start=130)
    bars_5m = _series("5m", 31, step=-0.1, start=120)
    snapshot = MarketSnapshot(
        symbol="TEST/USDT",
        as_of=max(bar.close_time for bar in bars_1h),
        bars={"1h": bars_1h, "15m": bars_15m, "5m": bars_5m},
        regime="STRONG_RISK_ON",
    )
    assert PullbackReclaimStrategy().evaluate(snapshot) == ()


def test_structural_target_does_not_reuse_consumed_liquidity():
    bars_15m = _series("15m", 60, step=0.02, special_high=108.0)
    # Consume the confirmed pivot after its two right-side confirmation bars.
    consumed_index = 55
    consumed = bars_15m[consumed_index]
    bars_15m[consumed_index] = Bar(
        symbol=consumed.symbol,
        timeframe=consumed.timeframe,
        open_time=consumed.open_time,
        close_time=consumed.close_time,
        open=consumed.open,
        high=108.5,
        low=consumed.low,
        close=consumed.close,
        volume=consumed.volume,
    )
    final_touch = bars_15m[58]
    bars_15m[58] = Bar(
        symbol=final_touch.symbol,
        timeframe=final_touch.timeframe,
        open_time=final_touch.open_time,
        close_time=final_touch.close_time,
        open=final_touch.open,
        high=109.0,
        low=final_touch.low,
        close=final_touch.close,
        volume=final_touch.volume,
    )
    snapshot = MarketSnapshot(
        symbol="TEST/USDT",
        as_of=bars_15m[-1].close_time,
        bars={"15m": bars_15m},
        regime="RANGE",
    )
    assert _nearest_structural_target(snapshot, Side.BUY, 100.0) is None
