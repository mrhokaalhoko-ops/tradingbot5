import inspect
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from strategy.momentum_continuation import (
    build_external_thesis_continuation_setup,
    build_fresh_continuation_plan,
    detect_momentum_impulse,
    evaluate_continuation_confirmation,
    evaluate_shadow_plan_outcome,
)
from utils.momentum_continuation_monitor import (
    MomentumContinuationShadowMonitor,
    _take_external_smc_theses,
    register_external_smc_thesis,
)


def _impulse_frame():
    timestamps = pd.date_range("2026-08-27T00:00:00Z", periods=62, freq="5min")
    closes = np.linspace(100.0, 102.0, 60)
    rows = []
    for timestamp, close in zip(timestamps[:60], closes):
        rows.append({
            "timestamp": timestamp,
            "open": close - 0.03,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": 100.0,
        })
    # Last closed candle is the causal impulse.
    rows.append({
        "timestamp": timestamps[60],
        "open": 102.0,
        "high": 102.60,
        "low": 101.95,
        "close": 102.55,
        "volume": 220.0,
    })
    # REST's final row is live and must never affect the decision.
    rows.append({
        "timestamp": timestamps[61],
        "open": 102.55,
        "high": 110.0,
        "low": 90.0,
        "close": 91.0,
        "volume": 50_000.0,
    })
    return pd.DataFrame(rows)


def _candidate():
    return {
        "symbol": "TESTUSDT",
        "tags": ["MOMENTUM_UP"],
        "change_1m_pct": 0.9,
        "change_5m_pct": 2.2,
        "change_24h_pct": 12.0,
        "quote_volume_24h": 25_000_000.0,
    }


def test_impulse_uses_closed_5m_and_rejects_opposing_regime():
    bars = _impulse_frame()
    accepted = detect_momentum_impulse(
        symbol="TEST/USDT",
        candidate=_candidate(),
        bars_5m=bars,
        market_regime={"label": "STRONG_RISK_ON", "known": True},
        context_15m={"up_structure": True, "down_structure": False},
    )

    assert accepted["qualified"] is True
    assert accepted["impulse_price"] == 102.55
    assert accepted["evidence"]["volume_ratio"] > 2.0

    sell_candidate = {**_candidate(), "tags": ["MOMENTUM_DOWN"]}
    rejected = detect_momentum_impulse(
        symbol="TEST/USDT",
        candidate=sell_candidate,
        bars_5m=bars,
        market_regime={"label": "STRONG_RISK_ON", "known": True},
        context_15m={"up_structure": False, "down_structure": True},
    )
    assert rejected["qualified"] is False
    assert "regime_policy" in rejected["failed_gates"]


def test_confirmation_requires_a_later_closed_bar_not_impulse_tick():
    bars = _impulse_frame()
    setup = detect_momentum_impulse(
        symbol="TEST/USDT",
        candidate=_candidate(),
        bars_5m=bars,
        market_regime={"label": "MILD_RISK_ON", "known": True},
        context_15m={"up_structure": True, "down_structure": False},
    )
    waiting = evaluate_continuation_confirmation(setup=setup, bars_5m=bars)
    assert waiting["passed"] is False
    assert waiting["reason"] == "WAITING_FOR_NEXT_CLOSED_5M_BAR"

    next_timestamp = pd.Timestamp(bars.iloc[-1]["timestamp"]) + pd.Timedelta(minutes=5)
    live_timestamp = next_timestamp + pd.Timedelta(minutes=5)
    continuation = pd.DataFrame([
        {
            "timestamp": next_timestamp,
            "open": 102.30,
            "high": 102.52,
            "low": 102.20,
            "close": 102.47,
            "volume": 100.0,
        },
        {
            "timestamp": live_timestamp,
            "open": 102.47,
            "high": 103.0,
            "low": 100.0,
            "close": 100.5,
            "volume": 50_000.0,
        },
    ])
    confirmed_bars = pd.concat([bars.iloc[:-1], continuation], ignore_index=True)
    confirmed = evaluate_continuation_confirmation(
        setup=setup, bars_5m=confirmed_bars
    )

    assert confirmed["passed"] is True
    assert confirmed["status"] == "PASS"
    assert confirmed["entry_price"] == 102.47


def test_external_smc_thesis_builds_fresh_closed_5m_setup_without_old_entry():
    bars = _impulse_frame()
    setup = build_external_thesis_continuation_setup(
        symbol="TEST/USDT",
        side="buy",
        source_thesis_id="smc-thesis-1",
        bars_5m=bars,
        invalidation_level=99.0,
        market_regime={"label": "MILD_RISK_ON", "known": True},
        context_15m={"up_structure": True},
        source_snapshot={"original_entry": 100.0, "original_tp": 110.0},
    )

    assert setup["qualified"] is True
    assert setup["source"] == "SMC_RR_PASS_PAIRED_SHADOW"
    assert setup["source_thesis_id"] == "smc-thesis-1"
    assert setup["breakout_level"] == 102.55
    assert setup["impulse_price"] == 102.55
    assert setup["breakout_level"] != setup["source_snapshot"]["original_entry"]
    assert setup["evidence"]["reference_is_closed_5m"] is True


