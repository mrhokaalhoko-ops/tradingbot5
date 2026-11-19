import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytest

import main as main_module
from core.config import Config
from core.exchange import ExchangeClient
from strategy.risk_manager import RiskManager
from strategy.trailing_stop import TrailingStopManager
from utils.state_manager import StateManager
from utils.trade_journal import TradeJournal
import utils.state_manager as state_module
import webapp.server as web_server


PROFILE = {
    "min_rr": 0.0,
    "allow_rr_fallback": True,
    "fallback_rr": 3.0,
    "nearest_liquidity_checkpoint_only": True,
}


def _levels_long():
    return {
        "swing_lows": [{"price": 96.0, "age": 1, "created_at": "2026-10-10T00:00:00Z"}],
        "bullish_obs": [{"low": 97.0, "age": 1, "created_at": "2026-10-10T00:00:00Z"}],
    }


def _calculate_v2(side="buy", entry=100.0, levels=None, anchor=None, invalidation=None):
    manager = RiskManager()
    with patch.object(Config, "STRUCTURAL_STOP_V2_ENABLED", True), patch.object(
        Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", True
    ):
        result = manager.calculate_sl_tp(
            side,
            entry,
            atr=1.0,
            invalidation_level=invalidation,
            market_levels=levels or _levels_long(),
            setup_anchor=anchor,
            tick_size=0.1,
            spread=0.05,
            filter_profile=PROFILE,
        )
    return manager, result


def test_v2_entry_anchor_is_not_automatically_thesis_invalidation_long():
    anchor = {"type": "bullish_ob", "low": 99.0, "high": 99.5, "created_at": "2026-10-11T00:00:00Z"}
    manager, (sl, _) = _calculate_v2(anchor=anchor)
    plan = manager.last_plan["stop_plan"]

    assert plan["relation_to_thesis"] == "THESIS_STRUCTURE"
    assert plan["entry_anchor_level"] == 99.0
    assert plan["selected_thesis_invalidation"] == 96.0
    assert plan["selected_structural_level"] == 96.0
    assert plan["structural_level_type"].startswith("swing_low")
    assert sl == pytest.approx(95.7)
    assert any(
        item.get("relation_to_thesis") == "ENTRY_ANCHOR"
        and item.get("reject_reason") == "LOWER_THESIS_RELEVANCE"
        for item in plan["rejected_structural_candidates"]
    )


def test_v2_entry_anchor_is_not_automatically_thesis_invalidation_short():
    levels = {
        "swing_highs": [{"price": 105.0, "age": 1}],
        "bearish_obs": [{"high": 104.0, "age": 1}],
    }
    anchor = {"type": "bearish_ob", "low": 100.5, "high": 101.0}
    manager, (sl, _) = _calculate_v2(
        side="sell", levels=levels, anchor=anchor
    )
    plan = manager.last_plan["stop_plan"]

    assert plan["relation_to_thesis"] == "THESIS_STRUCTURE"
    assert plan["selected_structural_level"] == 105.0
    assert sl == pytest.approx(105.3)


def test_v2_explicit_thesis_invalidation_has_priority_and_is_not_clamped():
    manager, (sl, _) = _calculate_v2(invalidation=94.0)
    plan = manager.last_plan["stop_plan"]

    assert plan["relation_to_thesis"] == "EXPLICIT_THESIS_INVALIDATION"
    assert plan["selected_structural_level"] == 94.0
    assert sl == pytest.approx(93.7)
    assert plan["clamp"] == "NONE"
    assert plan["clamp_attempted"] is False


def test_v2_invalidation_equal_to_entry_anchor_does_not_override_real_structure():
    anchor = {"type": "bullish_ob", "low": 99.0, "high": 99.5}
    manager, (sl, _) = _calculate_v2(anchor=anchor, invalidation=99.0)
    plan = manager.last_plan["stop_plan"]

    assert plan["selected_thesis_invalidation"] == 96.0
    assert plan["relation_to_thesis"] == "THESIS_STRUCTURE"
    assert sl == pytest.approx(95.7)
    assert any(
        item.get("relation_to_thesis") == "ENTRY_ANCHOR_ALIAS"
        and item.get("reject_reason") == "LOWER_THESIS_RELEVANCE"
        for item in plan["rejected_structural_candidates"]
    )


