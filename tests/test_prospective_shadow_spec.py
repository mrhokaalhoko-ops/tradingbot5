from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.freeze_prospective_shadow_v1 import no_execution_proof
from strategy.prospective_shadow_spec import (
    EVENT_SCHEMA,
    FREEZE_ID,
    POLICY_FREEZE_COMMIT,
    build_event_schema,
    build_freeze_spec,
    validate_freeze_spec,
)


ROOT = Path(__file__).resolve().parents[1]


def _spec():
    return build_freeze_spec(ROOT)


def test_frozen_spec_validates_and_has_four_non_executing_policies():
    spec = _spec()
    assert validate_freeze_spec(spec) == []
    assert spec["freeze_hash"] == "62A4237F1178729BA9E6143D6B7C5FF3FCD7D74C3C7E710E681DA01FA6219D86"
    assert len(spec["policies"]) == 4
    assert all(row["execution_authority"] == "NONE" for row in spec["policies"])
    assert all(row["policy_version"] == "1.3.0" for row in spec["policies"])


def test_margin_and_stop_risk_are_distinct_and_match_runtime_evidence():
    spec = _spec()
    portfolio = spec["common_contract"]["executable_portfolio_model"]
    evidence = spec["resolved_runtime_config"]
    assert portfolio["target_initial_margin_pct_of_equity"] == 1.0
    assert portfolio["base_stop_risk_pct_of_equity"] == 0.25
    assert portfolio["hard_max_stop_risk_pct_of_equity"] == 1.0
    assert "MUST_EQUAL_0.25_PERCENT" in portfolio["runtime_override_freeze_rule"]
    assert evidence["production_environment_snapshot"]["RISK_PER_TRADE_PERCENT"] == 0.25
    assert evidence["production_environment_snapshot"]["ENTRY_MARGIN_PERCENT"] == 1.0


def test_policy_and_future_runtime_identity_are_separate():
    spec = _spec()
    assert spec["identity"]["policy_freeze_commit"] == POLICY_FREEZE_COMMIT
    assert spec["identity"]["runtime_commit_at_freeze"] == "NOT_IMPLEMENTED"
    identity_required = set(EVENT_SCHEMA["properties"]["identity"]["required"])
    assert {
        "policy_freeze_commit", "runtime_commit", "runtime_build_hash",
        "freeze_hash", "policy_config_hash",
    } <= identity_required


def test_config_mutation_without_new_hash_is_rejected():
    spec = _spec()
    spec["policies"][1]["frozen_config"]["geometry"]["target_r"] = 2.0
    errors = validate_freeze_spec(spec)
    assert "CONFIG_HASH_MISMATCH:ACTIVE_ALTCOIN_LONG_V1" in errors
    assert "FREEZE_HASH_MISMATCH" in errors


def test_a_and_b_share_identical_candidate_population_hash():
    by_id = {row["policy_id"]: row for row in _spec()["policies"]}
    assert (
        by_id["ACTIVE_ALTCOIN_LONG_V1"]["frozen_config"]["candidate_contract_hash"]
        == by_id["MOMENTUM_REPRICED_V1"]["frozen_config"]["candidate_contract_hash"]
    )


def test_b_and_c_share_identical_execution_mechanics_after_entry_ready():
    by_id = {row["policy_id"]: row for row in _spec()["policies"]}
    assert (
        by_id["MOMENTUM_REPRICED_V1"]["frozen_config"]["shared_execution_mechanics_hash"]
        == by_id["TOP_DOWN_V1"]["frozen_config"]["shared_execution_mechanics_hash"]
    )
    assert (
        by_id["MOMENTUM_REPRICED_V1"]["frozen_config"]["shared_execution_mechanics"]
        == by_id["TOP_DOWN_V1"]["frozen_config"]["shared_execution_mechanics"]
    )


def test_b_entry_ready_is_initial_event_and_never_waits_for_a():
    by_id = {row["policy_id"]: row for row in _spec()["policies"]}
    timing = by_id["MOMENTUM_REPRICED_V1"]["frozen_config"]["entry_ready_rule"]
    assert timing["entry_ready_at"] == "EXACTLY_EQUAL_TO_CANDIDATE_EMITTED_AT"
    assert timing["wait_for_a_pullback_or_reclaim"] is False
    assert timing["wait_for_top_down_state_machine"] is False


def test_c_entry_ready_waits_for_own_state_transition_not_candidate_emission():
    by_id = {row["policy_id"]: row for row in _spec()["policies"]}
    b = by_id["MOMENTUM_REPRICED_V1"]["frozen_config"]
    c = by_id["TOP_DOWN_V1"]["frozen_config"]
    timing = c["entry_ready_rule"]
    assert timing["entry_ready_at"] == "TIMESTAMP_OF_WAIT_FOR_TRIGGER_TO_ENTRY_READY_TRANSITION"
    assert timing["required_transition"] == "WAIT_FOR_TRIGGER->ENTRY_READY:15M_TRIGGER_RULE"
    assert timing["wait_for_top_down_state_machine"] is True
    assert timing["entry_before_required_transition_forbidden"] is True
    assert b["entry_ready_rule_hash"] != c["entry_ready_rule_hash"]


