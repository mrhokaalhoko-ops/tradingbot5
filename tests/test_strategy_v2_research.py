import pytest

from strategy.strategy_v2_research import (
    build_causal_level_map,
    evaluate_causal_opportunity,
    evaluate_thesis_reentry,
    select_thesis_invalidation,
    structural_management_candidate,
)


DECISION = "2026-10-20T00:00:00Z"


def test_target_v2_uses_only_predecision_market_levels_and_never_synthetic_4r():
    levels = {
        "15m": {"swing_highs": [
            {"price": 103.0, "created_at": "2026-10-19T23:30:00Z"},
            {"price": 110.0, "created_at": "2026-10-20T00:05:00Z"},
        ]},
        "4h": {"resistance": [
            {"price": 108.0, "created_at": "2026-10-19T20:00:00Z"},
        ]},
    }
    result = evaluate_causal_opportunity(
        side="buy", entry=100.0, initial_stop=98.0,
        market_levels=levels, decision_time=DECISION, minimum_rr=1.5,
    )
    assert result["valid"] is True
    assert result["synthetic_terminal_target"] is None
    assert result["fixed_final_tp"] is None
    assert result["opportunity_limit"]["price"] == 108.0
    future = next(item for item in result["levels"] if item["price"] == 110.0)
    assert future["causal"] is False
    assert future["causal_reason"] == "FUTURE_LEVEL"


def test_target_v2_rejects_when_market_has_no_causal_room():
    result = evaluate_causal_opportunity(
        side="buy", entry=100.0, initial_stop=98.0,
        market_levels={"15m": {"swing_highs": [
            {"price": 101.0, "created_at": "2026-10-19T23:30:00Z"}
        ]}}, decision_time=DECISION, minimum_rr=1.5,
    )
    assert result["valid"] is False
    assert result["reason"] == "REJECT_NO_CAUSAL_MARKET_ROOM"


def test_causal_map_marks_missing_timestamp_noncausal():
    mapped = build_causal_level_map(
        side="buy", entry=100.0, initial_stop=98.0,
        market_levels={"swing_highs": [{"price": 105.0}]},
        decision_time=DECISION, target=True,
    )
    assert mapped[0].causal is False
    assert mapped[0].causal_reason == "MISSING_LEVEL_TIMESTAMP"


def test_stop_v2_separates_entry_anchor_from_thesis_invalidation():
    result = select_thesis_invalidation(
        side="buy", entry=100.0,
        setup_anchor={"type": "bullish_ob", "low": 99.0,
                      "created_at": "2026-10-19T23:00:00Z"},
        explicit_invalidation=99.0,
        market_levels={"4h": {"swing_lows": [
            {"price": 95.0, "created_at": "2026-10-19T20:00:00Z",
             "is_thesis_invalidation": True}
        ]}}, decision_time=DECISION,
    )
    assert result["valid"] is True
    assert result["entry_anchor"] == 99.0
    assert result["selected"]["price"] == 95.0
    assert result["selected"]["type"] == "THESIS_INVALIDATION"


def test_stop_v2_fails_closed_when_only_anchor_alias_exists():
    result = select_thesis_invalidation(
        side="buy", entry=100.0,
        setup_anchor={"type": "bullish_ob", "low": 99.0,
                      "created_at": "2026-10-19T23:00:00Z"},
        explicit_invalidation=99.0, market_levels={}, decision_time=DECISION,
    )
    assert result["valid"] is False
    assert result["reason"] == "NO_VALID_THESIS_INVALIDATION"


def test_same_thesis_reentry_rejected_even_after_time_without_reset():
    verdict = evaluate_thesis_reentry(
        old_thesis_id="same", new_thesis_id="same",
        prior_closed_at="2026-10-19T20:00:00Z",
        reset_evidence=[], decision_time=DECISION,
    )
    assert verdict["allow"] is False
    assert verdict["reason"] == "REJECT_SAME_THESIS_REENTRY"


def test_new_causal_bos_after_close_allows_reentry():
    verdict = evaluate_thesis_reentry(
        old_thesis_id="old", new_thesis_id="new",
        prior_closed_at="2026-10-19T20:00:00Z",
        reset_evidence=[{"event": "NEW_BOS", "timestamp": "2026-10-19T22:00:00Z"}],
        decision_time=DECISION,
    )
    assert verdict["allow"] is True
    assert verdict["reset_event"] == "NEW_BOS"


def test_preclose_or_future_reset_evidence_is_rejected():
    verdict = evaluate_thesis_reentry(
        old_thesis_id="old", new_thesis_id="new",
        prior_closed_at="2026-10-19T20:00:00Z",
        reset_evidence=[
            {"event": "NEW_BOS", "timestamp": "2026-10-19T19:00:00Z"},
            {"event": "NEW_CHOCH", "timestamp": "2026-10-20T00:05:00Z"},
        ], decision_time=DECISION,
    )
    assert verdict["allow"] is False
    assert len(verdict["rejected_evidence"]) == 2


def test_management_v2_moves_only_for_new_confirmed_structure():
    move = structural_management_candidate(
        side="buy", entry=100.0, current_stop=95.0, initial_risk=5.0,
        best_r=2.0, activation_r=1.5, atr=1.0, tick_size=0.1,
        confirmed_structure={"price": 104.0,
                             "confirmed_at": "2026-10-19T23:45:00Z",
                             "event": "CONFIRMED_SWING_LOW"},
        last_structure_at="2026-10-19T23:30:00Z",
    )
    assert move["move"] is True
    assert move["new_stop"] == pytest.approx(103.7)
    assert move["reason"] == "NEW_CONFIRMED_STRUCTURE_PROGRESS"

    duplicate = structural_management_candidate(
        side="buy", entry=100.0, current_stop=103.7, initial_risk=5.0,
        best_r=2.2, activation_r=1.5, atr=1.0, tick_size=0.1,
        confirmed_structure={"price": 104.0,
                             "confirmed_at": "2026-10-19T23:45:00Z"},
        last_structure_at="2026-10-19T23:45:00Z",
    )
    assert duplicate["move"] is False
    assert duplicate["reason"] == "NO_NEW_MARKET_INFORMATION"
