"""Deterministic candidate-state and explainability contract.

This module does not decide or execute trades.  It translates the existing
production pipeline into a stable, JSON-safe state that can be rendered by
Telegram/the dashboard or supplied to an LLM without granting the LLM any
execution authority.  ``pipeline_progress_pct`` is deliberately a progress
measure, not a probability of profit.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from strategy.candidate_scoring import score_candidate
from strategy.entry_confirmation import entry_zone_tolerance


_STATUS_LABELS = {
    "IGNORED": "لا توجد فكرة تداول مكتملة",
    "SETUP_FORMING": "الفكرة قيد التكوين",
    "WAITING_FOR_VOLATILITY": "انتظار عودة التذبذب",
    "WAITING_FOR_PULLBACK": "انتظار تصحيح أو إعادة اختبار",
    "WAITING_FOR_BETTER_RR": "انتظار نقطة دخول بعائد/مخاطرة أفضل",
    "WAITING_FOR_CONFIRMATION": "انتظار تأكيد دخول مغلق",
    "ENTRY_READY": "الدخول جاهز بعد اجتياز الشروط",
    "EXECUTION_REJECTED": "رفض في طبقة التنفيذ",
    "RISK_REJECTED": "رفض حماية صلب",
    "DATA_INCOMPLETE": "البيانات غير مكتملة أو غير حديثة",
    "COOLDOWN": "الفكرة في فترة تهدئة",
    "IN_POSITION": "يوجد مركز مفتوح",
    "EXECUTED": "تم تنفيذ الصفقة",
    "INVALIDATED": "أُبطلت الفكرة",
}

_PROGRESS_STAGES = (
    "TECHNICAL_SIGNAL",
    "ENTRY_QUALITY",
    "TRADE_GEOMETRY",
    "ENTRY_TRIGGER",
    "EXECUTION",
)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _side(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"buy", "sell"} else ""


def _first_finite(*values: Any) -> float | None:
    for value in values:
        number = _finite(value)
        if number is not None and number > 0:
            return number
    return None


def _quality_gate(result: Mapping[str, Any]) -> str:
    audit = result.get("quality_audit") or {}
    if isinstance(audit, Mapping):
        return str(audit.get("terminal_gate") or "").upper()
    return ""


def _candidate_status(result: Mapping[str, Any]) -> str:
    decision = str(result.get("decision") or "").upper()
    stage = str(result.get("rejection_stage") or "").upper()
    reason = str(result.get("reason") or "").upper()

    if "ACTIVE" in decision:
        return "IN_POSITION"
    if ("BUY" in decision or "SELL" in decision) and "REJECT" not in decision:
        return "EXECUTED"
    if "V-LIMIT" in decision or "PENDING" in decision:
        return "WAITING_FOR_CONFIRMATION"

    if stage == "DATA_QUALITY":
        return "DATA_INCOMPLETE"
    if stage == "DERIVATIVES_FILTER":
        return "RISK_REJECTED"
    if stage == "SETUP_COOLDOWN":
        return "COOLDOWN"
    if stage == "ANTI_PUMP_FILTER":
        return "WAITING_FOR_PULLBACK"
    if stage == "RR_FILTER":
        return "WAITING_FOR_BETTER_RR"
    if stage == "EXECUTION":
        return "EXECUTION_REJECTED"
    if stage == "QUALITY_FILTER":
        gate = _quality_gate(result)
        if gate in {
            "DATA_SUFFICIENCY",
            "VALID_PRICE",
            "REFERENCE_CANDLE_DATA",
            "FILTER_EXCEPTION",
        }:
            return "DATA_INCOMPLETE"
        if gate == "MIN_ATR":
            return "WAITING_FOR_VOLATILITY"
        if gate in {"EXTREME_ZONE", "BIG_MOVE_RETRACEMENT", "DIRECTIONAL_EXTENSION"}:
            return "WAITING_FOR_PULLBACK"
        return "SETUP_FORMING"

    if "INVALID" in reason or "أُبطل" in reason or "إبطال" in reason:
        return "INVALIDATED"
    if "REJECT" in decision or "FAILED" in decision:
        return "EXECUTION_REJECTED"
    if _side(result.get("side")) or _side(result.get("analysis_side")):
        return "SETUP_FORMING"
    return "IGNORED"


def _passed_stages(result: Mapping[str, Any], status: str) -> list[str]:
    if status in {"EXECUTED", "IN_POSITION"}:
        return list(_PROGRESS_STAGES)
    if status in {"ENTRY_READY", "EXECUTION_REJECTED"}:
        return list(_PROGRESS_STAGES[:4])
    if status == "WAITING_FOR_CONFIRMATION":
        return list(_PROGRESS_STAGES[:3])

    stage = str(result.get("rejection_stage") or "").upper()
    if stage in {"RR_FILTER", "DATA_QUALITY"}:
        return list(_PROGRESS_STAGES[:2])
    if _side(result.get("side")):
        return list(_PROGRESS_STAGES[:1])
    return []


def _next_trigger(result: Mapping[str, Any], status: str) -> str:
    if status == "WAITING_FOR_VOLATILITY":
        audit = result.get("quality_audit") or {}
        values = (audit.get("values") or {}) if isinstance(audit, Mapping) else {}
        thresholds = (audit.get("thresholds") or {}) if isinstance(audit, Mapping) else {}
        current = _finite(values.get("atr_pct")) if isinstance(values, Mapping) else None
        required = _finite(thresholds.get("min_atr_pct")) if isinstance(thresholds, Mapping) else None
        if current is not None and required is not None:
            return (
                f"ارتفاع ATR/السعر من {current:.2%} إلى {required:.2%} على الأقل "
                "مع بقاء البنية صالحة."
            )
        return "ارتفاع التذبذب المقاس مع بقاء البنية والاتجاه صالحين."
    if status == "WAITING_FOR_PULLBACK":
        return "تصحيح أو إعادة اختبار مغلقة بدل مطاردة السعر عند طرف الحركة."
    if status == "WAITING_FOR_BETTER_RR":
        plan = result.get("target_plan") or {}
        rr = _finite(plan.get("rr")) if isinstance(plan, Mapping) else None
        minimum = _finite(plan.get("min_rr")) if isinstance(plan, Mapping) else None
        if rr is not None and minimum is not None:
            return (
                f"تحسن سعر الدخول أو ظهور هدف سيولة واقعي يرفع RR من "
                f"{rr:.2f} إلى {minimum:.2f} على الأقل."
            )
        return "تحسن سعر الدخول أو ظهور هدف واقعي يحقق الحد الأدنى لـRR."
    if status == "WAITING_FOR_CONFIRMATION":
        confirmation = result.get("entry_confirmation") or {}
        reason = str(confirmation.get("reason") or "") if isinstance(confirmation, Mapping) else ""
        return reason or "إغلاق شمعة 5m يؤكد اللمس والارتداد دون كسر الإبطال."
    if status == "COOLDOWN":
        expiry = str(result.get("cooldown_expires_at") or "").strip()
        return f"ظهور بنية جديدة أو انتهاء التهدئة عند {expiry}." if expiry else "ظهور بنية جديدة أو انتهاء فترة التهدئة."
    if status == "DATA_INCOMPLETE":
        return "وصول بيانات OHLCV/ATR حديثة وكاملة؛ البيانات الناقصة لا تمنح دخولًا."
    if status == "RISK_REJECTED":
        return "زوال سبب الحماية الصلب ثم إعادة بناء الخطة من بيانات حديثة."
    if status == "SETUP_FORMING":
        context_1h = result.get("context_1h") or {}
        if isinstance(context_1h, Mapping) and context_1h.get("next_trigger"):
            return str(context_1h["next_trigger"])
        return "اكتمال بنية صالحة ثم اجتياز الجودة وRR وتأكيد الدخول."
    if status == "ENTRY_READY":
        return "تحقق طبقة التنفيذ من السعر الفعلي والسيولة وحدود الحساب."
    if status == "EXECUTION_REJECTED":
        return "إعادة التقييم من الصفر بعد زوال سبب رفض التنفيذ."
    if status == "INVALIDATED":
        return "لا متابعة لنفس الفكرة؛ يلزم Setup جديد مستقل."
    if status in {"EXECUTED", "IN_POSITION"}:
        return "إدارة المركز وفق الوقف والهدف المسجلين."
    return "تكوين إشارة فنية قابلة للتقييم."


def _gate_class(status: str) -> str:
    if status in {"RISK_REJECTED", "DATA_INCOMPLETE", "EXECUTION_REJECTED"}:
        return "HARD_SAFETY"
    if status in {"EXECUTED", "IN_POSITION", "ENTRY_READY"}:
        return "READY_OR_ACTIVE"
    if status == "IGNORED":
        return "INFORMATIONAL"
    return "SOFT_SETUP"


def _analysis_summary(
    result: Mapping[str, Any],
    *,
    status: str,
    status_label: str,
    bias: str,
    execution_side_confirmed: bool,
    context_4h: Mapping[str, Any],
    context_1h: Mapping[str, Any],
    next_trigger: str,
) -> str:
    direction = "شراء" if bias == "buy" else "بيع" if bias == "sell" else "غير محسوم"
    # وجود ``side`` يعني أن الإشارة الفنية وصلت إلى المسار التنفيذي، لكنه لا
    # يعني أن الجودة وRR والتأكيد والتنفيذ قد اجتازت. لذلك نتجنب كلمة
    # "تنفيذ" التي أوحت للمستخدم أن البطاقة توصية جاهزة.
    prefix = "اتجاه الإشارة الأولية" if execution_side_confirmed else "ميل المراقبة"
    parts = [f"{prefix}: {direction}."]
    if context_4h.get("known"):
        extended = " والحركة ممتدة" if context_4h.get("extended") else ""
        parts.append(
            f"سياق 4H {context_4h.get('trend', 'UNKNOWN')} ببنية "
            f"{context_4h.get('structure', 'UNKNOWN')}{extended}."
        )
    else:
        parts.append("سياق 4H غير مكتمل ولم يُعامل كإشارة سلبية مصطنعة.")
    if context_1h.get("known"):
        parts.append(
            f"آخر شمعة 1H المغلقة مصنفة "
            f"{context_1h.get('selected_state', 'UNKNOWN')}."
        )
    rvol = _finite(result.get("rvol_20"))
    if rvol is not None and rvol > 0:
        parts.append(f"RVOL للعشرين شمعة السابقة يساوي {rvol:.2f}.")
    parts.append(f"الحالة الحالية: {status_label} ({status}).")
    parts.append(f"لذلك لا يوجد إذن تنفيذ الآن؛ المطلوب تالياً: {next_trigger}")
    return " ".join(parts)


def _build_conditional_plan(
    result: Mapping[str, Any],
    *,
    side: str,
    bias: str,
    status: str,
    next_trigger: str,
    invalidation: float | None,
    trade_allowed_by_pipeline: bool,
) -> dict[str, Any]:
    """Build a display-only conditional plan from production-calculated facts.

    Missing entry/target/stop values stay missing.  In particular, this layer
    never manufactures targets for candidates rejected before RiskManager.
    """

    plan = result.get("target_plan") or {}
    if not isinstance(plan, Mapping):
        plan = {}
    stop_plan = plan.get("stop_plan") or {}
    if not isinstance(stop_plan, Mapping):
        stop_plan = {}
    ta_context = result.get("_ta_context") or {}
    if not isinstance(ta_context, Mapping):
        ta_context = {}

    direction = side or bias
    direction_label = (
        "لونج — شراء" if side == "buy"
        else "شورت — بيع" if side == "sell"
        else "ميل صاعد — مراقبة فقط" if bias == "buy"
        else "ميل هابط — مراقبة فقط" if bias == "sell"
        else "الاتجاه لم يُحسم"
    )
    entry = _first_finite(
        plan.get("entry"),
        result.get("optimal_entry"),
        ta_context.get("optimal_entry"),
    )
    final_stop = _first_finite(plan.get("sl"), stop_plan.get("final_sl"))
    stop = final_stop or invalidation
    stop_kind = "CALCULATED_STOP" if final_stop is not None else (
        "STRUCTURAL_INVALIDATION" if invalidation is not None else "NOT_CALCULATED"
    )

    quality_audit = result.get("quality_audit") or {}
    quality_values = (
        quality_audit.get("values") or {}
        if isinstance(quality_audit, Mapping)
        else {}
    )
    current_price = _finite(result.get("price"))
    atr = _first_finite(stop_plan.get("atr"), result.get("atr"), ta_context.get("atr"))
    atr_pct = _finite(quality_values.get("atr_pct")) if isinstance(quality_values, Mapping) else None
    if atr is None and atr_pct is not None and current_price is not None:
        atr = current_price * atr_pct

    entry_zone = None
    if entry is not None and atr is not None:
        tolerance = entry_zone_tolerance(entry, atr)
        entry_zone = {
            "low": entry - tolerance,
            "high": entry + tolerance,
            "tolerance": tolerance,
            "source": "PRODUCTION_ENTRY_TOLERANCE",
        }

    target = _first_finite(plan.get("tp")) if plan.get("valid") is True else None
    target_type = str(plan.get("target_type") or "").strip() or None
    targets = []
    if target is not None:
        targets.append({
            "position": 1,
            "price": target,
            "kind": "FINAL_TARGET",
            "label_ar": "الهدف النهائي المحسوب",
            "source": target_type or str(plan.get("source") or "RISK_MANAGER"),
        })

    activation_conditions = [{
        "code": "CURRENT_GATE",
        "state": "PENDING",
        "text_ar": next_trigger,
    }]
    if entry_zone is not None:
        activation_conditions.append({
            "code": "ENTRY_ZONE_RETEST",
            "state": "PENDING",
            "text_ar": (
                "بعد تحقق الشرط السابق يعود السعر إلى منطقة الدخول المحسوبة "
                "دون مطاردة الحركة."
            ),
        })
    elif entry is not None:
        activation_conditions.append({
            "code": "ENTRY_LEVEL_RETEST",
            "state": "PENDING",
            "text_ar": "بعد تحقق الشرط السابق يعود السعر إلى مستوى الدخول المحسوب.",
        })
    else:
        activation_conditions.append({
            "code": "ENTRY_PLAN_REQUIRED",
            "state": "PENDING",
            "text_ar": "اجتياز جودة الدخول حتى يحسب المحرك منطقة دخول صالحة.",
        })

    if status != "WAITING_FOR_CONFIRMATION":
        activation_conditions.append({
            "code": "CLOSED_5M_CONFIRMATION",
            "state": "PENDING",
            "text_ar": "إغلاق شمعة 5m يؤكد اللمس والارتداد من دون كسر الإبطال.",
        })
    minimum_rr = _finite(plan.get("min_rr"))
    if target is not None:
        rr_text = (
            f"إعادة حساب RR قبل التنفيذ وبقاؤه عند {minimum_rr:.2f}R أو أعلى."
            if minimum_rr is not None
            else "إعادة حساب RR من السعر الفعلي قبل التنفيذ وبقاؤه مقبولاً."
        )
    else:
        rr_text = "حساب هدف سيولة واقعي وRR مقبول قبل منح أي إذن تنفيذ."
    activation_conditions.append({
        "code": "FRESH_RR_CHECK",
        "state": "PENDING",
        "text_ar": rr_text,
    })

    cancellation_conditions = []
    if invalidation is not None and direction in {"buy", "sell"}:
        relation = "أسفل" if direction == "buy" else "أعلى"
        cancellation_conditions.append({
            "code": "STRUCTURAL_INVALIDATION",
            "text_ar": f"إغلاق 15m {relation} مستوى الإبطال يلغي الفكرة.",
            "level": invalidation,
        })
    if target is not None:
        cancellation_conditions.append({
            "code": "TARGET_BEFORE_ENTRY",
            "text_ar": "وصول السعر إلى الهدف قبل أخذ أمر الدخول يلغي المطاردة.",
            "level": target,
        })
    cancellation_conditions.append({
        "code": "STALE_OR_CHANGED_SETUP",
        "text_ar": "تغيّر البنية أو انتهاء المهلة أو قدم البيانات يتطلب Setup جديداً.",
    })

    complete = bool(side and entry and final_stop and target)
    return {
        "schema_version": 1,
        "kind": "CONDITIONAL_TRADE_PLAN" if side else "OBSERVATION_WATCH_PLAN",
        "direction": direction.upper() if direction else None,
        "direction_label_ar": direction_label,
        "execution_side_confirmed": bool(side),
        "entry": entry,
        "entry_zone": entry_zone,
        "stop": stop,
        "stop_kind": stop_kind,
        "targets": targets,
        "targets_calculated": bool(targets),
        "plan_complete": complete,
        "activation_conditions": activation_conditions,
        "cancellation_conditions": cancellation_conditions,
        "trade_allowed_by_pipeline": trade_allowed_by_pipeline,
        "notice_ar": (
            "خطة مشروطة للمتابعة؛ لا تصبح صفقة قابلة للتنفيذ إلا بعد تحقق "
            "الشروط واجتياز المسار الإنتاجي كاملاً."
        ),
    }


def assess_candidate(
    result: Mapping[str, Any],
    *,
    score_weights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable truth payload for one production scan result."""

    status = _candidate_status(result)
    passed = _passed_stages(result, status)
    progress = round(100.0 * len(passed) / len(_PROGRESS_STAGES), 1)
    plan = result.get("target_plan") or {}
    ta_context = result.get("_ta_context") or {}
    market_regime = result.get("market_regime") or {}
    if isinstance(market_regime, Mapping):
        market_regime = market_regime.get("label") or market_regime.get("regime") or "UNKNOWN"
    context_4h = result.get("context_4h") or {}
    if not isinstance(context_4h, Mapping):
        context_4h = {}
    context_1h = result.get("context_1h") or {}
    if not isinstance(context_1h, Mapping):
        context_1h = {}

    invalidation = _first_finite(
        result.get("invalidation_level"),
        ta_context.get("invalidation_level") if isinstance(ta_context, Mapping) else None,
        plan.get("invalidation_level") if isinstance(plan, Mapping) else None,
        plan.get("sl") if isinstance(plan, Mapping) else None,
    )

    setup_score = _finite(result.get("setup_score"))
    if setup_score is None and isinstance(ta_context, Mapping):
        setup_score = _finite(ta_context.get("score"))
    planned_rr = _finite(plan.get("rr")) if isinstance(plan, Mapping) else None
    side = _side(result.get("side"))
    analysis_side = _side(result.get("analysis_side"))
    bias = side or analysis_side
    # لا نرفع خطأً عاماً بلا اتجاه إلى قائمة الفرص. الاستثناء الوحيد هو أمر
    # التأكيد القديم الذي قد يحمل حالته من مدير الأوامر دون تكرار ``side``.
    is_candidate = bool(bias) or status in {
        "WAITING_FOR_CONFIRMATION",
        "ENTRY_READY",
        "EXECUTED",
    }
    gate_class = _gate_class(status)
    diagnostic_score = score_candidate(
        result,
        weights=score_weights,
        hard_safety_veto=gate_class == "HARD_SAFETY",
    )
    next_trigger = _next_trigger(result, status)
    status_label = _STATUS_LABELS[status]
    analysis_summary = _analysis_summary(
        result,
        status=status,
        status_label=status_label,
        bias=bias,
        execution_side_confirmed=bool(side),
        context_4h=context_4h,
        context_1h=context_1h,
        next_trigger=next_trigger,
    )
    trade_allowed_by_pipeline = status in {"ENTRY_READY", "EXECUTED"}
    conditional_plan = _build_conditional_plan(
        result,
        side=side,
        bias=bias,
        status=status,
        next_trigger=next_trigger,
        invalidation=invalidation,
        trade_allowed_by_pipeline=trade_allowed_by_pipeline,
    )

    return {
        "schema_version": 2,
        "symbol": str(result.get("symbol") or result.get("_symbol") or result.get("coin") or ""),
        "side": side.upper() if side else None,
        "bias": bias.upper() if bias else None,
        "execution_side_confirmed": bool(side),
        "status": status,
        "status_label_ar": status_label,
        "gate_class": gate_class,
        "is_candidate": is_candidate,
        "pipeline_progress_pct": progress,
        "progress_kind": "PIPELINE_PROGRESS_NOT_WIN_PROBABILITY",
        "passed_stages": passed,
        "waiting_stage": next((stage for stage in _PROGRESS_STAGES if stage not in passed), None),
        "setup_type": str(result.get("setup_type") or result.get("winning_strategy") or "").strip() or None,
        "setup_score_raw": setup_score,
        "candidate_score": diagnostic_score["score"],
        "candidate_score_band": diagnostic_score["score_band"],
        "evidence_coverage_pct": diagnostic_score["evidence_coverage_pct"],
        "score_components": diagnostic_score["components"],
        "score_kind": diagnostic_score["score_kind"],
        "hard_safety_veto": diagnostic_score["hard_safety_veto"],
        "planned_rr": planned_rr,
        "market_regime": str(market_regime or "UNKNOWN"),
        "context_4h": dict(context_4h),
        "context_1h": dict(context_1h),
        "htf_trend": str(result.get("htf_trend") or "unknown"),
        "current_price": _finite(result.get("price")),
        "invalidation_level": invalidation,
        "next_trigger": next_trigger,
        "analysis_summary_ar": analysis_summary,
        "conditional_plan": conditional_plan,
        "reason": str(result.get("reason") or "").strip(),
        # This layer may explain and rank; it never authorizes an order.
        "execution_authority": False,
        "trade_allowed_by_pipeline": trade_allowed_by_pipeline,
    }


