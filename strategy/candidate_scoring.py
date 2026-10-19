"""Evidence-based candidate scoring for observability and ranking only.

The production execution pipeline remains authoritative.  This module converts
already-computed deterministic evidence into an explainable 0-100 score.  It
does not create signals, bypass gates, size positions, or submit orders.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


DEFAULT_WEIGHTS = {
    "technical_setup": 10.0,
    "context_4h": 15.0,
    "htf_alignment": 5.0,
    "setup_1h": 10.0,
    "momentum": 10.0,
    "volume": 10.0,
    "entry_quality": 15.0,
    "trade_geometry": 15.0,
    "entry_trigger": 5.0,
    "derivatives_context": 5.0,
}

_LABELS_AR = {
    "technical_setup": "جودة الإشارة الفنية",
    "context_4h": "سياق الأربع ساعات",
    "htf_alignment": "توافق اتجاه الساعة",
    "setup_1h": "جودة إعداد الساعة",
    "momentum": "الزخم الحالي",
    "volume": "مشاركة حجم التداول",
    "entry_quality": "جودة موقع الدخول",
    "trade_geometry": "العائد مقابل المخاطرة",
    "entry_trigger": "تأكيد الدخول المغلق",
    "derivatives_context": "سياق المشتقات",
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _direction(result: Mapping[str, Any]) -> str:
    value = str(result.get("side") or result.get("analysis_side") or "").lower()
    return value if value in {"buy", "sell"} else ""


def _normalized_weights(weights: Mapping[str, Any] | None) -> dict[str, float]:
    source = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, Mapping):
        for key in source:
            value = _finite(weights.get(key))
            if value is not None and value >= 0:
                source[key] = value
    if sum(source.values()) <= 0:
        return dict(DEFAULT_WEIGHTS)
    return source


def _component(name: str, value: float | None, evidence: str) -> dict[str, Any]:
    return {
        "name": name,
        "label_ar": _LABELS_AR[name],
        "available": value is not None,
        "value": None if value is None else round(_clamp(value), 4),
        "evidence": evidence,
    }


def _technical_component(result: Mapping[str, Any]) -> dict[str, Any]:
    context = result.get("_ta_context") or {}
    raw = _finite(result.get("setup_score"))
    if raw is None and isinstance(context, Mapping):
        raw = _finite(context.get("score"))
    if raw is None:
        return _component("technical_setup", None, "لا توجد درجة فنية مسجلة")
    return _component(
        "technical_setup",
        _clamp(raw / 3.0),
        f"درجة محرك SMC الحالية {raw:.2f} من 3",
    )


def _htf_component(result: Mapping[str, Any]) -> dict[str, Any]:
    side = _direction(result)
    trend = str(result.get("htf_trend") or "unknown").lower()
    if side not in {"buy", "sell"} or trend not in {"bullish", "bearish", "neutral"}:
        return _component("htf_alignment", None, "اتجاه أو جهة الصفقة غير متاح")
    aligned = (side == "buy" and trend == "bullish") or (
        side == "sell" and trend == "bearish"
    )
    value = 1.0 if aligned else (0.5 if trend == "neutral" else 0.0)
    return _component(
        "htf_alignment",
        value,
        f"الاتجاه 1H={trend.upper()} مقابل {side.upper()}",
    )


def _context_4h_component(result: Mapping[str, Any]) -> dict[str, Any]:
    context = result.get("context_4h") or {}
    if not isinstance(context, Mapping) or not context.get("known"):
        return _component("context_4h", None, "سياق 4H غير متاح؛ لم يتحول إلى رفض")
    side = _direction(result)
    trend = str(context.get("trend") or "UNKNOWN").upper()
    if side not in {"buy", "sell"} or trend not in {"BULLISH", "BEARISH", "RANGE"}:
        return _component("context_4h", None, "سياق 4H أو جهة المرشح غير صالح")
    aligned = (side == "buy" and trend == "BULLISH") or (
        side == "sell" and trend == "BEARISH"
    )
    value = 1.0 if aligned else (0.5 if trend == "RANGE" else 0.0)
    if context.get("extended"):
        value *= 0.5
    return _component(
        "context_4h",
        value,
        str(context.get("reason") or f"4H={trend}"),
    )


def _setup_1h_component(result: Mapping[str, Any]) -> dict[str, Any]:
    context = result.get("context_1h") or {}
    if not isinstance(context, Mapping) or not context.get("known"):
        return _component("setup_1h", None, "إعداد 1H غير متاح؛ لم يتحول إلى رفض")
    state = str(context.get("selected_state") or "UNKNOWN").upper()
    values = {
        "CONFIRMED_BREAKOUT": 1.0,
        "CONFIRMED_BREAKDOWN": 1.0,
        "CLOSE_BREAK": 0.8,
        "TOUCH": 0.55,
        "INSIDE_RANGE": 0.35,
        "WICK_BREAK": 0.15,
    }
    value = values.get(state)
    return _component(
        "setup_1h",
        value,
        f"تفاعل آخر شمعة 1H المغلقة مع المستوى: {state}",
    )


def _momentum_component(result: Mapping[str, Any]) -> dict[str, Any]:
    context = result.get("_ta_context") or {}
    adx = _finite(result.get("adx"))
    rsi = _finite(result.get("rsi"))
    if isinstance(context, Mapping):
        adx = adx if adx is not None else _finite(context.get("adx"))
        rsi = rsi if rsi is not None else _finite(context.get("rsi"))
    side = _direction(result)
    if adx is None or rsi is None or side not in {"buy", "sell"}:
        return _component("momentum", None, "ADX/RSI أو جهة المرشح غير متاحة")
    adx_quality = _clamp((adx - 15.0) / 20.0)
    if side == "buy":
        rsi_quality = 1.0 if 50.0 <= rsi <= 68.0 else (0.5 if 42.0 <= rsi < 50.0 else 0.2)
    else:
        rsi_quality = 1.0 if 32.0 <= rsi <= 50.0 else (0.5 if 50.0 < rsi <= 58.0 else 0.2)
    return _component(
        "momentum",
        0.6 * adx_quality + 0.4 * rsi_quality,
        f"ADX={adx:.1f} وRSI={rsi:.1f} للاتجاه {side.upper()}",
    )


def _quality_component(result: Mapping[str, Any]) -> dict[str, Any]:
    audit = result.get("quality_audit") or {}
    if not isinstance(audit, Mapping) or not audit:
        return _component("entry_quality", None, "لم تصل الفكرة إلى فحص الجودة")
    gate = str(audit.get("terminal_gate") or "UNKNOWN").upper()
    if gate == "ALL_GATES":
        value = 1.0
    elif gate == "MIN_ATR":
        value = 0.35
    elif gate in {"EXTREME_ZONE", "BIG_MOVE_RETRACEMENT", "DIRECTIONAL_EXTENSION"}:
        value = 0.25
    elif gate in {"DATA_SUFFICIENCY", "VALID_PRICE", "REFERENCE_CANDLE_DATA", "FILTER_EXCEPTION"}:
        value = 0.0
    else:
        value = 0.4
    return _component("entry_quality", value, f"بوابة الجودة النهائية: {gate}")


def _volume_component(result: Mapping[str, Any]) -> dict[str, Any]:
    context = result.get("_ta_context") or {}
    rvol = _finite(result.get("rvol_20"))
    if rvol is None and isinstance(context, Mapping):
        rvol = _finite(context.get("rvol_20"))
    side = _direction(result)
    open_price = _finite(result.get("open"))
    close_price = _finite(result.get("price"))
    if rvol is None or rvol <= 0 or side not in {"buy", "sell"}:
        return _component("volume", None, "RVOL غير متاح؛ لم يتحول إلى رفض")
    base = _clamp((rvol - 0.5) / 1.5)
    aligned = None
    if open_price is not None and close_price is not None:
        aligned = (side == "buy" and close_price > open_price) or (
            side == "sell" and close_price < open_price
        )
        if not aligned and close_price != open_price:
            base *= 0.4
    alignment_text = (
        "ومتوافق مع اتجاه الشمعة" if aligned is True
        else "لكن اتجاه الشمعة معاكس" if aligned is False
        else "واتجاه الشمعة غير محسوم"
    )
    return _component("volume", base, f"RVOL20={rvol:.2f} {alignment_text}")


def _geometry_component(result: Mapping[str, Any]) -> dict[str, Any]:
    plan = result.get("target_plan") or {}
    if not isinstance(plan, Mapping) or not plan:
        return _component("trade_geometry", None, "لم تُحسب خطة هدف/إبطال بعد")
    rr = _finite(plan.get("rr"))
    minimum = _finite(plan.get("min_rr")) or 1.5
    if rr is None or minimum <= 0:
        return _component("trade_geometry", None, "RR غير متاح في الخطة")
    return _component(
        "trade_geometry",
        _clamp(rr / minimum),
        f"RR={rr:.2f} والحد المسجل={minimum:.2f}",
    )


def _trigger_component(result: Mapping[str, Any]) -> dict[str, Any]:
    confirmation = result.get("entry_confirmation") or {}
    if not isinstance(confirmation, Mapping) or not confirmation:
        return _component("entry_trigger", None, "لم تصل الفكرة إلى تأكيد الدخول")
    if confirmation.get("passed"):
        value = 1.0
    else:
        score = _finite(confirmation.get("score"))
        value = _clamp(score / 100.0) if score is not None else 0.0
    reason = str(confirmation.get("reason") or "تأكيد مسجل بلا وصف")
    return _component("entry_trigger", value, reason)


def _derivatives_component(result: Mapping[str, Any]) -> dict[str, Any]:
    derivatives = result.get("derivatives_result") or {}
    if not isinstance(derivatives, Mapping) or not derivatives:
        return _component(
            "derivatives_context", None,
            "بيانات OI/Funding اختيارية وغير متاحة لهذا المرشح",
        )
    decision = str(derivatives.get("decision") or "UNKNOWN").upper()
    value = {"ALLOW": 1.0, "WARNING": 0.5, "BLOCK_SUGGESTED": 0.0}.get(decision)
    return _component(
        "derivatives_context",
        value,
        str(derivatives.get("reason") or f"قرار المشتقات={decision}"),
    )


def score_candidate(
    result: Mapping[str, Any],
    *,
    weights: Mapping[str, Any] | None = None,
    hard_safety_veto: bool = False,
) -> dict[str, Any]:
    """Return an explainable diagnostic score without granting trade authority."""

    resolved = _normalized_weights(weights)
    components = [
        _technical_component(result),
        _context_4h_component(result),
        _htf_component(result),
        _setup_1h_component(result),
        _momentum_component(result),
        _volume_component(result),
        _quality_component(result),
        _geometry_component(result),
        _trigger_component(result),
        _derivatives_component(result),
    ]
    total_weight = sum(resolved.values())
    available_weight = 0.0
    earned = 0.0
    for component in components:
        weight = resolved[component["name"]]
        component["weight"] = round(weight, 4)
        if component["available"]:
            available_weight += weight
            points = weight * float(component["value"])
            earned += points
            component["points"] = round(points, 2)
        else:
            component["points"] = None

    score = 0.0 if available_weight <= 0 else 100.0 * earned / available_weight
    coverage = 0.0 if total_weight <= 0 else 100.0 * available_weight / total_weight
    if hard_safety_veto:
        band = "SAFETY_VETO"
    elif coverage < 40.0:
        band = "INSUFFICIENT_EVIDENCE"
    elif score >= 85.0 and coverage >= 80.0:
        band = "HIGH_QUALITY_SETUP"
    elif score >= 75.0 and coverage >= 65.0:
        band = "VALID_SETUP"
    elif score >= 65.0:
        band = "WATCHLIST"
    elif score >= 50.0:
        band = "WEAK_SETUP"
    else:
        band = "LOW_QUALITY"

    return {
        "score": round(score, 1),
        "score_band": band,
        "evidence_coverage_pct": round(coverage, 1),
        "available_weight": round(available_weight, 2),
        "total_weight": round(total_weight, 2),
        "components": components,
        "hard_safety_veto": bool(hard_safety_veto),
        "score_kind": "DIAGNOSTIC_EXISTING_EVIDENCE_NOT_EXECUTION_PERMISSION",
        "execution_authority": False,
    }
