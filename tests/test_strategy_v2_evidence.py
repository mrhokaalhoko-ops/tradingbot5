import copy
import inspect
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd
from core.config import Config
from strategy.strategy_v2_evidence import (
    build_causal_level_snapshot,
    build_management_shadow_record,
    build_shadow_decision_record,
    collect_authoritative_proposal_evidence,
    collect_closed_outcome_evidence,
    collect_post_exit_checkpoint,
)
from strategy.user_visible_trade_proposal import build_user_visible_trade_proposal
from strategy.trailing_stop import TrailingStopManager
from strategy.strategy_v2_shadow_storage import ShadowEvidenceStore


DECISION = "2026-10-20T12:00:00Z"


def proposal(proposal_id="shadow-1"):
    return build_user_visible_trade_proposal(
        proposal_id=proposal_id, symbol="BTC/USDT", side="BUY",
        proposed_entry=100.0, original_stop=95.0, original_target=120.0,
        target_stages=[], thesis_id="thesis-1", setup_id="thesis-1",
        confirmation_bar_id="2026-10-20T11:55:00Z", leverage=5,
        risk_percent=0.25, amount=0.1,
        target_plan={
            "source": "fallback_rr", "rr": 4.0, "min_rr": 1.5,
            "stop_plan": {
                "selected_thesis_invalidation": 94.0,
                "relation_to_thesis": "THESIS_STRUCTURE",
                "structural_level_created_at": "2026-10-20T08:00:00Z",
                "structural_level_confirmed_at": "2026-10-20T09:00:00Z",
                "tick_size": 0.1,
            },
        }, timestamp=DECISION,
    )


def market_levels(*, future=False):
    resistance_time = "2026-10-20T12:05:00Z" if future else "2026-10-20T08:00:00Z"
    return {
        "4h": {
            "resistance": [{
                "price": 115.0, "created_at": resistance_time,
                "confirmed_at": resistance_time, "strength": 4.0,
            }],
            "swing_lows": [{
                "price": 94.0, "created_at": "2026-10-20T04:00:00Z",
                "confirmed_at": "2026-10-20T08:00:00Z",
                "relation_to_thesis": "THESIS_STRUCTURE",
                "is_thesis_invalidation": True,
            }],
        },
        "1h": {"liquidity": [{
            "price": 110.0, "created_at": "2026-10-20T10:00:00Z",
            "confirmed_at": "2026-10-20T11:00:00Z", "touch_count": 2,
        }]},
    }


def ta_context():
    return {
        "setup_anchor": {
            "type": "bullish_ob", "low": 98.0, "high": 99.0,
            "created_at": "2026-10-20T10:00:00Z",
        },
        "bos_created_at": "2026-10-20T11:00:00Z",
        "1h": {"trend": "bullish", "known_at": "2026-10-20T11:00:00Z"},
        "15m": {
            "trigger": "reclaim", "confirmed_at": "2026-10-20T11:45:00Z"
        },
        "5m": {
            "confirmation": "closed", "confirmed_at": "2026-10-20T11:55:00Z"
        },
    }


def test_future_source_invalidates_research_sample_without_hiding_level():
    snapshot = build_causal_level_snapshot(
        market_levels=market_levels(future=True), decision_timestamp=DECISION,
        entry=100.0, side="BUY",
    )
    assert snapshot["lookahead_violation"] is True
    assert snapshot["valid_for_research"] is False
    future = next(item for item in snapshot["levels"] if item["price"] == 115.0)
    assert future["causal_at_decision"] is False
    assert future["causal_reason"] == "LOOKAHEAD_SOURCE_TIMESTAMP"


def test_decision_snapshot_is_causal_has_no_outcome_and_does_not_mutate_v1_inputs():
    item, levels, context = proposal(), market_levels(), ta_context()
    originals = copy.deepcopy((item, levels, context))
    record = build_shadow_decision_record(
        proposal=item, market_levels=levels, ta_context=context,
        reset_evidence=[], prior_closed_thesis=None,
    )
    assert (item, levels, context) == originals
    assert record["execution_authority"] is False
    assert record["data_integrity"]["valid_for_research"] is True
    assert "outcome" not in record
    assert "realized_R" not in record
    assert record["what_v1_did"]["target"] == 120.0
    assert record["what_v2_would_have_done"]["target"]["synthetic_terminal_target"] is None
    assert record["what_v2_would_have_done"]["target"]["v1_target_matches_known_level"] is False


