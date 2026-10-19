"""Fail-closed contract between Mainnet signals and Demo execution.

This module has no account or order capability.  It only validates that a
candidate built from Binance USD-M Mainnet public data can be evaluated and
executed faithfully against the corresponding Binance Demo contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping

from shadow_runtime.models import ExecutionQuote, MarketSnapshot, Quote


MAINNET_USDM_PUBLIC_URL = "https://fapi.binance.com"
SIGNAL_MARKET_ENVIRONMENT = "MAINNET"
EXECUTION_ENVIRONMENT = "DEMO"
MAINNET_SNAPSHOT_SOURCE = "BINANCE_MAINNET_PUBLIC_USDM"
DEMO_EXECUTION_QUOTE_SOURCE = "BINANCE_DEMO_PUBLIC_REST_BOOK_TICKER"

_IDENTITY_FIELDS = (
    "symbol",
    "contract_type",
    "base_asset",
    "quote_asset",
    "margin_asset",
)
_NUMERIC_FILTER_FIELDS = (
    "tick_size",
    "step_size",
    "min_qty",
    "max_qty",
    "min_notional",
)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def validate_mainnet_signal_endpoint(base_url: str) -> dict[str, Any]:
    """Accept exactly the credential-free Binance USD-M Mainnet public host."""
    normalized = str(base_url or "").rstrip("/")
    if normalized != MAINNET_USDM_PUBLIC_URL:
        raise RuntimeError(f"MAINNET_SIGNAL_ENDPOINT_REJECTED:{normalized}")
    return {
        "signal_market_environment": SIGNAL_MARKET_ENVIRONMENT,
        "signal_base_url": normalized,
        "public_get_only": True,
        "credentials_present": False,
        "order_route": False,
    }


def compare_contracts(
    symbol: str,
    signal_contract: Mapping[str, Any] | None,
    execution_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare identities and record every execution-filter difference.

    Filter equality is not required: Demo quantity and price filters are the
    authoritative execution filters, and the frozen executor already rounds
    against those filters and rechecks RR/risk.  Missing, non-positive, or
    semantically different contracts fail closed.
    """
    signal = dict(signal_contract or {})
    execution = dict(execution_contract or {})
    reasons: list[str] = []
    if not signal:
        reasons.append("SIGNAL_CONTRACT_NOT_FOUND")
    if not execution:
        reasons.append("SYMBOL_NOT_AVAILABLE_ON_DEMO")

    if signal and execution:
        for field in _IDENTITY_FIELDS:
            left = str(signal.get(field) or "")
            right = str(execution.get(field) or "")
            if not left or not right:
                reasons.append(f"CONTRACT_IDENTITY_MISSING:{field}")
            elif left != right:
                reasons.append(f"CONTRACT_IDENTITY_MISMATCH:{field}:{left}!={right}")
        for environment, contract in (("MAINNET", signal), ("DEMO", execution)):
            if str(contract.get("contract_type") or "") != "PERPETUAL":
                reasons.append(
                    f"UNSUPPORTED_CONTRACT_TYPE:{environment}:"
                    f"{str(contract.get('contract_type') or 'MISSING')}"
                )
            if str(contract.get("status") or "") != "TRADING":
                reasons.append(
                    f"CONTRACT_NOT_TRADING:{environment}:"
                    f"{str(contract.get('status') or 'MISSING')}"
                )
            for field in _NUMERIC_FILTER_FIELDS:
                value = _decimal(contract.get(field))
                if value is None or value <= 0:
                    reasons.append(f"INVALID_{environment}_FILTER:{field}")

    filter_comparison = {}
    for field in _NUMERIC_FILTER_FIELDS:
        left = _decimal(signal.get(field))
        right = _decimal(execution.get(field))
        filter_comparison[field] = {
            "mainnet": str(left) if left is not None else None,
            "demo": str(right) if right is not None else None,
            "equal": left is not None and right is not None and left == right,
        }
    return {
        "symbol": str(symbol),
        "compatible": not reasons,
        "reasons": reasons,
        "identity_fields_checked": list(_IDENTITY_FIELDS),
        "filter_fields_checked": list(_NUMERIC_FILTER_FIELDS),
        "filter_comparison": filter_comparison,
        "signal_contract": signal,
        "execution_contract": execution,
        "execution_filters_authoritative": True,
        "post_rounding_rr_recheck_required": True,
    }


def demo_execution_quote(symbol: str, quote: Quote) -> ExecutionQuote:
    return ExecutionQuote(
        symbol=str(symbol),
        event_time=quote.observed_at,
        received_at=datetime.now(timezone.utc),
        best_bid=float(quote.best_bid),
        best_ask=float(quote.best_ask),
        update_id=int(quote.observed_at.timestamp() * 1000),
        source=DEMO_EXECUTION_QUOTE_SOURCE,
    )