def test_v2_fails_closed_when_only_entry_anchor_alias_exists():
    anchor = {"type": "bullish_ob", "low": 99.0, "high": 99.5}
    manager, (sl, tp) = _calculate_v2(
        anchor=anchor,
        invalidation=99.0,
        levels={"bullish_obs": [{"low": 99.0, "age": 1}]},
    )

    assert sl is None and tp is None
    assert manager.last_block_reason == "NO_VALID_THESIS_INVALIDATION"


def test_structural_v2_flag_rolls_back_to_v1():
    manager = RiskManager()
    anchor = {"type": "bullish_ob", "low": 99.0, "high": 99.5}
    with patch.object(Config, "STRUCTURAL_STOP_V2_ENABLED", False), patch.object(
        Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", True
    ):
        manager.calculate_sl_tp(
            "buy", 100.0, atr=1.0, invalidation_level=99.0,
            market_levels=_levels_long(), setup_anchor=anchor,
            tick_size=0.1, spread=0.05, filter_profile=PROFILE,
        )
    assert manager.last_plan["stop_plan"]["policy"] == "STRUCTURAL_INITIAL_STOP_V1"


def _trailing_client():
    client = Mock()
    client.fetch_ohlcv.return_value = pd.DataFrame({
        "high": [100 + i * 0.1 for i in range(50)],
        "low": [99 + i * 0.1 for i in range(50)],
        "close": [99.5 + i * 0.1 for i in range(50)],
    })
    client.get_price_tick_size.return_value = 0.1
    client.get_position_amount.return_value = 1.0
    client.normalize_price.side_effect = lambda _symbol, value: value
    client.replace_sl_order.side_effect = lambda _symbol, _side, _amount, value: value
    client._is_hedge_mode = False
    return client


@pytest.mark.parametrize("activation_r", [1.0, 1.5, 2.0])
def test_trade_management_v2_activation_is_configurable(activation_r):
    manager = TrailingStopManager()
    manager.register_trade("BTC/USDT", "buy", 100.0, 95.0, 1.0)
    manager.get_confirmed_swing = Mock(return_value=104.0)
    manager.calculate_atr = Mock(return_value=1.0)
    client = _trailing_client()
    current = 100.0 + 5.0 * activation_r

    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", True), patch.object(
        Config, "TRADE_MANAGEMENT_V2_ACTIVATION_R", activation_r
    ):
        assert manager.update("BTC/USDT", current, client) == pytest.approx(103.7)


def test_trade_management_v2_waits_below_configured_activation():
    manager = TrailingStopManager()
    manager.register_trade("BTC/USDT", "buy", 100.0, 95.0, 1.0)
    client = _trailing_client()
    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", True), patch.object(
        Config, "TRADE_MANAGEMENT_V2_ACTIVATION_R", 2.0
    ):
        assert manager.update("BTC/USDT", 107.5, client) is None
    client.fetch_ohlcv.assert_not_called()


def test_trade_management_v2_never_moves_stop_backward():
    manager = TrailingStopManager()
    manager.register_trade("BTC/USDT", "buy", 100.0, 95.0, 1.0)
    manager._trades["BTC/USDT"]["current_sl"] = 104.0
    manager.get_confirmed_swing = Mock(return_value=103.0)
    manager.calculate_atr = Mock(return_value=1.0)
    client = _trailing_client()
    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", True), patch.object(
        Config, "TRADE_MANAGEMENT_V2_ACTIVATION_R", 1.0
    ):
        assert manager.update("BTC/USDT", 110.0, client) is None
    client.replace_sl_order.assert_not_called()


def test_trade_management_v2_hysteresis_skips_trivial_improvement():
    manager = TrailingStopManager()
    manager.register_trade("BTC/USDT", "buy", 100.0, 95.0, 1.0)
    manager._trades["BTC/USDT"]["current_sl"] = 103.65
    manager.get_confirmed_swing = Mock(return_value=104.0)
    manager.calculate_atr = Mock(return_value=1.0)
    client = _trailing_client()
    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", True), patch.object(
        Config, "TRADE_MANAGEMENT_V2_ACTIVATION_R", 1.0
    ):
        assert manager.update("BTC/USDT", 110.0, client) is None
    client.replace_sl_order.assert_not_called()


def test_v2_disables_legacy_tp_price_hugging_but_keeps_full_position(tmp_path):
    state = Mock()
    state.get_state.return_value = {"entry_metadata": {
        "BTC/USDT": {
            "side": "BUY", "entry_price": 100.0, "initial_sl": 95.0,
            "current_sl": 95.0, "tp": 115.0,
            "tp_steps": [{"price": 105.0, "volume": 0.0, "label": "PROTECT_1"}],
            "reached_tp_steps": [],
        }
    }}
    client = Mock()
    trailing = Mock()
    notifier = Mock()
    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", True):
        main_module.manage_tp_ladder(client, state, trailing, notifier)
    client.replace_sl_order.assert_not_called()
    client.close_position.assert_not_called()


