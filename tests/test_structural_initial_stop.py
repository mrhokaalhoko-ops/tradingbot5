import json
from unittest.mock import patch

import pytest

from core.config import Config
from strategy.risk_manager import RiskManager
from utils.state_manager import StateManager


PROFILE = {
    "min_rr": 0.0,
    "allow_rr_fallback": True,
    "fallback_rr": 4.0,
    "nearest_liquidity_checkpoint_only": True,
}


def anchor(kind, low, high, created_at="2026-10-11T00:00:00Z"):
    return {
        "type": kind,
        "low": low,
        "high": high,
        "age": 1,
        "created_at": created_at,
    }


def calculate(manager, side, entry, atr, setup, levels=None, tick=0.1):
    invalidation = setup["low"] if side == "buy" else setup["high"]
    levels = levels or {
        "bullish_obs": [setup] if side == "buy" else [],
        "bearish_obs": [setup] if side == "sell" else [],
    }
    with patch.object(Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", True):
        return manager.calculate_sl_tp(
            side,
            entry,
            atr=atr,
            invalidation_level=invalidation,
            market_levels=levels,
            setup_anchor=setup,
            tick_size=tick,
            spread=0.05,
            filter_profile=PROFILE,
        )


def test_long_stop_is_below_causal_support_plus_outward_buffer():
    manager = RiskManager()
    setup = anchor("bullish_ob", 98.0, 99.0)
    sl, tp = calculate(manager, "buy", 100.0, 1.0, setup)

    plan = manager.last_plan["stop_plan"]
    assert sl == pytest.approx(97.7)
    assert tp > 100.0
    assert sl < plan["selected_structural_level"] == 98.0
    assert plan["structural_level_type"] == "setup_anchor:bullish_ob"
    assert plan["volatility_buffer"] == pytest.approx(0.3)
    assert plan["clamp"] == "NONE"


def test_short_stop_is_above_causal_resistance_plus_outward_buffer():
    manager = RiskManager()
    setup = anchor("bearish_ob", 101.0, 102.0)
    sl, tp = calculate(manager, "sell", 100.0, 1.0, setup)

    plan = manager.last_plan["stop_plan"]
    assert sl == pytest.approx(102.3)
    assert tp < 100.0
    assert sl > plan["selected_structural_level"] == 102.0
    assert plan["structural_level_type"] == "setup_anchor:bearish_ob"


def test_new_policy_never_uses_old_max_distance_clamp_inside_structure():
    manager = RiskManager()
    setup = anchor("bullish_ob", 90.0, 91.0)
    sl, _ = calculate(manager, "buy", 100.0, 1.0, setup)

    plan = manager.last_plan["stop_plan"]
    assert plan["old_stop"] == pytest.approx(96.0)
    assert plan["old_stop_plan"]["clamp"] == "MAX_DISTANCE_TIGHTENING"
    assert plan["old_stop"] > setup["low"]
    assert sl == pytest.approx(89.7)
    assert sl < setup["low"]
    assert plan["clamp_attempted"] is False


def test_far_structural_stop_inside_hard_cap_is_retained_not_clamped():
    manager = RiskManager()
    setup = anchor("bullish_ob", 90.0, 91.0)
    sl, _ = calculate(manager, "buy", 100.0, 1.0, setup)
    risk = manager.evaluate_structural_stop_account_risk(
        equity=1000.0,
        amount=0.1,
        entry_price=100.0,
        stop_price=sl,
    )

    assert sl == pytest.approx(89.7)
    assert risk["valid"] is True
    assert risk["actual_risk_pct"] < 1.0


def test_structural_stop_over_hard_account_risk_skips_instead_of_moving_stop():
    manager = RiskManager()
    risk = manager.evaluate_structural_stop_account_risk(
        equity=100.0,
        amount=10.0,
        entry_price=100.0,
        stop_price=90.0,
    )

    assert risk["valid"] is False
    assert risk["reason"] == "SKIP_STRUCTURAL_STOP_RISK_TOO_HIGH"
    assert risk["stop"] == 90.0


def test_missing_causal_structural_invalidation_fails_closed():
    manager = RiskManager()
    with patch.object(Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", True):
        sl, tp = manager.calculate_sl_tp(
            "buy",
            100.0,
            atr=1.0,
            invalidation_level=98.5,
            market_levels={},
            setup_anchor=None,
            filter_profile=PROFILE,
        )

    assert sl is None and tp is None
    assert manager.last_plan["reason"] == "NO_VALID_STRUCTURAL_INVALIDATION"


@pytest.mark.parametrize(
    "side,setup,expected",
    [
        ("buy", anchor("bullish_ob", 98.03, 99.0), 97.7),
        ("sell", anchor("bearish_ob", 101.0, 102.03), 102.4),
    ],
)
def test_tick_rounding_is_always_outward_from_structure(side, setup, expected):
    manager = RiskManager()
    sl, _ = calculate(manager, side, 100.0, 1.0, setup, tick=0.1)
    level = setup["low"] if side == "buy" else setup["high"]

    assert sl == pytest.approx(expected)
    assert sl < level if side == "buy" else sl > level


def test_structural_stop_plan_survives_state_restart_unchanged(tmp_path):
    state_path = tmp_path / "state.json"
    with patch("utils.state_manager.STATE_FILE", str(state_path)):
        state = StateManager()
        plan = {
            "policy": "STRUCTURAL_INITIAL_STOP_V1",
            "selected_structural_level": 98.0,
            "final_sl": 97.7,
            "clamp": "NONE",
        }
        assert state.save_entry_metadata("BTCUSDT", {
            "entry_price": 100.0,
            "sl": 97.7,
            "target_plan": {"stop_plan": plan},
        })
        restarted = StateManager()

    stored = restarted.get_entry_metadata("BTCUSDT")
    assert stored["sl"] == 97.7
    assert stored["target_plan"]["stop_plan"] == plan


def test_trailing_manager_source_is_not_part_of_structural_policy():
    # A narrow regression guard: the stop policy lives in RiskManager and must
    # not gain authority over post-entry trailing behavior.
    from strategy.trailing_stop import TrailingStopManager

    assert "STRUCTURAL_INITIAL_STOP_V1" not in json.dumps(
        sorted(TrailingStopManager.__dict__.keys())
    )