def test_external_smc_thesis_rejects_already_invalid_structure():
    setup = build_external_thesis_continuation_setup(
        symbol="TEST/USDT",
        side="buy",
        source_thesis_id="smc-thesis-invalid",
        bars_5m=_impulse_frame(),
        invalidation_level=103.0,
        market_regime={"label": "RANGE", "known": True},
        context_15m={"up_structure": True},
    )

    assert setup["qualified"] is False
    assert setup["reason"] == "THESIS_ALREADY_STRUCTURALLY_INVALID_AT_REGISTRATION"


def test_fresh_plan_uses_real_target_and_shadow_outcome_orders_tp_sl():
    setup = {
        "setup_id": "mc_setup_test",
        "side": "buy",
        "atr_5m": 0.35,
        "invalidation_level": 101.95,
    }
    confirmation = {
        "entry_price": 102.47,
        "confirmation_low": 102.20,
        "confirmation_high": 102.52,
        "confirmation_bar_id": "bar-confirmed",
        "confirmation_open_time": "2026-08-27T05:10:00Z",
    }
    plan = build_fresh_continuation_plan(
        setup=setup,
        confirmation=confirmation,
        market_levels={"swing_highs": [{"price": 104.0}]},
        min_net_rr=1.5,
    )

    assert plan["valid"] is True
    assert plan["execution_enabled"] is False
    assert plan["target_type"] == "swing_high_liquidity"
    assert plan["net_rr"] >= 1.5

    bars = pd.DataFrame([
        {
            "timestamp": pd.Timestamp("2026-08-27T05:10:00Z"),
            "open": 102.4, "high": 102.6, "low": 102.2,
            "close": 102.47, "volume": 100,
        },
        {
            "timestamp": pd.Timestamp("2026-08-27T05:15:00Z"),
            "open": 102.47, "high": plan["tp"] + 0.01, "low": 102.3,
            "close": plan["tp"], "volume": 120,
        },
        {
            "timestamp": pd.Timestamp("2026-08-27T05:20:00Z"),
            "open": plan["tp"], "high": plan["tp"] + 0.2, "low": plan["tp"] - 0.1,
            "close": plan["tp"], "volume": 20,
        },
    ])
    outcome = evaluate_shadow_plan_outcome(plan=plan, bars_5m=bars)
    assert outcome["terminal"] is True
    assert outcome["status"] == "TARGET_FIRST"
    assert outcome["final_r"] == plan["net_rr"]


def test_stale_hot_list_cannot_create_new_setup_but_shadow_has_no_execution_api():
    monitor = MomentumContinuationShadowMonitor(
        Mock(), Mock(), state_manager=None, now_fn=lambda: 10_000.0
    )
    stale = {
        "status": "OK",
        "timestamp": datetime.fromtimestamp(1.0, tz=timezone.utc).isoformat(),
        "candidates": [_candidate()],
    }
    with patch.object(monitor, "_process_candidate") as process:
        snapshot = monitor.collect_once(
            hot_snapshot=stale,
            market_regime={"label": "RANGE", "known": True},
        )

    assert snapshot["status"] == "HOT_LIST_STALE"
    assert snapshot["candidate_count"] == 0
    process.assert_not_called()

    source = inspect.getsource(MomentumContinuationShadowMonitor)
    for forbidden in (
        "create_order",
        "execute_trade",
        "execute_and_protect_trade",
        "save_order_intent",
    ):
        assert forbidden not in source


def test_external_smc_registration_is_deduplicated_and_consumed_by_shadow():
    _take_external_smc_theses()
    payload = {
        "symbol": "TEST/USDT",
        "side": "buy",
        "source_thesis_id": "smc-paired-queue",
        "invalidation_level": 99.0,
    }
    assert register_external_smc_thesis(payload) is True
    assert register_external_smc_thesis(payload) is True

    monitor = MomentumContinuationShadowMonitor(
        Mock(), Mock(), state_manager=None, now_fn=lambda: 10_000.0
    )
    with patch.object(monitor, "_process_external_thesis", return_value=True) as process:
        snapshot = monitor.collect_once(
            hot_snapshot={
                "status": "OK",
                "timestamp": "1970-01-01T02:46:40Z",
                "candidates": [],
            },
            market_regime={"label": "RANGE", "known": True},
        )

    process.assert_called_once()
    assert snapshot["external_theses_received"] == 1
    assert snapshot["execution_enabled"] is False