def test_confirmed_protection_notification_is_persistent_and_deduplicated(tmp_path):
    old = state_module.STATE_FILE
    state_module.STATE_FILE = str(tmp_path / "state.json")
    try:
        state = StateManager()
        first = state.record_confirmed_protection_notification(
            "BTCUSDT", trade_id="t1", protection_revision="r1",
            confirmed_sl=101.0, stage="STRUCTURAL_TRAIL_V2",
        )
        restarted = StateManager()
        duplicate = restarted.record_confirmed_protection_notification(
            "BTCUSDT", trade_id="t1", protection_revision="r1",
            confirmed_sl=101.0, stage="STRUCTURAL_TRAIL_V2",
        )
    finally:
        state_module.STATE_FILE = old
    assert first["emit"] is True
    assert duplicate["emit"] is False


def test_real_confirmed_sl_change_emits_once_and_restart_emits_none(tmp_path):
    old = state_module.STATE_FILE
    state_module.STATE_FILE = str(tmp_path / "state.json")
    notifier = Mock()
    try:
        state = StateManager()
        kwargs = {
            "state_manager": state, "notifier": notifier, "symbol": "BTC/USDT",
            "meta": {"trade_id": "t-notify"}, "stage": "STRUCTURAL_TRAIL_V2",
            "revision": "rev-1", "previous_sl": 95.0, "requested_sl": 101.0,
            "confirmed_sl": 101.0, "message": "confirmed",
        }
        assert main_module._emit_confirmed_protection_notification(**kwargs)["emit"] is True
        restarted = StateManager()
        kwargs["state_manager"] = restarted
        assert main_module._emit_confirmed_protection_notification(**kwargs)["emit"] is False
    finally:
        state_module.STATE_FILE = old
    notifier.send_message.assert_called_once_with("confirmed")


def test_failed_sl_replacement_keeps_old_metadata_and_sends_no_notification():
    state = Mock()
    metadata = {
        "BTC/USDT": {
            "trade_id": "t-failed", "side": "BUY", "entry_price": 100.0,
            "initial_sl": 95.0, "current_sl": 95.0, "sl": 95.0, "tp": 115.0,
            "amount": 1.0, "original_amount": 1.0,
            "tp_steps": [{"position": 1, "label": "PROTECT_1", "price": 105.0,
                          "volume": 0.0, "status": "idle"}],
        }
    }
    state.get_state.return_value = {"entry_metadata": metadata}
    client = Mock()
    client.get_current_price.return_value = 106.0
    client.get_position_amount.return_value = 1.0
    client.replace_sl_order.return_value = False
    notifier, trailing = Mock(), Mock()

    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", False):
        main_module.manage_tp_ladder(client, state, trailing, notifier)

    assert metadata["BTC/USDT"]["current_sl"] == 95.0
    notifier.send_message.assert_not_called()


def test_live_sl_ui_sync_flag_rolls_back_to_persisted_confirmed_sl():
    prior_state, prior_client = web_server._state_manager, web_server._client
    state = Mock()
    state.get.return_value = {"BTC/USDT": {
        "initial_sl": 95.0, "sl": 95.0, "current_sl": 97.0, "tp": 115.0,
        "protection_stage": "TRAILING_STOP_V1",
    }}
    client = Mock()
    client._is_hedge_mode = False
    client.exchange.fetch_positions.return_value = [{
        "symbol": "BTC/USDT:USDT", "entryPrice": 100.0, "markPrice": 101.0,
        "info": {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100",
                 "markPrice": "101", "unRealizedProfit": "0.1", "leverage": "5"},
    }]
    web_server._state_manager, web_server._client = state, client
    try:
        with patch.object(Config, "LIVE_SL_UI_SYNC_ENABLED", False):
            position = web_server._get_live_positions()[0]
    finally:
        web_server._state_manager, web_server._client = prior_state, prior_client
    assert position["sl"] == 95.0
    assert position["initial_sl"] == 95.0
    assert position["current_sl"] == 97.0
    client.get_position_protection_orders.assert_not_called()


