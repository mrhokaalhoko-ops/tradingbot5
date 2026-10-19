from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.replay_current_smc_historical import (
    _evaluated_from_record,
    _execution_bars,
    _geometry_key,
    _live_frame,
    _preselected_side,
    _preselect_signal_indices,
    _resample_ohlcv,
)
from trading_v2.evaluation import EvaluatedPlan
from trading_v2.models import EntryKind, ReplayResult, ReplayStatus, Side, TradePlan


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _raw(count=6):
    rows = []
    for index in range(count):
        opened = T0 + timedelta(minutes=5 * index)
        rows.append({
            "open_time": opened, "close_time": opened + timedelta(minutes=5) - timedelta(milliseconds=1),
            "open": 100 + index, "high": 102 + index, "low": 99 + index,
            "close": 101 + index, "volume": 10,
            "global_regime": "RANGE", "target_month": "2026-01", "selection_rank": 1,
        })
    return pd.DataFrame(rows)


def test_resample_ohlcv_requires_every_source_bar():
    frame = _raw().drop(index=1)
    result = _resample_ohlcv(frame, 15)
    assert len(result) == 1
    assert result.iloc[0]["open_time"] == pd.Timestamp(T0 + timedelta(minutes=15))


def test_live_frame_preserves_last_closed_at_minus_two():
    history = _raw(3)
    result = _live_frame(history, 123.0, T0 + timedelta(minutes=15))
    assert float(result.iloc[-2]["close"]) == 103.0
    assert float(result.iloc[-1]["close"]) == 123.0


def test_execution_bars_uses_the_same_inclusive_window():
    frame = _raw(6)
    result = _execution_bars(
        frame,
        "TESTUSDT",
        start=T0 + timedelta(minutes=5),
        end=T0 + timedelta(minutes=20),
    )
    assert [bar.open_time for bar in result] == [
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=15),
        T0 + timedelta(minutes=20),
    ]


def test_checkpoint_record_round_trip_preserves_replay_values():
    from scripts.replay_current_smc_historical import _record

    plan = TradePlan(
        plan_id="MODEL:thesis", strategy_id="MODEL", setup_id="thesis",
        symbol="TEST/USDT", side=Side.BUY, created_at=T0,
        valid_until=T0 + timedelta(minutes=20),
        exit_by=T0 + timedelta(hours=4), entry_kind=EntryKind.LIMIT,
        entry=100, stop=99, target=102, thesis="test", score=7,
        evidence={"htf_trend": "bullish", "market_regime": "RISK_ON"},
    )
    result = ReplayResult(
        plan_id=plan.plan_id, status=ReplayStatus.TP_FIRST,
        filled_at=T0 + timedelta(minutes=5), exited_at=T0 + timedelta(minutes=30),
        entry_price=100, exit_price=102, gross_r=2, net_r=1.9,
        mfe_r=2.1, mae_r=-0.2, bars_observed=6, reason="target",
    )
    restored = _evaluated_from_record(_record(EvaluatedPlan(plan, result)))
    assert restored.plan == plan
    assert restored.result == result


def test_preselector_matches_buy_precedence_and_sell_candidate():
    base = pd.DataFrame({
        "close": [100.0, 100.0], "atr": [2.0, 2.0],
        "ema_50": [110.0, 90.0], "ema_200": [100.0, 100.0],
        "ob_bullish_low": [98.0, 98.0], "ob_bullish_high": [100.0, 100.0],
        "ob_bullish_age": [1, 1], "ob_bullish_mitigated": [False, False],
        "ob_bearish_low": [100.0, 100.0], "ob_bearish_high": [102.0, 102.0],
        "ob_bearish_age": [1, 1], "ob_bearish_mitigated": [False, False],
    })
    assert _preselect_signal_indices(base, 100) == [0, 1]
    assert _preselected_side(base.iloc[0], 100) == "buy"
    assert _preselected_side(base.iloc[1], 100) == "sell"
    assert _geometry_key("TESTUSDT", "buy", base.iloc[0]) == (
        "TESTUSDT", "buy", "98", "100"
    )


def test_legacy_checkpoint_cannot_be_reused_after_replay_changes():
    import pytest
    from scripts.replay_current_smc_historical import _validate_checkpoint, STRATEGY_VERSION
    with pytest.raises(ValueError, match="code identity"):
        _validate_checkpoint({"strategy_version":STRATEGY_VERSION, "profile":"smc_balanced"}, "smc_balanced")


def test_checkpoint_accepts_same_code_and_rejects_changed_simulator(tmp_path):
    import json
    import pytest
    from collections import Counter
    from scripts.replay_current_smc_historical import _validate_checkpoint, _write_checkpoint
    path = tmp_path / "checkpoint.json"
    _write_checkpoint(path, profile_key="smc_balanced", completed_symbols=set(), counters=Counter(), records={})
    saved = json.loads(path.read_text())
    _validate_checkpoint(saved, "smc_balanced")
    saved["code_identity"]["trading_v2/replay.py"] = "old-simulator"
    with pytest.raises(ValueError, match="code identity"):
        _validate_checkpoint(saved, "smc_balanced")