def test_primary_portfolio_capacity_is_common_ten_and_a_legacy_five_is_metadata_only():
    spec = _spec()
    by_id = {row["policy_id"]: row for row in spec["policies"]}
    portfolio = spec["common_contract"]["executable_portfolio_model"]
    assert portfolio["max_open_positions"] == 10
    assert set(portfolio["capacity_applies_to_all_policies"]) == set(by_id)
    for policy in by_id.values():
        config = policy["frozen_config"]
        assert config["experimental_portfolio_cap"] == 10
    legacy = by_id["ACTIVE_ALTCOIN_LONG_V1"]["frozen_config"]["native_policy_metadata"]
    assert legacy["legacy_portfolio_cap"] == 5
    assert legacy["governs_primary_experimental_portfolio"] is False


def test_cost_model_embeds_spread_once_and_decomposes_12_bps():
    model = _spec()["common_contract"]["execution_cost_model"]
    assert model["quote_side"]["LONG_ENTRY"] == "BEST_ASK"
    assert model["quote_side"]["LONG_TP_EXIT"] == "BEST_BID"
    assert model["quote_side"]["LONG_SL_EXIT"] == "BEST_BID"
    assert model["quote_side"]["LONG_TIME_EXIT"] == "BEST_BID"
    assert model["fee_bps_per_filled_side"] == 5.0
    assert model["additional_adverse_slippage_bps_per_filled_side"] == 1.0
    assert model["fixed_round_trip_cost_excluding_spread_bps"] == 12.0
    assert model["subtract_generic_12_bps_again"] is False


def test_c_does_not_use_5m_as_hard_gate():
    by_id = {row["policy_id"]: row for row in _spec()["policies"]}
    five = by_id["TOP_DOWN_V1"]["frozen_config"]["decision_contract"]["timeframes"]["5m"]
    assert five["hard_gate"] is False


def test_c_state_machine_closes_all_reviewer_gaps():
    by_id = {row["policy_id"]: row for row in _spec()["policies"]}
    state = by_id["TOP_DOWN_V1"]["frozen_config"]["decision_contract"]["state_machine"]
    transitions = "\n".join(state["transitions"])
    assert "TOO_EXTENDED->WAIT_FOR_PULLBACK" in transitions
    assert "ANY_ACTIVE->BIAS_LOST" in transitions
    assert "BREAKOUT_WAIT_RETEST->EXPIRED" in transitions
    assert state["breakout"]["same_bar_retest_allowed"] is False
    assert state["breakout"]["maximum_later_closed_1h_bars"] == 4
    assert state["null_level_rules"]["invalidation_null"] == "DATA_UNAVAILABLE_NO_THESIS"
    assert state["after_invalidation"]["resume_old_thesis"] is False


def test_event_schema_requires_pairing_market_and_policy_evidence():
    required = set(EVENT_SCHEMA["required"])
    assert {
        "identity", "market_snapshot_id", "candidate_id", "candidate_emitted_at", "thesis_id",
        "market", "decision",
    } <= required
    market_required = set(EVENT_SCHEMA["properties"]["market"]["required"])
    assert {
        "best_bid", "best_ask", "spread_bps", "mark_price", "index_price",
        "funding_rate", "next_funding_time", "closed_candle_ids",
    } <= market_required


def _entry_ready_event():
    return {
        "schema_version": 3,
        "experiment_id": "PROSPECTIVE_SHADOW_EXPERIMENT_V1",
        "freeze_id": FREEZE_ID,
        "event_id": "event-00000001",
        "emitted_at_utc": "2026-10-05T00:00:01Z",
        "identity": {
            "policy_id": "MOMENTUM_REPRICED_V1",
            "policy_version": "1.3.0",
            "policy_freeze_commit": POLICY_FREEZE_COMMIT,
            "runtime_commit": "1" * 40,
            "runtime_build_hash": "A" * 64,
            "freeze_hash": "B" * 64,
            "policy_config_hash": "C" * 64,
        },
        "market_snapshot_id": "snapshot-1",
        "candidate_id": "candidate-1",
        "candidate_emitted_at": "2026-10-05T00:00:00Z",
        "entry_ready_at": "2026-10-05T00:00:00Z",
        "thesis_id": "thesis-1",
        "stage": "ENTRY_READY",
        "status": "READY",
        "market": {
            "symbol": "ABCUSDT", "timestamp_utc": "2026-10-05T00:00:00Z",
            "best_bid": 9.99, "best_ask": 10.0, "spread": 0.01,
            "spread_bps": 10.0, "mark_price": 9.995, "index_price": 9.994,
            "funding_rate": 0.0001, "next_funding_time": "2026-10-05T08:00:00Z",
            "closed_candle_ids": {
                "1w": "w1", "1d": "d1", "4h": "h4", "1h": "h1",
                "15m": "m15", "5m": "m5", "1m": "m1",
            },
        },
        "decision": {
            "side": "LONG", "state_before": "WAIT_FOR_TRIGGER",
            "state_after": "ENTRY_READY", "reason_codes": ["TRIGGER_OK"],
        },
        "geometry": {
            "executable_quote_time": "2026-10-05T00:00:00Z",
            "executable_entry": 10.001, "entry_quote_side": "BEST_ASK",
            "entry_price_source": "quote-1", "old_reference_entry": 9.8,
            "stop": 9.8, "stop_source": "near_support", "stop_level_id": "support-1",
            "target": 10.4, "target_source": "near_resistance",
            "target_level_id": "resistance-1", "planned_RR": 1.8,
            "actual_RR_at_entry": 1.98, "geometry_change_reason": "REPRICED_AT_QUOTE",
        },
    }