def test_post_exit_observation_records_checkpoints_and_stop_then_run(tmp_path):
    old = state_module.STATE_FILE
    state_module.STATE_FILE = str(tmp_path / "state.json")
    try:
        state = StateManager()
        assert state.register_post_exit_observation({
            "trade_id": "t-post", "symbol": "BTC/USDT", "side": "BUY",
            "entry_price": 100.0, "initial_sl": 95.0,
            "initial_risk_per_unit": 5.0, "exit_price": 99.0,
            "exit_at_epoch": 1_000.0, "is_stop_exit": True,
            "post_exit_mfe_r": -0.2, "post_exit_mae_r": -0.2,
            "checkpoints": {}, "stop_then_run": [], "next_check_at": 1_000.0,
            "complete": False,
        })
        client = Mock()
        client.get_current_price.return_value = 110.0
        changed = main_module.monitor_post_exit_observations(
            client, state, now=1_000.0 + 121 * 60
        )
        item = state.get_post_exit_observations()["t-post"]
    finally:
        state_module.STATE_FILE = old
    assert changed == 1
    assert set(item["checkpoints"]) == {"15m", "30m", "60m", "120m"}
    assert item["post_exit_mfe_r"] == pytest.approx(2.0)
    assert item["stop_then_run"] == [0.5, 1.0, 1.5, 2.0]
    assert item["complete"] is True


def test_trade_journal_serializes_timestamp_like_runtime_record(tmp_path):
    client = Mock()
    client.exchange.fetch_ohlcv.return_value = []
    client.exchange.fetch_trades.return_value = []
    with patch.object(Config, "TRADE_JOURNAL_DIR", str(tmp_path)), patch.object(
        Config, "TRADE_JOURNAL_TIMEFRAMES", ""
    ), patch.object(Config, "TRADE_JOURNAL_FETCH_TRADES", False):
        journal = TradeJournal()
        path = journal.record_entry(
            client, "BTC/USDT",
            {
                "trade_id": "timestamp-case", "side": "BUY",
                "entry_time": pd.Timestamp("2026-10-11T00:00:00Z"),
                "entry_price": np.float64(100.0), "amount": np.int64(1),
                "nested": {
                    "closed_at": pd.Timestamp("2026-10-11T00:05:00Z"),
                    "python_time": datetime(2026, 10, 11, 0, 6, tzinfo=timezone.utc),
                    "non_finite": np.float64(np.nan),
                },
            },
            {"decision_time": pd.Timestamp("2026-10-11T00:00:00Z")},
        )
    payload = json.loads((Path(path) / "trade_entry.json").read_text(encoding="utf-8"))
    assert payload["entry_meta"]["entry_time"].startswith("2026-10-11T00:00:00")
    assert payload["entry_meta"]["nested"]["closed_at"].startswith("2026-10-11T00:05:00")
    assert payload["entry_meta"]["nested"]["python_time"] == "2026-10-11T00:06:00Z"
    assert payload["entry_meta"]["nested"]["non_finite"] is None
    assert payload["entry_meta"]["entry_price"] == 100.0
    assert payload["entry_meta"]["amount"] == 1


def _market(max_lot="100", max_market="5"):
    return {
        "symbol": "DYM/USDT:USDT", "type": "swap", "contract": True,
        "swap": True, "linear": True, "active": True,
        "quote": "USDT", "settle": "USDT", "precision": {"amount": 0.1},
        "limits": {"amount": {"min": 0.1, "max": 100.0}, "cost": {"min": 5.0}},
        "info": {"filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.1", "maxQty": max_lot},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "maxQty": max_market},
        ]},
    }


def test_market_preflight_uses_strictest_lot_and_market_lot_max_qty():
    client = ExchangeClient.__new__(ExchangeClient)
    client.exchange = Mock()
    market = _market()
    client.exchange.markets = {market["symbol"]: market}
    client.exchange.amount_to_precision.side_effect = lambda _s, amount: str(amount)
    client.last_execution_failure = None
    assert client.preview_order_amount("DYM/USDT", 6.0, 1.0) is None
    assert client.last_execution_failure["code"] == "MARKET_MAX_QTY_EXCEEDED"
    assert client.last_execution_failure["send_attempted"] is False


def test_market_preflight_accepts_amount_inside_both_limits():
    client = ExchangeClient.__new__(ExchangeClient)
    client.exchange = Mock()
    market = _market()
    client.exchange.markets = {market["symbol"]: market}
    client.exchange.amount_to_precision.side_effect = lambda _s, amount: str(amount)
    client.last_execution_failure = None
    assert client.preview_order_amount("DYM/USDT", 5.0, 2.0) == 5.0