def test_external_smc_thesis_enters_paired_shadow_state_not_execution():
    _take_external_smc_theses()
    client = Mock()
    client.exchange.markets = {
        "TEST/USDT:USDT": {
            "symbol": "TEST/USDT:USDT",
            "contract": True,
            "swap": True,
            "quote": "USDT",
            "settle": "USDT",
            "linear": True,
            "active": True,
            "info": {"status": "TRADING"},
        }
    }
    client.fetch_ohlcv.return_value = _impulse_frame()
    monitor = MomentumContinuationShadowMonitor(
        client, Mock(), state_manager=None, now_fn=lambda: 10_000.0
    )
    assert register_external_smc_thesis({
        "symbol": "TEST/USDT",
        "side": "buy",
        "source_thesis_id": "smc-paired-state",
        "invalidation_level": 99.0,
        "market_regime": {"label": "MILD_RISK_ON", "known": True},
        "context_15m": {"up_structure": True},
    }) is True

    with patch.object(monitor, "_audit") as audit:
        snapshot = monitor.collect_once(
            hot_snapshot={
                "status": "OK",
                "timestamp": "1970-01-01T02:46:40Z",
                "candidates": [],
            },
            market_regime={"label": "RANGE", "known": True},
        )

    assert snapshot["paired_active_setups"] == 1
    assert snapshot["paired_active_plans"] == 0
    assert snapshot["execution_enabled"] is False
    setup = snapshot["setups"][0]
    assert setup["source"] == "SMC_RR_PASS_PAIRED_SHADOW"
    statuses = [call.kwargs["status"] for call in audit.call_args_list]
    assert "SETUP_CREATED_EXTERNAL_SMC" in statuses


def test_mainnet_only_hot_symbol_is_skipped_before_demo_candle_fetch():
    client = Mock()
    client.exchange.markets = {
        "BTC/USDT:USDT": {
            "symbol": "BTC/USDT:USDT",
            "contract": True,
            "swap": True,
            "quote": "USDT",
            "settle": "USDT",
            "linear": True,
            "active": True,
            "info": {"status": "TRADING"},
        }
    }
    monitor = MomentumContinuationShadowMonitor(
        client, Mock(), state_manager=None, now_fn=lambda: 10_000.0
    )
    mainnet_only = {**_candidate(), "symbol": "CHIPUSDT"}

    with patch.object(monitor, "_audit") as audit:
        changed = monitor._process_candidate(
            mainnet_only,
            {"label": "STRONG_RISK_ON", "known": True},
            monitor._profile(),
        )

    assert changed is False
    client.fetch_ohlcv.assert_not_called()
    assert audit.call_args.kwargs["status"] == "SYMBOL_UNAVAILABLE_ON_EXECUTION_VENUE"
    assert audit.call_args.kwargs["details"]["reason"] == "SYMBOL_NOT_LISTED"


def test_non_trading_contract_is_skipped_before_candle_fetch():
    client = Mock()
    client.exchange.markets = {
        "TEST/USDT:USDT": {
            "symbol": "TEST/USDT:USDT",
            "contract": True,
            "swap": True,
            "quote": "USDT",
            "settle": "USDT",
            "linear": True,
            "active": True,
            "info": {"status": "PENDING_TRADING"},
        }
    }
    monitor = MomentumContinuationShadowMonitor(
        client, Mock(), state_manager=None, now_fn=lambda: 10_000.0
    )

    supported, reason = monitor._execution_venue_supports("TEST/USDT")

    assert supported is False
    assert reason == "PENDING_TRADING"


def test_persisted_mainnet_only_setup_is_pruned_before_demo_fetch():
    client = Mock()
    client.exchange.markets = {
        "BTC/USDT:USDT": {
            "symbol": "BTC/USDT:USDT",
            "contract": True,
            "swap": True,
            "quote": "USDT",
            "settle": "USDT",
            "linear": True,
            "active": True,
            "info": {"status": "TRADING"},
        }
    }
    monitor = MomentumContinuationShadowMonitor(
        client, Mock(), state_manager=None, now_fn=lambda: 10_000.0
    )
    monitor._state["setups"]["CHIP/USDT"] = {"setup_id": "old-mainnet-only"}

    with patch.object(monitor, "_audit") as audit:
        snapshot = monitor.collect_once(
            hot_snapshot={
                "status": "OK",
                "timestamp": "1970-01-01T02:46:40Z",
                "candidates": [],
            },
            market_regime={"label": "RANGE", "known": True},
        )

    client.fetch_ohlcv.assert_not_called()
    assert "CHIP/USDT" not in monitor._state["setups"]
    assert snapshot["active_setups"] == 0
    assert audit.call_args.kwargs["status"] == "SYMBOL_UNAVAILABLE_ON_EXECUTION_VENUE"
