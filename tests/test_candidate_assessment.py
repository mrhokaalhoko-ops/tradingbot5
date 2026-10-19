from strategy.candidate_assessment import assess_candidate, enrich_scan_results


def test_low_atr_is_waiting_not_claimed_as_bad_trade():
    result = {
        "coin": "SOL",
        "symbol": "SOL/USDT",
        "side": "buy",
        "decision": "REJECT",
        "rejection_stage": "QUALITY_FILTER",
        "reason": "low atr",
        "quality_audit": {
            "terminal_gate": "MIN_ATR",
            "values": {"atr_pct": 0.00238},
            "thresholds": {"min_atr_pct": 0.007},
        },
    }

    assessment = assess_candidate(result)

    assert assessment["status"] == "WAITING_FOR_VOLATILITY"
    assert assessment["gate_class"] == "SOFT_SETUP"
    assert assessment["pipeline_progress_pct"] == 20.0
    assert "0.24%" in assessment["next_trigger"]
    assert assessment["execution_authority"] is False
    assert assessment["score_kind"] == "DIAGNOSTIC_EXISTING_EVIDENCE_NOT_EXECUTION_PERMISSION"
    assert assessment["conditional_plan"]["targets"] == []
    assert assessment["conditional_plan"]["trade_allowed_by_pipeline"] is False


def test_rr_reject_explains_the_exact_next_requirement():
    assessment = assess_candidate({
        "coin": "SUSHI",
        "side": "buy",
        "decision": "REJECT",
        "rejection_stage": "RR_FILTER",
        "target_plan": {"rr": 0.59, "min_rr": 1.5, "sl": 0.24},
        "price": 0.2537,
    })

    assert assessment["status"] == "WAITING_FOR_BETTER_RR"
    assert assessment["pipeline_progress_pct"] == 40.0
    assert "0.59" in assessment["next_trigger"]
    assert "1.50" in assessment["next_trigger"]
    assert assessment["invalidation_level"] == 0.24


def test_pending_confirmation_is_ranked_ahead_of_earlier_reject():
    rows, lifecycle = enrich_scan_results([
        {
            "coin": "A",
            "symbol": "A/USDT",
            "side": "buy",
            "decision": "REJECT",
            "rejection_stage": "QUALITY_FILTER",
            "quality_audit": {"terminal_gate": "EXTREME_ZONE"},
        },
        {
            "coin": "B",
            "symbol": "B/USDT",
            "side": "sell",
            "decision": "V-LIMIT",
            "entry_confirmation": {"reason": "WAITING_FOR_NEXT_CLOSED_5M_BAR"},
        },
    ], timestamp="2026-09-06T12:00:00+00:00")

    by_symbol = {row["symbol"]: row["candidate_assessment"] for row in rows}
    assert by_symbol["B/USDT"]["pipeline_progress_pct"] == 60.0
    assert by_symbol["B/USDT"]["candidate_rank"] == 1
    assert by_symbol["A/USDT"]["candidate_rank"] == 2
    assert lifecycle["candidate_count"] == 2
    assert lifecycle["metrics"]["status_counts"] == {
        "WAITING_FOR_CONFIRMATION": 1,
        "WAITING_FOR_PULLBACK": 1,
    }
    assert lifecycle["metrics"]["hard_safety_veto_count"] == 0


def test_state_transition_is_explicit_across_scans():
    first, lifecycle = enrich_scan_results([
        {"coin": "A", "symbol": "A/USDT", "side": "buy", "decision": "REJECT", "rejection_stage": "RR_FILTER"}
    ], timestamp="2026-09-06T12:00:00+00:00")
    second, _ = enrich_scan_results([
        {"coin": "A", "symbol": "A/USDT", "side": "buy", "decision": "V-LIMIT"}
    ], previous_lifecycle=lifecycle, timestamp="2026-09-06T12:05:00+00:00")

    assert first[0]["candidate_assessment"]["previous_status"] is None
    assert second[0]["candidate_assessment"]["previous_status"] == "WAITING_FOR_BETTER_RR"
    assert second[0]["candidate_assessment"]["state_changed"] is True