class _BinanceReject(Exception):
    code = -4005


def _entry_client_with_send_error(error):
    client = ExchangeClient.__new__(ExchangeClient)
    client.exchange = Mock()
    client._is_hedge_mode = False
    client.last_execution_failure = None
    client._resolve_usdm_contract_market = Mock(return_value=(
        "BTC/USDT:USDT", {"symbol": "BTC/USDT:USDT"}
    ))
    client._set_and_confirm_leverage = Mock(return_value=True)
    client._validate_order_market_rules = Mock(return_value=0.1)
    client.exchange.create_order.side_effect = error
    client._find_order_by_client_id = Mock(return_value=None)
    return client


def test_explicit_exchange_rejection_is_not_status_unknown():
    client = _entry_client_with_send_error(_BinanceReject("code=-4005 maxQty"))
    assert client.execute_trade(
        "BTC/USDT", "buy", 0.1, current_price=100.0,
        client_order_id="explicit-reject",
    ) is None
    assert client.last_execution_failure["code"] == "ENTRY_REJECTED_BY_EXCHANGE"
    assert client.last_execution_failure["send_attempted"] is True


def test_ambiguous_transport_failure_requires_reconciliation():
    client = _entry_client_with_send_error(TimeoutError("connection timeout"))
    assert client.execute_trade(
        "BTC/USDT", "buy", 0.1, current_price=100.0,
        client_order_id="transport-ambiguous",
    ) is None
    assert client.last_execution_failure["code"] == "ORDER_SUBMISSION_UNRESOLVED"
    assert client.last_execution_failure["send_attempted"] is True
    client._find_order_by_client_id.assert_called_once()


def test_normal_tp_stages_are_protection_milestones_not_partial_closes():
    steps = RiskManager().get_tp_steps(
        "buy", entry_price=100.0, sl=95.0, tp3_override=115.0,
        is_bottom_fishing=False,
    )
    assert steps
    assert all(step["volume"] == 0.0 for step in steps)
    assert steps[-1]["label"] == "FINAL_TARGET"


def test_mini_app_effective_runtime_config_uses_state_risk_not_hardcoded():
    prior = web_server._state_manager
    state = Mock()
    state.get_risk_pct.return_value = 0.37
    web_server._state_manager = state
    try:
        config = web_server._effective_runtime_config()
    finally:
        web_server._state_manager = prior
    assert config["base_stop_risk_percent"] == 0.37
    assert config["entry_margin_percent"] == Config.ENTRY_MARGIN_PERCENT
    assert config["hard_risk_cap_percent"] == Config.HARD_MAX_RISK_PER_TRADE_PERCENT


def test_mini_app_displays_confirmed_algo_sl_without_mutating_initial_sl():
    prior_state, prior_client = web_server._state_manager, web_server._client
    state = Mock()
    state.get.return_value = {"BTC/USDT": {
        "initial_sl": 95.0, "sl": 95.0, "current_sl": 97.0, "tp": 115.0,
        "protection_stage": "STRUCTURAL_TRAIL_V2",
        "current_sl_confirmed_at": "2026-10-11T00:00:00Z",
    }}
    client = Mock()
    client._is_hedge_mode = False
    client.exchange.fetch_positions.return_value = [{
        "symbol": "BTC/USDT:USDT", "entryPrice": 100.0, "markPrice": 108.0,
        "unrealizedPnl": 0.8, "leverage": 5,
        "info": {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100",
                 "markPrice": "108", "unRealizedProfit": "0.8", "leverage": "5"},
    }]
    client.get_position_protection_orders.return_value = {
        "status": "OK", "sl_orders": [{"info": {"triggerPrice": "102.0"}}],
    }
    client._order_trigger_price.side_effect = lambda order: float(order["info"]["triggerPrice"])
    web_server._state_manager, web_server._client = state, client
    try:
        with patch.object(Config, "LIVE_SL_UI_SYNC_ENABLED", True):
            position = web_server._get_live_positions()[0]
    finally:
        web_server._state_manager, web_server._client = prior_state, prior_client
    assert position["sl"] == 95.0
    assert position["current_sl"] == 102.0
    assert position["current_sl_source"] == "binance_algo_order"
    assert position["protection_stage"] == "STRUCTURAL_TRAIL_V2"


def test_feature_flags_default_to_safe_rollback_state():
    assert Config.STRUCTURAL_STOP_V2_ENABLED is False
    assert Config.TRADE_MANAGEMENT_V2_ENABLED is False