def audit_cross_environment_candidate(
    snapshot: MarketSnapshot,
    signal_contract: Mapping[str, Any] | None,
    execution_contract: Mapping[str, Any] | None,
    execution_quote: ExecutionQuote | None,
) -> dict[str, Any]:
    """Return machine-readable proof; never raises for a candidate mismatch."""
    contract = compare_contracts(
        snapshot.symbol, signal_contract, execution_contract
    )
    reasons = list(contract["reasons"])
    if snapshot.source != MAINNET_SNAPSHOT_SOURCE:
        reasons.append(f"SIGNAL_SNAPSHOT_SOURCE_MISMATCH:{snapshot.source}")
    if snapshot.symbol != snapshot.candidate.symbol:
        reasons.append("SIGNAL_SYMBOL_MISMATCH")
    if snapshot.as_of < snapshot.candidate.emitted_at:
        reasons.append("SIGNAL_SNAPSHOT_BEFORE_CANDIDATE")
    event_close_raw = snapshot.candidate.event.get("close_time")
    event_close = None
    try:
        event_close = datetime.fromisoformat(
            str(event_close_raw).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        reasons.append("CANDIDATE_CLOSE_TIME_INVALID")
    if event_close is not None:
        if event_close.tzinfo is None:
            reasons.append("CANDIDATE_CLOSE_TIME_NOT_UTC_AWARE")
        elif event_close > snapshot.candidate.emitted_at:
            reasons.append("CANDIDATE_CLOSE_AFTER_EMISSION")
    if execution_quote is None:
        reasons.append("DEMO_EXECUTION_QUOTE_UNAVAILABLE")
    else:
        if execution_quote.symbol != snapshot.symbol:
            reasons.append("DEMO_QUOTE_SYMBOL_MISMATCH")
        if execution_quote.source != DEMO_EXECUTION_QUOTE_SOURCE:
            reasons.append(f"DEMO_QUOTE_SOURCE_MISMATCH:{execution_quote.source}")
        if execution_quote.event_time < snapshot.candidate.emitted_at:
            reasons.append("DEMO_QUOTE_BEFORE_CANDIDATE")

    signal_ask = float(snapshot.quote.best_ask)
    execution_ask = float(execution_quote.best_ask) if execution_quote else None
    delta_bps = None
    if execution_ask is not None and signal_ask > 0:
        delta_bps = (execution_ask / signal_ask - 1.0) * 10_000.0
    result = {
        "ok": not reasons,
        "reasons": reasons,
        "signal_market_environment": SIGNAL_MARKET_ENVIRONMENT,
        "execution_environment": EXECUTION_ENVIRONMENT,
        "signal_snapshot_id": snapshot.snapshot_id,
        "signal_snapshot_source": snapshot.source,
        "candidate_id": snapshot.candidate.candidate_id,
        "candidate_emitted_at": snapshot.candidate.emitted_at.isoformat(),
        "candidate_closed_at": (
            event_close.isoformat() if event_close is not None else None
        ),
        "signal_snapshot_as_of": snapshot.as_of.isoformat(),
        "signal_closed_candle_ids": snapshot.closed_candle_ids,
        "signal_best_bid": float(snapshot.quote.best_bid),
        "signal_best_ask": signal_ask,
        "execution_quote_time": (
            execution_quote.event_time.isoformat() if execution_quote else None
        ),
        "execution_best_bid": (
            float(execution_quote.best_bid) if execution_quote else None
        ),
        "execution_best_ask": execution_ask,
        "execution_vs_signal_ask_delta_bps": delta_bps,
        "price_divergence_threshold_applied": False,
        "geometry_must_pass_frozen_b_at_demo_quote": True,
        "contract": contract,
    }
    result["audit_hash"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().upper()
    return result


def verify_cross_environment_audit(
    snapshot: MarketSnapshot,
    execution_quote: ExecutionQuote | None,
    audit: Mapping[str, Any] | None,
) -> list[str]:
    """Independently reject incomplete or mismatched caller-supplied proof."""
    proof = dict(audit or {})
    reasons: list[str] = []
    if not proof.get("ok"):
        reasons.extend(proof.get("reasons") or ["CROSS_ENVIRONMENT_AUDIT_REQUIRED"])
    if proof.get("signal_market_environment") != SIGNAL_MARKET_ENVIRONMENT:
        reasons.append("AUDIT_SIGNAL_ENVIRONMENT_MISMATCH")
    if proof.get("execution_environment") != EXECUTION_ENVIRONMENT:
        reasons.append("AUDIT_EXECUTION_ENVIRONMENT_MISMATCH")
    if proof.get("candidate_id") != snapshot.candidate.candidate_id:
        reasons.append("AUDIT_CANDIDATE_ID_MISMATCH")
    if proof.get("signal_snapshot_id") != snapshot.snapshot_id:
        reasons.append("AUDIT_SNAPSHOT_ID_MISMATCH")
    contract = dict(proof.get("contract") or {})
    if not contract.get("compatible"):
        reasons.append("AUDIT_CONTRACT_NOT_COMPATIBLE")
    if execution_quote is None:
        reasons.append("DEMO_EXECUTION_QUOTE_REQUIRED")
    else:
        if execution_quote.source != DEMO_EXECUTION_QUOTE_SOURCE:
            reasons.append("EXECUTION_QUOTE_NOT_FROM_DEMO")
        if execution_quote.symbol != snapshot.symbol:
            reasons.append("EXECUTION_QUOTE_SYMBOL_MISMATCH")
        if proof.get("execution_quote_time") != execution_quote.event_time.isoformat():
            reasons.append("AUDIT_EXECUTION_QUOTE_TIME_MISMATCH")
        if proof.get("execution_best_ask") != float(execution_quote.best_ask):
            reasons.append("AUDIT_EXECUTION_PRICE_MISMATCH")
    expected_hash = proof.pop("audit_hash", None)
    actual_hash = hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().upper()
    if expected_hash != actual_hash:
        reasons.append("AUDIT_HASH_MISMATCH")
    return list(dict.fromkeys(reasons))