def test_same_thesis_reentry_shadow_rejects_without_post_close_reset():
    record = build_shadow_decision_record(
        proposal=proposal("shadow-2"), market_levels=market_levels(),
        ta_context=ta_context(), reset_evidence=[{
            "event": "NEW_BOS", "timestamp": "2026-10-20T10:00:00Z"
        }],
        prior_closed_thesis={
            "thesis_id": "thesis-1", "closed_at": "2026-10-20T11:30:00Z"
        },
    )
    verdict = record["what_v2_would_have_done"]["reentry"]
    assert verdict["allow"] is False
    assert verdict["reason"] == "REJECT_SAME_THESIS_REENTRY"


def test_shadow_state_and_thesis_survive_restart_and_outcome_is_separate():
    with tempfile.TemporaryDirectory() as directory:
        state = ShadowEvidenceStore(directory)
        record = build_shadow_decision_record(
            proposal=proposal(), market_levels=market_levels(),
            ta_context=ta_context(), reset_evidence=[],
            prior_closed_thesis=None,
        )
        assert state.save_strategy_v2_shadow_decision(record)
        assert state.record_closed_thesis(
            "BTC/USDT", "thesis-1", side="BUY",
            closed_at="2026-10-20T12:30:00Z",
        )
        with patch("utils.decision_audit.audit_decision_event", return_value=True):
            collect_closed_outcome_evidence(
                storage=state, proposal_id="shadow-1",
                trade_id="shadow-1", symbol="BTC/USDT",
                exit_details={
                    "final_r": 1.2, "mfe_r": 2.0, "mae_r": -0.3,
                    "commission": 0.01, "funding": -0.001,
                },
            )
        state.close()
        restarted = ShadowEvidenceStore(directory)
        restored = restarted.get_strategy_v2_shadow_decision("shadow-1")
        assert restored["decision_record_hash"] == record["decision_record_hash"]
        assert restarted.get_latest_closed_thesis("BTC/USDT", "BUY")["thesis_id"] == "thesis-1"
        assert collect_post_exit_checkpoint(
            storage=restarted, trade_id="shadow-1", horizon="15m",
            checkpoint={"post_exit_mfe_r": 2.3},
        )
        assert "realized_R" not in restarted.get_strategy_v2_shadow_decision("shadow-1")
        with restarted._connection() as connection:
            raw = connection.execute(
                "SELECT payload FROM outcomes WHERE proposal_id='shadow-1'"
            ).fetchone()[0]
        import json
        outcome = json.loads(raw)
        assert outcome["realized_R"] == 1.2
        assert outcome["post_exit_MFE"]["15m"]["post_exit_mfe_r"] == 2.3
        restarted.close()


def test_collector_creates_no_order_intent_and_cannot_mutate_proposal():
    with tempfile.TemporaryDirectory() as directory:
        state = ShadowEvidenceStore(directory)
        item = proposal()
        coin_data = {"market_levels": market_levels(), "_ta_context": ta_context()}
        original = copy.deepcopy((item, coin_data))
        with patch("utils.decision_audit.audit_decision_event", return_value=True):
            record = collect_authoritative_proposal_evidence(
                storage=state, proposal=item, coin_data=coin_data,
                reset_evidence=[], audit_ids={"trade_id": "shadow-1"},
            )
        assert (item, coin_data) == original
        assert record["execution_authority"] is False
        assert not hasattr(state, "save_order_intent")
        assert not hasattr(state, "create_order")


def test_management_shadow_is_counterfactual_and_never_changes_v1_stop():
    record = build_management_shadow_record(
        symbol="BTC/USDT", trade_id="shadow-1", side="buy",
        entry=100.0, initial_stop=95.0, current_v1_stop=99.0,
        best_price=110.0, atr=1.0, tick_size=0.1,
        confirmed_structure={
            "structure_id": "swing-1", "price": 104.0,
            "confirmed_at": "2026-10-20T11:45:00Z",
            "event": "CONFIRMED_SWING_LOW",
        }, observed_at=DECISION,
    )
    assert record["execution_authority"] is False
    assert record["v1_stop_unchanged"] is True
    assert record["old_stop"] == 99.0
    assert record["candidate_stop"] > 99.0