def enrich_scan_results(
    results: Iterable[Mapping[str, Any]],
    *,
    previous_lifecycle: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
    score_weights: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach assessments, ranks and cross-scan state transitions."""

    now = timestamp or datetime.now(timezone.utc).isoformat()
    previous_items = (previous_lifecycle or {}).get("items") or {}
    enriched: list[dict[str, Any]] = []
    lifecycle_items: dict[str, dict[str, Any]] = {}

    for raw in results:
        row = dict(raw)
        assessment = assess_candidate(row, score_weights=score_weights)
        symbol = str(assessment.get("symbol") or row.get("coin") or "")
        prior = previous_items.get(symbol) or {}
        previous_status = str(prior.get("status") or "") or None
        assessment["previous_status"] = previous_status
        assessment["state_changed"] = previous_status != assessment["status"]
        assessment["observed_at"] = now
        row["candidate_assessment"] = assessment
        enriched.append(row)
        lifecycle_items[symbol] = {
            "status": assessment["status"],
            "side": assessment["side"],
            "bias": assessment["bias"],
            "pipeline_progress_pct": assessment["pipeline_progress_pct"],
            "candidate_score": assessment["candidate_score"],
            "evidence_coverage_pct": assessment["evidence_coverage_pct"],
            "next_trigger": assessment["next_trigger"],
            "observed_at": now,
        }

    candidates = [
        row for row in enriched
        if row["candidate_assessment"]["is_candidate"]
        and row["candidate_assessment"]["status"] not in {"IGNORED", "IN_POSITION"}
    ]
    candidates.sort(
        key=lambda row: (
            bool(row["candidate_assessment"]["hard_safety_veto"]),
            -float(row["candidate_assessment"]["pipeline_progress_pct"]),
            -float(row["candidate_assessment"]["candidate_score"] or 0.0),
            -float(row["candidate_assessment"]["evidence_coverage_pct"] or 0.0),
            -float(row["candidate_assessment"]["setup_score_raw"] or 0.0),
            -float(row["candidate_assessment"]["planned_rr"] or 0.0),
            str(row["candidate_assessment"]["symbol"]),
        )
    )
    for rank, row in enumerate(candidates, start=1):
        row["candidate_assessment"]["candidate_rank"] = rank

    scores = [
        float(row["candidate_assessment"]["candidate_score"])
        for row in candidates
        if row["candidate_assessment"].get("candidate_score") is not None
    ]
    coverages = [
        float(row["candidate_assessment"]["evidence_coverage_pct"])
        for row in candidates
        if row["candidate_assessment"].get("evidence_coverage_pct") is not None
    ]
    status_counts = Counter(
        str(row["candidate_assessment"].get("status") or "UNKNOWN")
        for row in candidates
    )
    band_counts = Counter(
        str(row["candidate_assessment"].get("candidate_score_band") or "UNKNOWN")
        for row in candidates
    )
    metrics = {
        "average_candidate_score": round(sum(scores) / len(scores), 1) if scores else None,
        "average_evidence_coverage_pct": (
            round(sum(coverages) / len(coverages), 1) if coverages else None
        ),
        "hard_safety_veto_count": sum(
            1 for row in candidates
            if row["candidate_assessment"].get("hard_safety_veto")
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "score_band_counts": dict(sorted(band_counts.items())),
    }

    return enriched, {
        "schema_version": 1,
        "updated_at": now,
        "ranking_method": "PIPELINE_PROGRESS_THEN_EXISTING_SETUP_SCORE_NOT_PROFIT_PROBABILITY",
        "candidate_count": len(candidates),
        "metrics": metrics,
        "items": lifecycle_items,
    }
