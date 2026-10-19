from strategy.candidate_scoring import score_candidate


def _complete_candidate():
    return {
        "side": "buy",
        "setup_score": 3,
        "context_4h": {
            "known": True,
            "trend": "BULLISH",
            "extended": False,
            "reason": "4H aligned",
        },
        "context_1h": {
            "known": True,
            "selected_state": "CONFIRMED_BREAKOUT",
        },
        "htf_trend": "bullish",
        "adx": 35,
        "rsi": 55,
        "open": 149.0,
        "price": 150.0,
        "rvol_20": 2.0,
        "quality_audit": {"terminal_gate": "ALL_GATES"},
        "target_plan": {"rr": 2.0, "min_rr": 1.5},
        "entry_confirmation": {"passed": True, "score": 90},
        "derivatives_result": {"decision": "ALLOW", "reason": "ضمن الحدود"},
    }


def test_complete_supported_candidate_scores_high_without_authorizing_execution():
    result = score_candidate(_complete_candidate())

    assert result["score"] == 100.0
    assert result["score_band"] == "HIGH_QUALITY_SETUP"
    assert result["evidence_coverage_pct"] == 100.0
    assert result["execution_authority"] is False


def test_missing_optional_derivatives_is_reported_and_renormalized_not_rejected():
    candidate = _complete_candidate()
    candidate.pop("derivatives_result")

    result = score_candidate(candidate)
    derivatives = next(
        item for item in result["components"]
        if item["name"] == "derivatives_context"
    )

    assert result["score"] == 100.0
    assert result["evidence_coverage_pct"] == 95.0
    assert derivatives["available"] is False
    assert derivatives["points"] is None


def test_hard_safety_veto_cannot_be_hidden_by_a_high_score():
    result = score_candidate(_complete_candidate(), hard_safety_veto=True)

    assert result["score"] == 100.0
    assert result["score_band"] == "SAFETY_VETO"
    assert result["hard_safety_veto"] is True


def test_weights_are_configurable_but_zero_weight_config_falls_back_safely():
    candidate = _complete_candidate()
    custom = score_candidate(candidate, weights={"technical_setup": 100, "momentum": 0})
    fallback = score_candidate(candidate, weights={key: 0 for key in (
        "technical_setup", "context_4h", "htf_alignment", "setup_1h", "momentum", "volume", "entry_quality",
        "trade_geometry", "entry_trigger", "derivatives_context",
    )})

    assert custom["total_weight"] == 180.0
    assert fallback["total_weight"] == 100.0