def test_future_4h_context_invalidates_entire_decision_record():
    context = ta_context()
    context["4h"] = {
        "trend": "bullish", "known_at": "2026-10-20T12:05:00Z"
    }
    record = build_shadow_decision_record(
        proposal=proposal("future-4h"), market_levels=market_levels(),
        ta_context=context, reset_evidence=[], prior_closed_thesis=None,
    )
    integrity = record["data_integrity"]
    assert integrity["valid_for_research"] is False
    assert integrity["ta_context_integrity"]["lookahead_violation"] is True


def test_future_15m_trigger_invalidates_entire_decision_record():
    context = ta_context()
    context["15m"]["confirmed_at"] = "2026-10-20T12:01:00Z"
    record = build_shadow_decision_record(
        proposal=proposal("future-15m"), market_levels=market_levels(),
        ta_context=context, reset_evidence=[], prior_closed_thesis=None,
    )
    assert record["data_integrity"]["valid_for_research"] is False
    violations = record["data_integrity"]["integrity_violations"]
    assert any(item["label"] == "ta_context.15m" for item in violations)


def test_missing_setup_anchor_timestamp_invalidates_decision_record():
    context = ta_context()
    context["setup_anchor"].pop("created_at")
    record = build_shadow_decision_record(
        proposal=proposal("missing-anchor-time"), market_levels=market_levels(),
        ta_context=context, reset_evidence=[], prior_closed_thesis=None,
    )
    integrity = record["data_integrity"]
    assert integrity["valid_for_research"] is False
    assert integrity["ta_context_integrity"]["missing_source_timestamps"] is True
    assert any(
        item["reason"] == "MISSING_SOURCE_TIMESTAMP"
        for item in integrity["integrity_violations"]
    )


def test_missing_explicit_invalidation_timestamp_invalidates_thesis_evidence():
    item = proposal("missing-invalidation-time")
    plan = item["target_plan"]["stop_plan"]
    plan.pop("structural_level_created_at")
    plan.pop("structural_level_confirmed_at")
    record = build_shadow_decision_record(
        proposal=item, market_levels=market_levels(), ta_context=ta_context(),
        reset_evidence=[], prior_closed_thesis=None,
    )
    thesis_integrity = record["data_integrity"]["thesis_integrity"]
    assert thesis_integrity["valid_for_research"] is False
    assert thesis_integrity["missing_source_timestamps"] is True
    assert record["thesis"]["explicit_thesis_invalidation"] is None


def test_future_reset_event_is_invalid_and_never_accepted():
    record = build_shadow_decision_record(
        proposal=proposal("future-reset"), market_levels=market_levels(),
        ta_context=ta_context(), reset_evidence=[{
            "event": "NEW_BOS", "timestamp": "2026-10-20T12:05:00Z"
        }], prior_closed_thesis={
            "thesis_id": "thesis-1", "closed_at": "2026-10-20T11:30:00Z"
        },
    )
    assert record["data_integrity"]["valid_for_research"] is False
    reset = record["data_integrity"]["reset_integrity"]["evidence"][0]
    assert reset["eligible_reset"] is False
    assert reset["reason"] == "LOOKAHEAD_SOURCE_TIMESTAMP"
    assert record["what_v2_would_have_done"]["reentry"]["allow"] is False


def test_reset_before_prior_close_is_causal_but_not_accepted_as_reset():
    record = build_shadow_decision_record(
        proposal=proposal("old-reset"), market_levels=market_levels(),
        ta_context=ta_context(), reset_evidence=[{
            "event": "NEW_BOS", "timestamp": "2026-10-20T11:00:00Z"
        }], prior_closed_thesis={
            "thesis_id": "thesis-1", "closed_at": "2026-10-20T11:30:00Z"
        },
    )
    reset = record["data_integrity"]["reset_integrity"]["evidence"][0]
    assert reset["causal_at_decision"] is True
    assert reset["eligible_reset"] is False
    assert reset["reset_rejection_reason"] == "NOT_AFTER_PRIOR_CLOSE"
    assert record["what_v2_would_have_done"]["reentry"]["allow"] is False


