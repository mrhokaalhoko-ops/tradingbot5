"""Immutable specification for Prospective Shadow Experiment v1.

This module defines research policy contracts only.  It has no exchange
client, credentials, order model, or runtime loop.  The generated hashes are
the authority for deciding whether prospective rows may be pooled.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ID = "PROSPECTIVE_SHADOW_EXPERIMENT_V1"
FREEZE_ID = "PSE_V1_FREEZE_R4_20261005"
FROZEN_AT_UTC = "2026-10-05T00:00:00Z"

# The policy was frozen against this source baseline.  The future Shadow
# engine MUST identify its own commit and build separately in every event.
POLICY_FREEZE_COMMIT = "031666f2fdcf59532684754370297ee78c9c19b3"
RUNTIME_COMMIT_AT_FREEZE = "NOT_IMPLEMENTED"

# Safe, non-secret production values supplied during the Railway migration.
# They agree with the defaults at POLICY_FREEZE_COMMIT.  This is deliberately
# explicit because 1% is margin, while 0.25% is the default stop-risk budget.
RESOLVED_RUNTIME_CONFIG = {
    "evidence_status": "SOURCE_AND_PRODUCTION_ENV_AGREE",
    "policy_freeze_commit": POLICY_FREEZE_COMMIT,
    "production_environment_snapshot": {
        "RISK_PER_TRADE_PERCENT": 0.25,
        "ENTRY_MARGIN_PERCENT": 1.0,
        "LEVERAGE": 5,
        "MAX_OPEN_TRADES": 10,
    },
    "source_defaults_at_policy_freeze_commit": {
        "RISK_PER_TRADE_PERCENT": 0.25,
        "MIN_RISK_PER_TRADE_PERCENT": 0.05,
        "HARD_MAX_RISK_PER_TRADE_PERCENT": 1.0,
        "ENTRY_MARGIN_PERCENT": 1.0,
        "LEVERAGE": 5,
        "MAX_OPEN_TRADES": 10,
        "HARD_MAX_OPEN_TRADES": 10,
        "MAX_NOTIONAL_BALANCE_PCT": 0.30,
        "MIN_SL_DISTANCE_PCT": 0.003,
    },
    "evidence_files": {
        "source_config": "core/config.py",
        "sizing_implementation": "strategy/risk_manager.py::calculate_position_size",
        "hard_runtime_guard": "main.py::execute_and_protect_trade",
        "runtime_override_source": "utils/state_manager.py::get_risk_pct",
        "production_env_evidence": "USER_PROVIDED_RAILWAY_MIGRATION_VARIABLE_SNAPSHOT_REDACTED",
    },
}

FORBIDDEN_EXECUTION_IMPORTS = (
    "core.exchange",
    "ccxt",
    "binance.client",
    "execute_and_protect_trade",
    "create_order",
    "place_order",
)

CONTROL_SOURCE_FILES = (
    "main.py",
    "core/config.py",
    "engines/ta_engine.py",
    "strategy/filter_profiles.py",
    "strategy/entry_confirmation.py",
    "strategy/risk_manager.py",
    "utils/state_manager.py",
)

A_SOURCE_FILES = (
    "strategy/active_altcoin_shadow.py",
    "trading_v2/active_altcoin_scalper.py",
    "utils/active_altcoin_shadow_monitor.py",
    "utils/hot_list_monitor.py",
)

# Freeze R4 was independently accepted with these exact source-lock hashes.
# Keep them immutable instead of re-hashing checkout bytes at runtime: Git may
# materialize identical Python source with LF or CRLF line endings on different
# hosts, which must not create a different policy identity.
FROZEN_SOURCE_SHA256 = {
    "main.py": "D49569B9079090DA3CDFB195A77B069C2B25DD6023CE8EC7DB1CFFD6D4C55C66",
    "core/config.py": "291C341D5ECA8D37D44778192B45161A55232ADC9924319C8EBE7E7C46C95147",
    "engines/ta_engine.py": "F0B871DB3E96898E4AFAB0F64EA9DDBFEA2F2700D4CCF4F9F01E4806D4658D38",
    "strategy/filter_profiles.py": "5976537F589247D061C024C94679E13D07E6DD38644CA6CDCB3715FABFD84B38",
    "strategy/entry_confirmation.py": "62EF3339958E880E5A4DD5E22716EA690F58117679C64BBD04EC511B9A4C3D56",
    "strategy/risk_manager.py": "ECFD1D42BE6DF80C9EC53081E042D6FE02BE6825C822ACC174CFE3AC72DCF330",
    "utils/state_manager.py": "06B372EA25FD5007D7EDB62FE52CE0ADB271109835DCC59458775452518242B6",
    "strategy/active_altcoin_shadow.py": "73385A5F6557843608284BA5EC63C252521A8479CE22C7319914441E15651F88",
    "trading_v2/active_altcoin_scalper.py": "49B604E66BA76E25FFFBDB8D9362F40765F343E5E23249738FA8FDBFF108FD02",
    "utils/active_altcoin_shadow_monitor.py": "68D06ECB81E72C11CD77D641431F04A59C270BBDBBA7095E1619C1ED53A95CFD",
    "utils/hot_list_monitor.py": "50C3B7387E2639324E0C9D63AFCA4F16E0B9CEF622DB9C5D8B327869EC9584C2",
}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _file_hash(root: Path, relative_path: str) -> str:
    return hashlib.sha256((root / relative_path).read_bytes()).hexdigest().upper()


COMMON_CONTRACT = {
    "authority": "SHADOW_ONLY_NO_ORDER_AUTHORITY",
    "clock": "UTC",
    "start_rule": "PROSPECTIVE_ONLY_AFTER_RUNTIME_FREEZE_EVENT",
    "backfill_allowed": False,
    "shared_stream": {
        "same_market_snapshot_for_all_policies": True,
        "required_identifiers": [
            "market_snapshot_id", "candidate_id", "thesis_id",
        ],
        "required_quote_fields": [
            "best_bid", "best_ask", "spread", "spread_bps", "mark_price",
            "index_price", "funding_rate", "next_funding_time",
        ],
        "required_closed_candle_ids": ["1w", "1d", "4h", "1h", "15m", "5m", "1m"],
    },
    "observation_fidelity": {
        "analysis_and_execution_are_separate": True,
        "analysis_quote_collection": "AFTER_ALL_REST_CANDLE_REQUESTS_DIAGNOSTIC_ONLY",
        "execution_stream": "ORDERED_PUBLIC_BOOK_TICKER_WITH_EVENT_AND_RECEIPT_TIMESTAMPS",
        "a_fill_capture": "FIRST_PROSPECTIVELY_OBSERVED_KLINE_1M_OPEN_AT_SCHEDULED_BOUNDARY",
        "historical_next_open_lookup_forbidden": True,
        "open_position_monitoring": "CONTINUOUS_BETWEEN_STRATEGY_SCANS",
        "stream_gap_both_levels": "CAUSAL_AMBIGUOUS_NEVER_ARBITRARY_WIN_OR_LOSS",
    },
    "outcome_rules": {
        "same_bar_tp_sl": "CAUSAL_AMBIGUOUS_AND_CONSERVATIVE_POLICY_LOSS_REPORTED_SEPARATELY",
        "funding": "ACTUAL_OBSERVED_RATE_AT_SETTLEMENT_WHILE_POSITION_OPEN",
        "entry_quote": "FIRST_NON_STALE_EXECUTABLE_QUOTE_AFTER_ENTRY_READY",
        "long_fill_price": "BEST_ASK",
        "short_fill_price": "BEST_BID",
        "quote_max_age_seconds": 2,
        "quote_wait_timeout_seconds": 5,
        "missing_quote_action": "DATA_UNAVAILABLE_NO_FILL",
    },
    "execution_cost_model": {
        "model_id": "TAKER_QUOTES_5_PLUS_1_BPS_PER_SIDE_V1",
        "quote_side": {
            "LONG_ENTRY": "BEST_ASK",
            "LONG_TP_EXIT": "BEST_BID",
            "LONG_SL_EXIT": "BEST_BID",
            "LONG_TIME_EXIT": "BEST_BID",
            "SHORT_ENTRY": "BEST_BID",
            "SHORT_TP_EXIT": "BEST_ASK",
            "SHORT_SL_EXIT": "BEST_ASK",
            "SHORT_TIME_EXIT": "BEST_ASK",
        },
        "trigger_rule": (
            "FIRST_NON_STALE_EXECUTABLE_QUOTE_IN_TIMESTAMP_ORDER_THAT_REACHES_LEVEL;"
            "NO_1M_OHLC_ORDER_INFERENCE_WHEN_QUOTE_ORDER_IS_MISSING"
        ),
        "fee_bps_per_filled_side": 5.0,
        "additional_adverse_slippage_bps_per_filled_side": 1.0,
        "fill_formula": {
            "LONG_ENTRY": "best_ask*(1+0.0001)",
            "LONG_EXIT": "best_bid*(1-0.0001)",
            "SHORT_ENTRY": "best_bid*(1-0.0001)",
            "SHORT_EXIT": "best_ask*(1+0.0001)",
        },
        "spread_accounting": "EMBEDDED_ONCE_BY_ASK_ENTRY_AND_BID_EXIT_OR_REVERSE_FOR_SHORT",
        "fixed_round_trip_cost_excluding_spread_bps": 12.0,
        "legacy_12_bps_interpretation": "10_BPS_FEES_PLUS_2_BPS_ADDITIONAL_SLIPPAGE",
        "subtract_generic_12_bps_again": False,
        "funding_accounting": "SIGNED_ACTUAL_CASHFLOW_AT_EACH_SETTLEMENT_WHILE_OPEN",
        "mfe_mae_quote_side": "BID_FOR_LONG_AND_ASK_FOR_SHORT",
        "gap_rule": "FILL_AT_FIRST_EXECUTABLE_QUOTE_PLUS_ADVERSE_SLIPPAGE_NOT_AT_STOP_OR_TARGET",
        "missing_exit_quote_action": "DATA_UNAVAILABLE_KEEP_OUTCOME_UNRESOLVED",
    },
    "strategy_normalized_model": {
        "risk_unit": "1R_PER_ACCEPTED_TRADE",
        "capital_constraint": "NONE",
        "purpose": "COMPARE_SIGNAL_AND_GEOMETRY_QUALITY",
    },
    "executable_portfolio_model": {
        "max_open_positions": 10,
        "capacity_applies_to_all_policies": [
            "CONTROL_ON_SHARED_ACTIVE_CANDIDATES_V1", "ACTIVE_ALTCOIN_LONG_V1",
            "MOMENTUM_REPRICED_V1", "TOP_DOWN_V1",
        ],
        "capacity_precedence": (
            "COMMON_EXPERIMENTAL_MAX_10_GOVERNS_ALL_PRIMARY_PAIRED_COMPARISONS;"
            "POLICY_NATIVE_OR_LEGACY_CAPS_ARE_METADATA_ONLY"
        ),
        "leverage": 5,
        "target_initial_margin_pct_of_equity": 1.0,
        "base_stop_risk_pct_of_equity": 0.25,
        "hard_max_stop_risk_pct_of_equity": 1.0,
        "risk_cap_source": "RESOLVED_RUNTIME_CONFIG_AND_CURRENT_RISK_MANAGER_SOURCE_LOCK",
        "risk_semantics": (
            "1.0_PERCENT_IS_TARGET_INITIAL_MARGIN;0.25_PERCENT_IS_BASE_STOP_RISK;"
            "1.0_PERCENT_HARD_MAX_IS_A_CEILING_NOT_THE_DEFAULT"
        ),
        "runtime_override_freeze_rule": (
            "EFFECTIVE_STATE_RISK_MUST_EQUAL_0.25_PERCENT_AT_SHADOW_START;"
            "ANY_OTHER_TELEGRAM_OR_STATE_OVERRIDE_REQUIRES_NEW_POLICY_VERSION"
        ),
        "sizing_source": "strategy/risk_manager.py::calculate_position_size",
        "sizing_formula": (
            "notional=min(equity*0.01*5,available_balance*0.95*5,equity*5,"
            "equity*0.30,equity*0.0025/risk_distance*entry_price)"
        ),
        "minimum_stop_distance_pct_for_sizing": 0.30,
        "uses_available_balance": True,
        "requires_exchange_filters": [
            "tick_size", "step_size", "min_qty", "min_notional",
        ],
        "rejection_if_capacity_or_filters_fail": True,
        "orders_sent": False,
    },
    "metrics": [
        "opportunities_seen", "rejected", "entry_ready", "simulated_fills",
        "tp_count", "sl_count", "time_exit_count", "net_R", "profit_factor",
        "expectancy_R", "MFE_R", "MAE_R", "trade_sequence_drawdown_R",
        "portfolio_equity_drawdown", "max_concurrent_exposure", "actual_RR",
        "entry_efficiency", "missed_move",
    ],
    "breakdowns": ["side", "symbol", "market_regime"],
    "checkpoints": {
        "independent_theses": [25, 50, 100],
        "diagnostic_only": [25, 50],
        "edge_claim_allowed_at": None,
        "final_analysis": "TIME_OR_BLOCK_BOOTSTRAP_WITH_ALTCOIN_CORRELATION",
    },
    "mutation_rule": (
        "ANY_LOGIC_OR_THRESHOLD_CHANGE_REQUIRES_NEW_POLICY_VERSION_AND_SEPARATE_SAMPLE"
    ),
}


ACTIVE_CANDIDATE_CONTRACT = {
    "universe": "BINANCE_USDM_HOT_LIST_ACTIVE_EVENT_CANDIDATES",
    "required_tags_any": ["ACTIVE_EVENT_PREFILTER", "ACTIVE_EVENT_WATCH"],
    "exclude_majors": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    ],
    "side": "LONG_ONLY",
    "event_timeframe": "5m_CLOSED",
    "min_abs_5m_pct": 1.0,
    "min_abs_20m_pct": 2.0,
    "same_direction_5m_20m": True,
    "min_volume_ratio": 1.5,
    "min_quote_volume_5m": 250000.0,
    "min_body_fraction": 0.50,
    "directional_close_boundary": 0.65,
    "minimum_event_score": 8.0,
    "max_flow_price_agreement": 0.24334596546967147,
    "cross_section_max_per_5m_close": 2,
    "symbol_cooldown_minutes": 60,
    "candidate_limit_per_poll": 50,
}


SHARED_EXECUTION_MECHANICS = {
    "entry_quote": "FIRST_NON_STALE_EXECUTABLE_QUOTE_AFTER_ENTRY_READY",
    "maximum_entry_quote_wait_seconds": 5,
    "first_eligible_quote_rule": "QUOTE_TIMESTAMP_GTE_ENTRY_READY_AT_AND_AGE_LTE_2_SECONDS",
    "long_fill": "BEST_ASK",
    "old_entry_stop_target_reuse_forbidden": True,
    "atr_source": "5m_ATR14_LAST_CLOSED_AT_DECISION",
    "level_source": {
        "all_levels_must_exist_before_decision": True,
        "timeframes": ["5m", "15m", "1h", "4h"],
        "confirmed_pivot": "2_LEFT_AND_2_RIGHT_FULLY_CLOSED_BARS",
        "previous_24h_high_low": True,
    },
    "long_stop": {
        "eligible_structural_levels": [
            "event_low_minus_0.10_ATR5m",
            "near_support_minus_0.10_ATR5m",
            "demand_lower_minus_0.10_ATR5m",
            "liquidity_below_minus_0.10_ATR5m",
        ],
        "selection": "HIGHEST_LEVEL_BELOW_ENTRY_WITH_VALID_RISK_DISTANCE",
        "minimum_distance_atr5m": 0.50,
        "maximum_distance_atr5m": 2.00,
        "missing_valid_level_action": "SKIP_NO_CAUSAL_STOP",
    },
    "long_target": {
        "eligible_opposing_levels": [
            "near_resistance", "supply_lower", "liquidity_above", "previous_24h_high",
        ],
        "selection": "NEAREST_PRE_DECISION_LEVEL_STRICTLY_ABOVE_EXECUTABLE_ENTRY",
        "arbitrary_rr_target_forbidden": True,
        "missing_level_action": "SKIP_NO_CAUSAL_TARGET",
    },
    "minimum_actual_rr": 1.50,
    "rr_below_minimum_action": "SKIP_REPRICED_RR",
    "target_may_be_moved_to_force_rr": False,
    "holding_horizon_minutes": 60,
    "exit_execution": {
        "tp_trigger": "FIRST_NON_STALE_BEST_BID_GTE_TARGET",
        "sl_trigger": "FIRST_NON_STALE_BEST_BID_LTE_STOP",
        "time_exit_trigger": "FIRST_NON_STALE_QUOTE_AT_OR_AFTER_ENTRY_TIME_PLUS_60_MINUTES",
        "tp_exit_quote": "BEST_BID",
        "sl_exit_quote": "BEST_BID",
        "time_exit_quote": "BEST_BID",
        "price_and_cost_model": "COMMON_CONTRACT.execution_cost_model",
        "same_timestamp_priority": "QUOTE_SEQUENCE_IF_RECORDED_ELSE_CAUSAL_AMBIGUOUS",
    },
    "round_trip_cost_bps_excluding_spread": 12.0,
    "spread_is_additional_implicit_cost": True,
    "generic_cost_subtraction_after_fill": False,
    "required_geometry_audit": [
        "candidate_emitted_at", "entry_ready_at", "old_reference_entry",
        "executable_entry", "stop", "stop_source",
        "stop_level_id", "target", "target_source", "target_level_id",
        "actual_RR", "geometry_change_reason",
    ],
}


B_ENTRY_READY_RULE = {
    "source": "INITIAL_PAIRED_ACTIVE_EVENT_DECISION",
    "candidate_emitted_at": "INITIAL_PAIRED_ACTIVE_EVENT_DECISION_TIME_UTC",
    "entry_ready_at": "EXACTLY_EQUAL_TO_CANDIDATE_EMITTED_AT",
    "wait_for_a_pullback_or_reclaim": False,
    "wait_for_top_down_state_machine": False,
}


C_ENTRY_READY_RULE = {
    "source": "TOP_DOWN_STATE_MACHINE_TRANSITION",
    "candidate_emitted_at": "INITIAL_PAIRED_ACTIVE_EVENT_DECISION_TIME_UTC",
    "entry_ready_at": "TIMESTAMP_OF_WAIT_FOR_TRIGGER_TO_ENTRY_READY_TRANSITION",
    "wait_for_a_pullback_or_reclaim": False,
    "wait_for_top_down_state_machine": True,
    "required_transition": "WAIT_FOR_TRIGGER->ENTRY_READY:15M_TRIGGER_RULE",
    "entry_before_required_transition_forbidden": True,
}


TOP_DOWN_DECISION = {
    "side": "LONG_ONLY",
    "principle": "LOCATION_AND_SIMPLE_TRIGGER_NOT_CONFIRMATION_STACKING",
    "indicators": ["EMA20", "EMA50", "ATR14", "CONFIRMED_PIVOTS"],
    "level_map": {
        "required_keys": [
            "main_support", "near_support", "main_resistance", "near_resistance",
            "demand", "supply", "liquidity_above", "liquidity_below", "invalidation",
        ],
        "nullable_when_no_causal_level": True,
        "pivot_confirmation": "2_LEFT_AND_2_RIGHT_FULLY_CLOSED_BARS",
        "near_support": "HIGHEST_CONFIRMED_PIVOT_LOW_BELOW_PRICE",
        "main_support": "NEXT_CONFIRMED_PIVOT_LOW_BELOW_NEAR_SUPPORT_OR_SAME",
        "near_resistance": "LOWEST_CONFIRMED_PIVOT_HIGH_ABOVE_PRICE",
        "main_resistance": "NEXT_CONFIRMED_PIVOT_HIGH_ABOVE_NEAR_RESISTANCE_OR_SAME",
        "demand": "NEAREST_4H_PIVOT_LOW_CANDLE_LOW_TO_BODY_HIGH",
        "supply": "NEAREST_4H_PIVOT_HIGH_CANDLE_BODY_LOW_TO_HIGH",
        "liquidity_cluster_tolerance_atr4h": 0.15,
        "invalidation": "LOWER_OF_NEAR_SUPPORT_AND_DEMAND_LOWER_BOUND",
    },
    "timeframes": {
        "1w_bias": {
            "minimum_closed_bars": 24,
            "long_rule": "CLOSE_ABOVE_EMA20_AND_EMA20_RISING_OVER_3_CLOSED_BARS",
        },
        "1d_regime": {
            "minimum_closed_bars": 60,
            "long_rule": "CLOSE_ABOVE_EMA20_ABOVE_EMA50",
        },
        "4h_location": {
            "minimum_closed_bars": 80,
            "structure_rule": "LAST_2_CONFIRMED_PIVOT_HIGHS_AND_LOWS_ARE_RISING",
            "too_extended_rule": "DISTANCE_FROM_NEAR_SUPPORT_GT_1.50_ATR4H",
        },
        "1h_setup": {
            "minimum_closed_bars": 80,
            "zone_tolerance": "MAX_0.25_ATR1H_OR_0.20_PERCENT_PRICE",
            "pullback_rule": "LOW_TOUCHES_NEAR_SUPPORT_OR_DEMAND_AND_CLOSE_ABOVE_INVALIDATION",
            "breakout_rule": "CLOSED_1H_ABOVE_NEAR_RESISTANCE_THEN_RETEST_REQUIRED",
        },
        "15m_trigger": {
            "minimum_closed_bars": 80,
            "rule": "FIRST_BULLISH_CLOSE_ABOVE_PREVIOUS_CLOSED_15M_HIGH_AFTER_AT_ZONE_OR_RETEST",
            "volume_hard_gate": False,
        },
        "5m": {
            "hard_gate": False,
            "purpose": "OBSERVATION_ONLY_IN_V1",
        },
    },
    "state_machine": {
        "states": [
            "WATCHLIST", "WAIT_FOR_PULLBACK", "AT_ZONE", "WAIT_FOR_TRIGGER",
            "ENTRY_READY", "TOO_EXTENDED", "INVALIDATED", "BREAKOUT_WAIT_RETEST",
            "BIAS_LOST", "EXPIRED", "DATA_UNAVAILABLE",
        ],
        "initial": "WATCHLIST",
        "transitions": [
            "WATCHLIST->DATA_UNAVAILABLE:NEAR_SUPPORT_AND_DEMAND_BOTH_NULL_OR_INVALIDATION_NULL",
            "WATCHLIST->WAIT_FOR_PULLBACK:1W_AND_1D_LONG_AND_4H_STRUCTURE_VALID_AND_CAUSAL_LOCATION_EXISTS",
            "WAIT_FOR_PULLBACK->AT_ZONE:1H_PULLBACK_RULE",
            "AT_ZONE->WAIT_FOR_TRIGGER:LOCATION_REMAINS_VALID",
            "AT_ZONE->WAIT_FOR_PULLBACK:LOCATION_NO_LONGER_WITHIN_ZONE_AND_NOT_INVALIDATED",
            "WAIT_FOR_TRIGGER->WAIT_FOR_PULLBACK:LOCATION_NO_LONGER_VALID_AND_NOT_INVALIDATED",
            "WAIT_FOR_TRIGGER->ENTRY_READY:15M_TRIGGER_RULE",
            "WAIT_FOR_PULLBACK->BREAKOUT_WAIT_RETEST:NEW_CLOSED_1H_BREAKOUT_CROSS_RECORDED",
            "BREAKOUT_WAIT_RETEST->WAIT_FOR_TRIGGER:LATER_CLOSED_1H_RETESTS_BREAKOUT_LEVEL_AND_CLOSES_BACK_ABOVE_IT",
            "BREAKOUT_WAIT_RETEST->EXPIRED:FOUR_LATER_CLOSED_1H_BARS_WITHOUT_VALID_RETEST",
            "ANY->TOO_EXTENDED:4H_TOO_EXTENDED_RULE_UNLESS_BREAKOUT_RETEST_ACTIVE",
            "TOO_EXTENDED->WAIT_FOR_PULLBACK:DISTANCE_FROM_NEAR_SUPPORT_LTE_1.25_ATR4H_AND_BIAS_STRUCTURE_LOCATION_VALID",
            "ANY_ACTIVE->BIAS_LOST:NEW_CLOSED_1W_OR_1D_BAR_FAILS_LONG_RULE",
            "ANY->INVALIDATED:CLOSED_4H_BELOW_INVALIDATION",
        ],
        "transition_priority": [
            "DATA_UNAVAILABLE", "INVALIDATED", "BIAS_LOST", "EXPIRED",
            "ENTRY_READY", "BREAKOUT_WAIT_RETEST", "TOO_EXTENDED",
            "WAIT_FOR_TRIGGER", "AT_ZONE", "WAIT_FOR_PULLBACK",
        ],
        "active_states": [
            "WATCHLIST", "WAIT_FOR_PULLBACK", "AT_ZONE", "WAIT_FOR_TRIGGER",
            "TOO_EXTENDED", "BREAKOUT_WAIT_RETEST",
        ],
        "terminal_states": [
            "ENTRY_READY", "INVALIDATED", "BIAS_LOST", "EXPIRED", "DATA_UNAVAILABLE",
        ],
        "too_extended": {
            "enter_above_atr4h": 1.50,
            "recover_at_or_below_atr4h": 1.25,
            "recovery_target_state": "WAIT_FOR_PULLBACK",
            "recovery_requires_bias_structure_location_valid": True,
        },
        "bias_loss": {
            "evaluation_time": "ONLY_ON_NEW_FULLY_CLOSED_1W_OR_1D_BAR",
            "action": "TERMINATE_THESIS_AS_BIAS_LOST",
            "resume_old_thesis": False,
            "restart": "FRESH_CANDIDATE_AFTER_BIAS_RESTORED_WITH_NEW_THESIS_ID",
        },
        "breakout": {
            "detection": (
                "NEW_CLOSED_1H_CLOSE_GT_NEAR_RESISTANCE_AND_PREVIOUS_CLOSED_1H_CLOSE_LTE_NEAR_RESISTANCE"
            ),
            "record_fields": ["breakout_detected_at", "breakout_level_id", "breakout_level"],
            "same_bar_retest_allowed": False,
            "retest": (
                "LATER_CLOSED_1H_LOW_LTE_BREAKOUT_LEVEL_PLUS_ZONE_TOLERANCE_AND_"
                "CLOSE_GTE_BREAKOUT_LEVEL_AND_CLOSE_GT_INVALIDATION"
            ),
            "maximum_later_closed_1h_bars": 4,
            "expiry_action": "TERMINATE_AS_EXPIRED_NEW_CANDIDATE_REQUIRED",
        },
        "null_level_rules": {
            "near_support_and_demand_both_null": "DATA_UNAVAILABLE_NO_THESIS",
            "invalidation_null": "DATA_UNAVAILABLE_NO_THESIS",
            "one_of_near_support_or_demand_null": "USE_THE_OTHER_CAUSAL_LOCATION",
            "near_resistance_null": "DISABLE_BREAKOUT_BRANCH_PULLBACK_BRANCH_REMAINS_ELIGIBLE",
            "all_opposing_targets_null_at_entry": "SKIP_NO_CAUSAL_TARGET",
        },
        "after_invalidation": {
            "resume_old_thesis": False,
            "minimum_restart_condition": (
                "NEW_CLOSED_4H_BAR_AFTER_INVALIDATION_REBUILDS_VALID_LEVEL_MAP_AND_"
                "FRESH_ACTIVE_EVENT_CANDIDATE_CREATES_NEW_THESIS_ID"
            ),
        },
        "thesis_rebuild_each_scan": False,
        "level_map_refresh": "ONLY_ON_NEW_CLOSED_4H_BAR_OR_INVALIDATION",
    },
}


def _policy(policy_id: str, version: str, role: str, config: dict) -> dict:
    bound_config = deepcopy(config)
    bound_config["common_contract_hash"] = canonical_hash(COMMON_CONTRACT)
    return {
        "policy_id": policy_id,
        "policy_version": version,
        "role": role,
        "execution_authority": "NONE",
        "frozen_config": bound_config,
        "config_hash": canonical_hash(bound_config),
    }


def build_freeze_spec(root: Path) -> dict:
    control_sources = {
        path: FROZEN_SOURCE_SHA256[path] for path in CONTROL_SOURCE_FILES
    }
    a_sources = {path: FROZEN_SOURCE_SHA256[path] for path in A_SOURCE_FILES}
    active_contract_hash = canonical_hash(ACTIVE_CANDIDATE_CONTRACT)
    shared_execution_mechanics_hash = canonical_hash(SHARED_EXECUTION_MECHANICS)
    runtime_config_evidence = deepcopy(RESOLVED_RUNTIME_CONFIG)
    runtime_config_evidence["source_evidence_sha256"] = {
        path: FROZEN_SOURCE_SHA256[path]
        for path in (
            "core/config.py", "strategy/risk_manager.py", "main.py", "utils/state_manager.py",
        )
    }
    runtime_config_evidence["sanitized_production_snapshot_hash"] = canonical_hash(
        runtime_config_evidence["production_environment_snapshot"]
    )

    policies = [
        _policy(
            "CONTROL_ON_SHARED_ACTIVE_CANDIDATES_V1", "1.3.0", "PAIRED_DIAGNOSTIC_BASELINE",
            {
                "source_lock": control_sources,
                "candidate_and_direction": "SHARED_ACTIVE_ALTCOIN_LONG_CANDIDATES_ONLY",
                "baseline_scope": "CONDITIONAL_ON_SHARED_ACTIVE_CANDIDATE_POPULATION",
                "is_full_native_production_baseline": False,
                "reporting_guard": "MUST_NOT_BE_REPORTED_AS_FULL_PRODUCTION_CONTROL",
                "filter_profile": "smc_strict",
                "filter_profile_values": {
                    "require_ob": True, "require_sweep": True, "require_bos": True,
                    "max_ob_age": 80, "enable_fallback": False,
                    "require_volume": True, "require_entry_sweep": True,
                    "trigger_evidence_min": 2, "allow_countertrend_reversal": False,
                    "allow_rr_fallback": False, "min_rr": 1.5, "min_atr_pct": 0.007,
                },
                "derivatives_filter_mode": "enforce",
                "entry_lifecycle": "CURRENT_OPTIMAL_ENTRY_SNIPER_AND_CLOSED_5M_CONFIRMATION",
                "entry_confirmation_timeframe": "5m",
                "strict_virtual_order_max_age_minutes": 30,
                "runtime_config_snapshot_required": True,
                "runtime_config_change_requires_new_version": True,
                "shadow_adapter": "MIRROR_DECISIONS_AND_GEOMETRY_WITHOUT_CALLING_EXECUTION",
                "experimental_portfolio_cap": 10,
            },
        ),
        _policy(
            "ACTIVE_ALTCOIN_LONG_V1", "1.3.0", "SELECTION_DIRECTION_HYPOTHESIS",
            {
                "source_lock": a_sources,
                "candidate_contract": deepcopy(ACTIVE_CANDIDATE_CONTRACT),
                "candidate_contract_hash": active_contract_hash,
                "entry_model": {
                    "states": ["WAITING_PULLBACK", "SWEEP_PENDING", "RECLAIM_CONFIRMED"],
                    "pullback_atr": 0.25,
                    "reclaim_distance_atr": 0.10,
                    "hard_invalidation_atr": 1.50,
                    "pullback_wait_5m_bars": 6,
                    "fill": "PROSPECTIVELY_CAPTURED_EXACT_NEXT_1M_OPEN_AFTER_CONFIRMED_RECLAIM",
                    "historical_open_lookup_forbidden": True,
                    "missing_boundary_event_action": "DATA_UNAVAILABLE_NO_FILL",
                },
                "geometry": {
                    "stop": "PULLBACK_EXTREME_MINUS_0.10_ATR_FOR_LONG",
                    "minimum_risk_atr": 0.50,
                    "maximum_risk_atr": 2.00,
                    "target_r": 1.50,
                    "holding_horizon_minutes": 60,
                    "execution_cost_model": "TAKER_QUOTES_5_PLUS_1_BPS_PER_SIDE_V1",
                    "round_trip_cost_bps_excluding_spread": 12.0,
                    "spread_is_embedded_in_executable_quotes": True,
                },
                "native_policy_metadata": {
                    "legacy_portfolio_cap": 5,
                    "governs_primary_experimental_portfolio": False,
                    "report_separately_if_native_policy_view_is_requested": True,
                },
                "experimental_portfolio_cap": 10,
            },
        ),
        _policy(
            "MOMENTUM_REPRICED_V1", "1.3.0", "ENTRY_GEOMETRY_HYPOTHESIS",
            {
                "candidate_contract": deepcopy(ACTIVE_CANDIDATE_CONTRACT),
                "candidate_contract_hash": active_contract_hash,
                "paired_with_policy": "ACTIVE_ALTCOIN_LONG_V1",
                "pairing_rule": "SAME_CANDIDATE_ID_AND_DIRECTION_BEFORE_ENTRY_MODEL",
                "entry_ready_rule": deepcopy(B_ENTRY_READY_RULE),
                "entry_ready_rule_hash": canonical_hash(B_ENTRY_READY_RULE),
                "shared_execution_mechanics": deepcopy(SHARED_EXECUTION_MECHANICS),
                "shared_execution_mechanics_hash": shared_execution_mechanics_hash,
                "experimental_portfolio_cap": 10,
            },
        ),
        _policy(
            "TOP_DOWN_V1", "1.3.0", "THESIS_LOCATION_HYPOTHESIS",
            {
                "shared_universe": ACTIVE_CANDIDATE_CONTRACT["universe"],
                "decision_contract": deepcopy(TOP_DOWN_DECISION),
                "entry_ready_rule": deepcopy(C_ENTRY_READY_RULE),
                "entry_ready_rule_hash": canonical_hash(C_ENTRY_READY_RULE),
                "shared_execution_mechanics": deepcopy(SHARED_EXECUTION_MECHANICS),
                "shared_execution_mechanics_hash": shared_execution_mechanics_hash,
                "paired_with_policy": "MOMENTUM_REPRICED_V1",
                "comparison_rule": (
                    "C_MINUS_B_MEASURES_DECISION_AND_LOCATION_INCLUDING_POLICY_SPECIFIC_"
                    "ENTRY_READY_TIMING;EXECUTION_MECHANICS_AFTER_ENTRY_READY_ARE_IDENTICAL"
                ),
                "experimental_portfolio_cap": 10,
            },
        ),
    ]
    payload = {
        "schema_version": 3,
        "experiment_id": EXPERIMENT_ID,
        "freeze_id": FREEZE_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "identity": {
            "policy_freeze_commit": POLICY_FREEZE_COMMIT,
            "runtime_commit_at_freeze": RUNTIME_COMMIT_AT_FREEZE,
            "runtime_build_hash_at_freeze": None,
            "runtime_build_hash_algorithm": (
                "SHA256_OF_CANONICAL_SORTED_RUNTIME_SOURCE_AND_DEPENDENCY_MANIFEST"
            ),
        },
        "resolved_runtime_config": runtime_config_evidence,
        "implementation_status": "SPECIFICATION_FROZEN_RUNTIME_NOT_IMPLEMENTED",
        "common_contract": deepcopy(COMMON_CONTRACT),
        "policies": policies,
    }
    payload["freeze_hash"] = canonical_hash({
        "freeze_id": payload["freeze_id"],
        "identity": payload["identity"],
        "resolved_runtime_config": payload["resolved_runtime_config"],
        "common_contract": payload["common_contract"],
        "policies": payload["policies"],
    })
    return payload


def validate_freeze_spec(spec: dict) -> list[str]:
    errors: list[str] = []
    if spec.get("experiment_id") != EXPERIMENT_ID:
        errors.append("EXPERIMENT_ID_MISMATCH")
    if spec.get("implementation_status") != "SPECIFICATION_FROZEN_RUNTIME_NOT_IMPLEMENTED":
        errors.append("IMPLEMENTATION_STATUS_MISMATCH")
    identity = spec.get("identity") or {}
    if identity.get("policy_freeze_commit") != POLICY_FREEZE_COMMIT:
        errors.append("POLICY_FREEZE_COMMIT_MISMATCH")
    if identity.get("runtime_commit_at_freeze") != "NOT_IMPLEMENTED":
        errors.append("RUNTIME_MUST_NOT_EXIST_AT_FREEZE")
    portfolio = (spec.get("common_contract") or {}).get("executable_portfolio_model") or {}
    if portfolio.get("target_initial_margin_pct_of_equity") != 1.0:
        errors.append("ENTRY_MARGIN_NOT_ONE_PERCENT")
    if portfolio.get("base_stop_risk_pct_of_equity") != 0.25:
        errors.append("BASE_STOP_RISK_NOT_QUARTER_PERCENT")
    if portfolio.get("hard_max_stop_risk_pct_of_equity") != 1.0:
        errors.append("HARD_MAX_STOP_RISK_NOT_ONE_PERCENT")
    policies = spec.get("policies") or []
    if len(policies) != 4:
        errors.append("POLICY_COUNT_NOT_FOUR")
    ids = [row.get("policy_id") for row in policies]
    if len(set(ids)) != len(ids):
        errors.append("DUPLICATE_POLICY_ID")
    by_id = {row.get("policy_id"): row for row in policies}
    for policy in policies:
        if policy.get("execution_authority") != "NONE":
            errors.append(f"EXECUTION_AUTHORITY:{policy.get('policy_id')}")
        expected = canonical_hash(policy.get("frozen_config"))
        if policy.get("config_hash") != expected:
            errors.append(f"CONFIG_HASH_MISMATCH:{policy.get('policy_id')}")
    a = by_id.get("ACTIVE_ALTCOIN_LONG_V1", {}).get("frozen_config", {})
    b = by_id.get("MOMENTUM_REPRICED_V1", {}).get("frozen_config", {})
    c = by_id.get("TOP_DOWN_V1", {}).get("frozen_config", {})
    if a.get("candidate_contract_hash") != b.get("candidate_contract_hash"):
        errors.append("A_B_CANDIDATE_POPULATION_NOT_PAIRED")
    if b.get("shared_execution_mechanics_hash") != c.get("shared_execution_mechanics_hash"):
        errors.append("B_C_EXECUTION_MECHANICS_NOT_IDENTICAL")
    if b.get("shared_execution_mechanics") != c.get("shared_execution_mechanics"):
        errors.append("B_C_EXECUTION_MECHANICS_CONTENT_NOT_IDENTICAL")
    b_timing = b.get("entry_ready_rule") or {}
    c_timing = c.get("entry_ready_rule") or {}
    if b_timing.get("entry_ready_at") != "EXACTLY_EQUAL_TO_CANDIDATE_EMITTED_AT":
        errors.append("B_ENTRY_READY_NOT_ANCHORED_TO_INITIAL_EVENT")
    if b_timing.get("wait_for_a_pullback_or_reclaim") is not False:
        errors.append("B_INCORRECTLY_WAITS_FOR_A")
    if c_timing.get("entry_ready_at") != (
        "TIMESTAMP_OF_WAIT_FOR_TRIGGER_TO_ENTRY_READY_TRANSITION"
    ):
        errors.append("C_ENTRY_READY_NOT_ANCHORED_TO_OWN_STATE_TRANSITION")
    if c_timing.get("wait_for_top_down_state_machine") is not True:
        errors.append("C_BYPASSES_TOP_DOWN_STATE_MACHINE")
    if b.get("entry_ready_rule_hash") == c.get("entry_ready_rule_hash"):
        errors.append("B_C_ENTRY_READY_RULES_MUST_DIFFER")
    cost_model = (spec.get("common_contract") or {}).get("execution_cost_model") or {}
    if cost_model.get("subtract_generic_12_bps_again") is not False:
        errors.append("COST_MODEL_DOUBLE_COUNTS_GENERIC_ALLOWANCE")
    if cost_model.get("spread_accounting") != (
        "EMBEDDED_ONCE_BY_ASK_ENTRY_AND_BID_EXIT_OR_REVERSE_FOR_SHORT"
    ):
        errors.append("SPREAD_ACCOUNTING_NOT_EXPLICIT")
    state = (c.get("decision_contract") or {}).get("state_machine") or {}
    required_terminal_states = {
        "ENTRY_READY", "INVALIDATED", "BIAS_LOST", "EXPIRED", "DATA_UNAVAILABLE",
    }
    if not required_terminal_states.issubset(set(state.get("terminal_states") or [])):
        errors.append("C_TERMINAL_STATES_INCOMPLETE")
    if not (state.get("after_invalidation") or {}).get("resume_old_thesis") is False:
        errors.append("C_INVALIDATED_THESIS_CAN_RESUME")
    common_cap = portfolio.get("max_open_positions")
    governed = set(portfolio.get("capacity_applies_to_all_policies") or [])
    if common_cap != 10 or governed != set(ids):
        errors.append("EXPERIMENTAL_CAP_NOT_COMMON_TEN_FOR_ALL_POLICIES")
    if a.get("experimental_portfolio_cap") != 10:
        errors.append("A_EXPERIMENTAL_CAP_NOT_TEN")
    native_a = a.get("native_policy_metadata") or {}
    if native_a.get("legacy_portfolio_cap") != 5:
        errors.append("A_LEGACY_CAP_METADATA_MISSING")
    if native_a.get("governs_primary_experimental_portfolio") is not False:
        errors.append("A_LEGACY_CAP_CONFOUNDS_PRIMARY_COMPARISON")
    if b.get("experimental_portfolio_cap") != 10 or c.get("experimental_portfolio_cap") != 10:
        errors.append("B_C_EXPERIMENTAL_CAP_NOT_TEN")
    expected_freeze = canonical_hash({
        "freeze_id": spec.get("freeze_id"),
        "identity": spec.get("identity"),
        "resolved_runtime_config": spec.get("resolved_runtime_config"),
        "common_contract": spec.get("common_contract"),
        "policies": policies,
    })
    if spec.get("freeze_hash") != expected_freeze:
        errors.append("FREEZE_HASH_MISMATCH")
    return errors


EVENT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://local.tradingbot/schemas/prospective-shadow-event-v1-r3.json",
    "title": "Prospective Shadow Event v1 freeze r3",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "experiment_id", "freeze_id", "event_id",
        "emitted_at_utc", "identity", "market_snapshot_id", "candidate_id",
        "candidate_emitted_at", "thesis_id", "stage", "status", "market", "decision",
    ],
    "$defs": {
        "hash64": {"type": "string", "pattern": "^[A-F0-9]{64}$"},
        "positiveNumber": {"type": "number", "exclusiveMinimum": 0},
        "geometry": {
            "type": "object", "additionalProperties": False,
            "required": [
                "executable_quote_time", "executable_entry", "entry_quote_side",
                "entry_price_source", "old_reference_entry", "stop", "stop_source",
                "stop_level_id", "target", "target_source", "target_level_id",
                "planned_RR", "actual_RR_at_entry", "geometry_change_reason",
            ],
            "properties": {
                "executable_quote_time": {"type": "string", "format": "date-time"},
                "executable_entry": {"$ref": "#/$defs/positiveNumber"},
                "entry_quote_side": {"enum": ["BEST_ASK", "BEST_BID", "NEXT_1M_OPEN"]},
                "entry_price_source": {"type": "string", "minLength": 3},
                "old_reference_entry": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "stop": {"$ref": "#/$defs/positiveNumber"},
                "stop_source": {"type": "string", "minLength": 3},
                "stop_level_id": {"type": "string", "minLength": 3},
                "target": {"$ref": "#/$defs/positiveNumber"},
                "target_source": {"type": "string", "minLength": 3},
                "target_level_id": {"type": "string", "minLength": 3},
                "planned_RR": {"type": "number", "minimum": 0},
                "actual_RR_at_entry": {"type": "number", "minimum": 0},
                "geometry_change_reason": {"type": "string", "minLength": 3},
            },
        },
        "portfolioSimulation": {
            "type": "object", "additionalProperties": False,
            "required": [
                "equity_before", "available_balance_before", "leverage", "quantity",
                "notional", "initial_margin", "initial_margin_pct_of_equity",
                "planned_stop_risk_amount", "planned_stop_risk_pct_of_equity",
                "base_stop_risk_pct_of_equity", "hard_max_stop_risk_pct_of_equity",
                "max_open_positions", "open_positions_before", "exchange_filters_passed",
                "sizing_formula_id",
            ],
            "properties": {
                "equity_before": {"$ref": "#/$defs/positiveNumber"},
                "available_balance_before": {"type": "number", "minimum": 0},
                "leverage": {"type": "number", "minimum": 1},
                "quantity": {"$ref": "#/$defs/positiveNumber"},
                "notional": {"$ref": "#/$defs/positiveNumber"},
                "initial_margin": {"$ref": "#/$defs/positiveNumber"},
                "initial_margin_pct_of_equity": {"type": "number", "minimum": 0},
                "planned_stop_risk_amount": {"type": "number", "minimum": 0},
                "planned_stop_risk_pct_of_equity": {"type": "number", "minimum": 0, "maximum": 1.0},
                "base_stop_risk_pct_of_equity": {"const": 0.25},
                "hard_max_stop_risk_pct_of_equity": {"const": 1.0},
                "max_open_positions": {"type": "integer", "minimum": 1},
                "open_positions_before": {"type": "integer", "minimum": 0},
                "exchange_filters_passed": {"type": "boolean"},
                "sizing_formula_id": {"type": "string", "minLength": 3},
            },
        },
        "executionCosts": {
            "type": "object", "additionalProperties": False,
            "required": [
                "model_id", "entry_spread_bps", "entry_fee_bps", "entry_slippage_bps",
                "expected_exit_fee_bps", "expected_exit_slippage_bps",
                "spread_embedded_in_quotes", "generic_12_bps_subtracted_again",
            ],
            "properties": {
                "model_id": {"const": "TAKER_QUOTES_5_PLUS_1_BPS_PER_SIDE_V1"},
                "entry_spread_bps": {"type": "number", "minimum": 0},
                "entry_fee_bps": {"const": 5.0},
                "entry_slippage_bps": {"const": 1.0},
                "expected_exit_fee_bps": {"const": 5.0},
                "expected_exit_slippage_bps": {"const": 1.0},
                "actual_exit_spread_bps": {"type": ["number", "null"], "minimum": 0},
                "actual_exit_fee_bps": {"type": ["number", "null"], "minimum": 0},
                "actual_exit_slippage_bps": {"type": ["number", "null"], "minimum": 0},
                "spread_embedded_in_quotes": {"const": True},
                "generic_12_bps_subtracted_again": {"const": False},
            },
        },
        "terminalOutcome": {
            "type": "object", "additionalProperties": False,
            "required": [
                "terminal_status", "exit_time", "exit_price", "exit_price_source",
                "terminal_reason", "gross_R", "net_R", "fees_R", "spread_R",
                "slippage_R", "funding_R", "MFE_R", "MAE_R", "same_bar_conflict",
            ],
            "properties": {
                "terminal_status": {
                    "enum": [
                        "TP", "SL", "TIME_EXIT", "CAUSAL_AMBIGUOUS",
                        "DATA_UNAVAILABLE", "EXPIRED_UNFILLED", "INVALIDATED_UNFILLED",
                    ]
                },
                "exit_time": {"type": "string", "format": "date-time"},
                "exit_price": {"$ref": "#/$defs/positiveNumber"},
                "exit_price_source": {"type": "string", "minLength": 3},
                "terminal_reason": {"type": "string", "minLength": 3},
                "gross_R": {"type": "number"},
                "net_R": {"type": "number"},
                "fees_R": {"type": "number", "minimum": 0},
                "spread_R": {"type": "number", "minimum": 0},
                "slippage_R": {"type": "number", "minimum": 0},
                "funding_R": {"type": "number"},
                "MFE_R": {"type": "number"},
                "MAE_R": {"type": "number"},
                "same_bar_conflict": {"type": "boolean"},
                "ordered_quote_event_id": {"type": ["string", "null"]},
            },
        },
    },
    "properties": {
        "schema_version": {"const": 3},
        "experiment_id": {"const": EXPERIMENT_ID},
        "freeze_id": {"const": FREEZE_ID},
        "event_id": {"type": "string", "minLength": 12},
        "emitted_at_utc": {"type": "string", "format": "date-time"},
        "identity": {
            "type": "object", "additionalProperties": False,
            "required": [
                "policy_id", "policy_version", "policy_freeze_commit", "runtime_commit",
                "runtime_build_hash", "freeze_hash", "policy_config_hash",
            ],
            "properties": {
                "policy_id": {"type": "string", "minLength": 3},
                "policy_version": {"type": "string", "pattern": "^1\\.3\\.0$"},
                "policy_freeze_commit": {"const": POLICY_FREEZE_COMMIT},
                "runtime_commit": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
                "runtime_build_hash": {"$ref": "#/$defs/hash64"},
                "freeze_hash": {"$ref": "#/$defs/hash64"},
                "policy_config_hash": {"$ref": "#/$defs/hash64"},
            },
        },
        "market_snapshot_id": {"type": "string", "minLength": 6},
        "candidate_id": {"type": "string", "minLength": 6},
        "candidate_emitted_at": {"type": "string", "format": "date-time"},
        "entry_ready_at": {"type": "string", "format": "date-time"},
        "thesis_id": {"type": "string", "minLength": 6},
        "stage": {
            "enum": [
                "CANDIDATE_EMITTED", "THESIS_STATE", "ENTRY_READY",
                "SIMULATED_FILL", "TERMINAL_OUTCOME",
            ]
        },
        "status": {"type": "string", "minLength": 3},
        "market": {
            "type": "object", "additionalProperties": False,
            "required": [
                "symbol", "timestamp_utc", "best_bid", "best_ask", "spread",
                "spread_bps", "mark_price", "index_price", "funding_rate",
                "next_funding_time", "closed_candle_ids",
            ],
            "properties": {
                "symbol": {"type": "string", "minLength": 3},
                "timestamp_utc": {"type": "string", "format": "date-time"},
                "best_bid": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "best_ask": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "spread": {"type": ["number", "null"], "minimum": 0},
                "spread_bps": {"type": ["number", "null"], "minimum": 0},
                "mark_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "index_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "funding_rate": {"type": ["number", "null"]},
                "next_funding_time": {"type": ["string", "null"], "format": "date-time"},
                "closed_candle_ids": {
                    "type": "object",
                    "required": ["1w", "1d", "4h", "1h", "15m", "5m", "1m"],
                },
            },
        },
        "decision": {
            "type": "object", "additionalProperties": False,
            "required": ["side", "state_before", "state_after", "reason_codes"],
            "properties": {
                "side": {"enum": ["LONG", "SHORT", "NONE"]},
                "state_before": {"type": ["string", "null"]},
                "state_after": {"type": ["string", "null"]},
                "reason_codes": {
                    "type": "array", "minItems": 1, "items": {"type": "string", "minLength": 2}
                },
            },
        },
        "level_map": {"type": ["object", "null"]},
        "geometry": {"$ref": "#/$defs/geometry"},
        "portfolio_simulation": {"$ref": "#/$defs/portfolioSimulation"},
        "execution_costs": {"$ref": "#/$defs/executionCosts"},
        "outcome": {"$ref": "#/$defs/terminalOutcome"},
        "runtime_evidence": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "execution_quote_event_id": {"type": "string"},
                "execution_quote_observed_at": {"type": "string", "format": "date-time"},
                "execution_quote_received_at": {"type": "string", "format": "date-time"},
                "quote_age_ms_at_receipt": {"type": "number", "minimum": 0},
                "quote_wait_ms": {"type": "number", "minimum": 0},
                "execution_resolution_started_at": {"type": "string", "format": "date-time"},
                "execution_source": {"type": "string"},
                "candle_open_event_id": {"type": "string"},
                "candle_open_received_at": {"type": "string", "format": "date-time"},
            },
        },
    },
    "allOf": [
        {
            "if": {"properties": {"stage": {"const": "ENTRY_READY"}}, "required": ["stage"]},
            "then": {"required": ["entry_ready_at", "geometry"]},
        },
        {
            "if": {"properties": {"stage": {"const": "SIMULATED_FILL"}}, "required": ["stage"]},
            "then": {
                "required": [
                    "entry_ready_at", "geometry", "portfolio_simulation", "execution_costs",
                ]
            },
        },
        {
            "if": {"properties": {"stage": {"const": "TERMINAL_OUTCOME"}}, "required": ["stage"]},
            "then": {
                "required": [
                    "entry_ready_at", "geometry", "portfolio_simulation",
                    "execution_costs", "outcome",
                ],
                "properties": {
                    "execution_costs": {
                        "allOf": [
                            {"$ref": "#/$defs/executionCosts"},
                            {
                                "required": [
                                    "actual_exit_spread_bps", "actual_exit_fee_bps",
                                    "actual_exit_slippage_bps",
                                ],
                                "properties": {
                                    "actual_exit_spread_bps": {"type": "number", "minimum": 0},
                                    "actual_exit_fee_bps": {"const": 5.0},
                                    "actual_exit_slippage_bps": {"const": 1.0},
                                },
                            },
                        ]
                    }
                },
            },
        },
    ],
    "x-runtime-invariants": [
        "B: entry_ready_at MUST equal candidate_emitted_at",
        "C: entry_ready_at MUST equal the recorded WAIT_FOR_TRIGGER->ENTRY_READY transition timestamp",
        "B and C MUST use the same shared_execution_mechanics_hash after ENTRY_READY",
        "All primary portfolio comparisons MUST enforce max_open_positions=10",
    ],
}


def build_event_schema(root: Path) -> dict:
    """Bind the generic stage schema to this exact freeze and policy set."""
    spec = build_freeze_spec(root)
    schema = deepcopy(EVENT_SCHEMA)
    identity = schema["properties"]["identity"]
    identity["properties"]["freeze_hash"] = {"const": spec["freeze_hash"]}
    identity["allOf"] = [
        {
            "if": {
                "properties": {"policy_id": {"const": policy["policy_id"]}},
                "required": ["policy_id"],
            },
            "then": {
                "properties": {
                    "policy_version": {"const": policy["policy_version"]},
                    "policy_config_hash": {"const": policy["config_hash"]},
                }
            },
        }
        for policy in spec["policies"]
    ]
    identity["properties"]["policy_id"] = {
        "enum": [policy["policy_id"] for policy in spec["policies"]]
    }
    return schema
