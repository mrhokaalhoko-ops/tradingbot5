"""Reproduce the logged false trend flip, plus retain genuine cancellation guards."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest
import main

FIXTURE = Path(__file__).parent / "fixtures/bulla_trend_guard_20260909.json"


def frame():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = [[r[0], *map(float, r[1:6])] for r in data["closed_bars"]]
    price = rows[-1][4]
    # Synthetic live row; historical future high/low/close are never replayed.
    rows.append([data["live_open_ms"], price, price, price, price, 0.0])
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def guard(bars, side, current):
    client = Mock()
    client.fetch_ohlcv.side_effect = lambda symbol, timeframe, limit: bars.tail(limit).copy()
    order = {"side": side, "entry_price": current}
    reason = main.check_virtual_order_cancellation(
        client, "BULLA/USDT", order, current_price=current, check_structure=True,
    )
    return reason, order


@pytest.mark.parametrize("mirror,side", [(False, "sell"), (True, "buy")])
def test_unchanged_closed_candle_does_not_invent_a_flip(mirror, side):
    bars = frame()
    if mirror:
        bars["close"] = 1.0 - bars["close"]
    reason, _ = guard(bars, side, float(bars.iloc[-2]["close"]))
    assert reason is None


@pytest.mark.parametrize("side,ascending", [("sell", True), ("buy", False)])
def test_real_opposing_trend_still_cancels(side, ascending):
    bars = frame()
    bars["close"] = [100 + i / 10 if ascending else 150 - i / 10 for i in range(260)]
    # The open candle must not decide the trend, even at an extreme price.
    bars.loc[bars.index[-1], "close"] = 0.001 if ascending else 1_000_000
    reason, _ = guard(bars, side, float(bars.iloc[-2]["close"]))
    assert reason and "EMA50" in reason


def test_recorded_basis_matches_the_original_scanner_values():
    _, order = guard(frame(), "sell", 0.07948972)
    e = order["trend_check_evidence"]
    assert e["analysis_candle_count"] == 260
    assert e["closed_candle_count"] == 259
    assert e["ema_50"] == pytest.approx(0.07791876018470245, abs=1e-14)
    assert e["ema_200"] == pytest.approx(0.0786753415031732, abs=1e-14)
    assert e["closed_bar_end"] == "2026-09-09T07:15:00+00:00"
    json.dumps(order)


def test_terminal_audit_keeps_the_trend_calculation_evidence():
    _, order = guard(frame(), "sell", 0.07948972)
    with patch.object(main, "audit_decision_event") as audit:
        main.finalize_virtual_order(Mock(), Mock(), "BULLA/USDT", order, "TEST_TERMINAL")
    assert audit.call_args.kwargs["details"]["trend_check_evidence"] == order["trend_check_evidence"]