def test_data_and_derivatives_are_hard_safety_not_soft_scores():
    data = assess_candidate({"side": "buy", "decision": "REJECT", "rejection_stage": "DATA_QUALITY"})
    derivatives = assess_candidate({"side": "sell", "decision": "REJECT", "rejection_stage": "DERIVATIVES_FILTER"})

    assert data["status"] == "DATA_INCOMPLETE"
    assert derivatives["status"] == "RISK_REJECTED"
    assert data["gate_class"] == derivatives["gate_class"] == "HARD_SAFETY"
    assert data["candidate_score_band"] == derivatives["candidate_score_band"] == "SAFETY_VETO"


def test_generic_data_failure_without_direction_does_not_look_like_a_trade_candidate():
    assessment = assess_candidate({
        "coin": "UNKNOWN",
        "decision": "REJECT",
        "rejection_stage": "DATA_QUALITY",
    })

    assert assessment["status"] == "DATA_INCOMPLETE"
    assert assessment["is_candidate"] is False


def test_observational_bias_is_ranked_but_never_promoted_to_execution_side():
    assessment = assess_candidate({
        "coin": "OBS",
        "decision": "HOLD",
        "analysis_side": "buy",
        "htf_trend": "bullish",
        "context_1h": {
            "known": True,
            "selected_state": "TOUCH",
            "next_trigger": "إغلاق 1H فوق المقاومة",
        },
    })

    assert assessment["status"] == "SETUP_FORMING"
    assert assessment["is_candidate"] is True
    assert assessment["side"] is None
    assert assessment["bias"] == "BUY"
    assert assessment["execution_side_confirmed"] is False
    assert assessment["trade_allowed_by_pipeline"] is False
    assert assessment["next_trigger"] == "إغلاق 1H فوق المقاومة"
    assert "ميل المراقبة: شراء" in assessment["analysis_summary_ar"]
    assert "لا يوجد إذن تنفيذ الآن" in assessment["analysis_summary_ar"]
    assert assessment["conditional_plan"]["direction_label_ar"] == "ميل صاعد — مراقبة فقط"


def test_conditional_plan_uses_only_production_calculated_levels():
    assessment = assess_candidate({
        "coin": "PLAN",
        "symbol": "PLAN/USDT:USDT",
        "side": "buy",
        "decision": "V-LIMIT",
        "price": 100.2,
        "invalidation_level": 97.5,
        "target_plan": {
            "valid": True,
            "entry": 100.0,
            "sl": 98.0,
            "tp": 104.0,
            "rr": 2.0,
            "min_rr": 1.5,
            "target_type": "swing_high_liquidity",
            "stop_plan": {"atr": 1.0, "final_sl": 98.0},
        },
        "entry_confirmation": {"reason": "انتظار إغلاق 5m جديد"},
    })

    plan = assessment["conditional_plan"]
    assert plan["direction_label_ar"] == "لونج — شراء"
    assert plan["entry"] == 100.0
    assert plan["entry_zone"] == {
        "low": 99.75,
        "high": 100.25,
        "tolerance": 0.25,
        "source": "PRODUCTION_ENTRY_TOLERANCE",
    }
    assert plan["stop"] == 98.0
    assert plan["stop_kind"] == "CALCULATED_STOP"
    assert plan["targets"] == [{
        "position": 1,
        "price": 104.0,
        "kind": "FINAL_TARGET",
        "label_ar": "الهدف النهائي المحسوب",
        "source": "swing_high_liquidity",
    }]
    assert plan["plan_complete"] is True
    assert any(item["code"] == "TARGET_BEFORE_ENTRY" for item in plan["cancellation_conditions"])


def test_early_candidate_does_not_invent_target_or_final_stop():
    assessment = assess_candidate({
        "coin": "EARLY",
        "side": "sell",
        "decision": "REJECT",
        "rejection_stage": "QUALITY_FILTER",
        "price": 50.0,
        "invalidation_level": 51.0,
        "_ta_context": {"optimal_entry": 49.5},
        "quality_audit": {
            "terminal_gate": "MIN_ATR",
            "values": {"atr_pct": 0.004},
            "thresholds": {"min_atr_pct": 0.007},
        },
    })

    plan = assessment["conditional_plan"]
    assert plan["entry"] == 49.5
    assert plan["entry_zone"] is not None
    assert plan["targets"] == []
    assert plan["stop"] == 51.0
    assert plan["stop_kind"] == "STRUCTURAL_INVALIDATION"
    assert plan["plan_complete"] is False
    assert all(item["code"] != "TARGET_BEFORE_ENTRY" for item in plan["cancellation_conditions"])