def test_future_management_structure_is_invalid_and_cannot_move_stop():
    record = build_management_shadow_record(
        symbol="BTC/USDT", trade_id="future-management", side="buy",
        entry=100.0, initial_stop=95.0, current_v1_stop=99.0,
        best_price=110.0, atr=1.0, tick_size=0.1,
        confirmed_structure={
            "structure_id": "swing-future", "price": 104.0,
            "confirmed_at": "2026-10-20T12:05:00Z",
        }, observed_at=DECISION,
    )
    assert record["valid_for_research"] is False
    assert record["shadow_decision"] == "NO_MOVE"
    assert record["candidate_stop"] is None
    assert record["reason"] == "INVALID_CAUSAL_MANAGEMENT_EVIDENCE"


def test_missing_management_structure_timestamp_is_invalid_and_cannot_move_stop():
    record = build_management_shadow_record(
        symbol="BTC/USDT", trade_id="missing-management", side="buy",
        entry=100.0, initial_stop=95.0, current_v1_stop=99.0,
        best_price=110.0, atr=1.0, tick_size=0.1,
        confirmed_structure={"structure_id": "swing-missing", "price": 104.0},
        observed_at=DECISION,
    )
    assert record["valid_for_research"] is False
    assert record["shadow_decision"] == "NO_MOVE"
    assert record["reason"] == "INVALID_CAUSAL_MANAGEMENT_EVIDENCE"


def test_closed_management_structure_before_observation_is_valid():
    record = build_management_shadow_record(
        symbol="BTC/USDT", trade_id="valid-management", side="buy",
        entry=100.0, initial_stop=95.0, current_v1_stop=99.0,
        best_price=110.0, atr=1.0, tick_size=0.1,
        confirmed_structure={
            "structure_id": "swing-valid", "price": 104.0,
            "confirmed_at": "2026-10-20T11:45:00Z",
        }, observed_at=DECISION,
    )
    assert record["valid_for_research"] is True
    assert record["shadow_decision"] == "MEANINGFUL_MOVE"
    assert record["candidate_stop"] > 99.0


def test_future_last_management_structure_is_invalid_and_cannot_move_stop():
    record = build_management_shadow_record(
        symbol="BTC/USDT", trade_id="future-last-structure", side="buy",
        entry=100.0, initial_stop=95.0, current_v1_stop=99.0,
        best_price=110.0, atr=1.0, tick_size=0.1,
        confirmed_structure={
            "structure_id": "swing-valid", "price": 104.0,
            "confirmed_at": "2026-10-20T11:45:00Z",
        }, observed_at=DECISION, last_structure_at="2026-10-20T12:10:00Z",
    )
    assert record["valid_for_research"] is False
    assert record["shadow_decision"] == "NO_MOVE"
    assert record["reason"] == "INVALID_CAUSAL_MANAGEMENT_EVIDENCE"


def test_management_shadow_hook_does_not_mutate_v1_trade_object():
    state = MagicMock()
    state.get_trailing_trades.return_value = {}
    state.get_entry_metadata.return_value = {"trade_id": "shadow-hook"}
    manager = TrailingStopManager(state_manager=state)
    trade = {
        "side": "buy", "entry": 100.0, "initial_sl": 95.0,
        "current_sl": 99.0, "best_price": 110.0,
    }
    original = copy.deepcopy(trade)
    index = pd.date_range("2026-10-20T00:00:00Z", periods=25, freq="15min")
    bars = pd.DataFrame({
        "high": [101 + (i % 4) for i in range(25)],
        "low": [99 - (i % 4) for i in range(25)],
        "close": [100 + (i % 3) for i in range(25)],
    }, index=index)
    client = MagicMock()
    client.fetch_ohlcv.return_value = bars
    client.get_price_tick_size.return_value = 0.1
    with tempfile.TemporaryDirectory() as directory, patch.object(
        Config, "STRATEGY_V2_SHADOW_DIR", directory
    ):
        assert manager._record_v2_management_shadow(
            "BTC/USDT", trade, client=client, best_r=2.0,
        ) is True
        assert trade == original


def test_evidence_module_has_no_exchange_write_surface():
    import strategy.strategy_v2_evidence as module
    source = inspect.getsource(module)
    for forbidden in (
        "create_order(", "cancel_order(", "set_leverage(",
        "save_order_intent(", "replace_sl_order(", "close_position(",
    ):
        assert forbidden not in source