def test_entry_ready_schema_rejects_missing_or_null_geometry():
    validator = Draft202012Validator(build_event_schema(ROOT))
    valid = _entry_ready_event()
    spec = _spec()
    policy = next(row for row in spec["policies"] if row["policy_id"] == "MOMENTUM_REPRICED_V1")
    valid["identity"]["freeze_hash"] = spec["freeze_hash"]
    valid["identity"]["policy_config_hash"] = policy["config_hash"]
    assert list(validator.iter_errors(valid)) == []
    missing = deepcopy(valid)
    missing.pop("geometry")
    assert list(validator.iter_errors(missing))
    invalid_null = deepcopy(valid)
    invalid_null["geometry"] = None
    assert list(validator.iter_errors(invalid_null))


def test_fill_and_terminal_stages_require_reproducible_evidence():
    validator = Draft202012Validator(build_event_schema(ROOT))
    fill = _entry_ready_event()
    spec = _spec()
    policy = next(row for row in spec["policies"] if row["policy_id"] == "MOMENTUM_REPRICED_V1")
    fill["identity"]["freeze_hash"] = spec["freeze_hash"]
    fill["identity"]["policy_config_hash"] = policy["config_hash"]
    fill["stage"] = "SIMULATED_FILL"
    assert list(validator.iter_errors(fill))
    fill["portfolio_simulation"] = {
        "equity_before": 1000.0, "available_balance_before": 900.0,
        "leverage": 5, "quantity": 5.0, "notional": 50.0,
        "initial_margin": 10.0, "initial_margin_pct_of_equity": 1.0,
        "planned_stop_risk_amount": 1.005, "planned_stop_risk_pct_of_equity": 0.1005,
        "base_stop_risk_pct_of_equity": 0.25,
        "hard_max_stop_risk_pct_of_equity": 1.0,
        "max_open_positions": 10, "open_positions_before": 0,
        "exchange_filters_passed": True, "sizing_formula_id": "CURRENT_RISK_MANAGER_R3",
    }
    fill["execution_costs"] = {
        "model_id": "TAKER_QUOTES_5_PLUS_1_BPS_PER_SIDE_V1",
        "entry_spread_bps": 10.0, "entry_fee_bps": 5.0,
        "entry_slippage_bps": 1.0, "expected_exit_fee_bps": 5.0,
        "expected_exit_slippage_bps": 1.0, "actual_exit_spread_bps": None,
        "actual_exit_fee_bps": None, "actual_exit_slippage_bps": None,
        "spread_embedded_in_quotes": True, "generic_12_bps_subtracted_again": False,
    }
    assert list(validator.iter_errors(fill)) == []
    terminal = deepcopy(fill)
    terminal["stage"] = "TERMINAL_OUTCOME"
    assert list(validator.iter_errors(terminal))
    terminal["execution_costs"].update({
        "actual_exit_spread_bps": 8.0, "actual_exit_fee_bps": 5.0,
        "actual_exit_slippage_bps": 1.0,
    })
    terminal["outcome"] = {
        "terminal_status": "TP", "exit_time": "2026-10-05T00:30:00Z",
        "exit_price": 10.4, "exit_price_source": "best_bid_quote-99",
        "terminal_reason": "TARGET_REACHED", "gross_R": 1.98, "net_R": 1.85,
        "fees_R": 0.05, "spread_R": 0.04, "slippage_R": 0.01,
        "funding_R": 0.0, "MFE_R": 2.1, "MAE_R": -0.2,
        "same_bar_conflict": False,
    }
    assert list(validator.iter_errors(terminal)) == []


def test_specification_layer_has_no_execution_route():
    proof = no_execution_proof(ROOT)
    assert proof["passed"] is True
    assert proof["runtime_engine_present"] is False
    assert proof["order_route_present"] is False
