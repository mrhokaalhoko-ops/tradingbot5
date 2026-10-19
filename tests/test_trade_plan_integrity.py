import threading

from strategy.filter_profiles import FILTER_PROFILES
from strategy.risk_manager import RiskManager


def test_balanced_profile_does_not_fabricate_target_without_market_levels():
    manager = RiskManager()
    profile = {**FILTER_PROFILES["smc_balanced"], "key": "smc_balanced"}

    sl, tp = manager.calculate_sl_tp(
        "buy",
        100.0,
        atr=1.0,
        invalidation_level=99.0,
        market_levels={},
        filter_profile=profile,
    )

    assert sl < 100.0
    assert tp is None
    assert "fallback" in manager.last_block_reason.lower()


def test_nearest_opposing_liquidity_blocks_farther_attractive_target():
    manager = RiskManager()
    plan = manager.calculate_trade_plan(
        "buy",
        entry=100.0,
        sl=99.0,
        market_levels={
            "bearish_fvgs": [{"low": 101.0, "high": 101.3}],
            "bearish_obs": [{"low": 105.0, "high": 106.0}],
        },
        min_rr=1.5,
    )

    assert plan["valid"] is False
    assert plan["blocking_target"]["type"] == "bearish_fvg"
    assert plan["rr"] < 1.5


def test_realistic_market_target_is_used_instead_of_four_r():
    manager = RiskManager()
    plan = manager.calculate_trade_plan(
        "buy",
        entry=100.0,
        sl=99.0,
        market_levels={"bearish_obs": [{"low": 102.0, "high": 102.5}]},
        min_rr=1.5,
    )

    assert plan["valid"] is True
    assert 1.5 <= plan["rr"] < 2.0
    assert plan["tp"] < 102.0  # front-run the actual supply
    assert plan["tp"] != 104.0


def test_explicit_demo_hybrid_mode_keeps_liquidity_as_checkpoint_and_trails_to_four_r():
    manager = RiskManager()
    sl, tp = manager.calculate_sl_tp(
        "buy",
        100.0,
        atr=1.0,
        invalidation_level=99.0,
        market_levels={"bearish_fvgs": [{"low": 101.0, "high": 101.3}]},
        filter_profile={
            "min_rr": 0.0,
            "allow_rr_fallback": True,
            "fallback_rr": 4.0,
            "nearest_liquidity_checkpoint_only": True,
        },
    )

    risk = abs(100.0 - sl)
    assert tp == 100.0 + risk * 4.0
    assert manager.last_plan["source"] == "nearest_liquidity_checkpoint_only"
    assert manager.last_plan["liquidity_checkpoint"]["type"] == "bearish_fvg"


def test_protection_milestones_never_extend_beyond_real_target():
    manager = RiskManager()
    steps = manager.get_tp_steps(
        "buy", entry_price=100.0, sl=99.0, tp3_override=101.8
    )

    assert [step["price"] for step in steps] == [101.5, 101.8]
    assert steps[-1]["label"] == "FINAL_TARGET"
    assert all(step["volume"] == 0.0 for step in steps)


def test_transient_risk_plan_state_is_thread_local():
    manager = RiskManager()
    barrier = threading.Barrier(2)
    observed = {}

    def worker(name, rr):
        manager.last_plan = {"rr": rr}
        manager.last_block_reason = name
        barrier.wait()
        observed[name] = (manager.last_plan["rr"], manager.last_block_reason)

    one = threading.Thread(target=worker, args=("scanner", 1.7))
    two = threading.Thread(target=worker, args=("monitor", 2.1))
    one.start()
    two.start()
    one.join()
    two.join()

    assert observed["scanner"] == (1.7, "scanner")
    assert observed["monitor"] == (2.1, "monitor")
