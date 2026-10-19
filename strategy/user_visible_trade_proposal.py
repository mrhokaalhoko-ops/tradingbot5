"""Canonical user-visible trade proposal contract.

The event is created only inside the central execution gate, after every
strategy/confirmation/geometry check has passed and before the first entry
POST.  Shadow/watchlist/candidate-assessment events cannot construct this
contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


EVENT_TYPE = "USER_VISIBLE_TRADE_PROPOSAL"
SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_user_visible_trade_proposal(
    *,
    proposal_id: str,
    symbol: str,
    side: str,
    proposed_entry: float,
    original_stop: float,
    original_target: float,
    target_stages: list[dict[str, Any]] | None,
    thesis_id: str | None,
    setup_id: str | None,
    confirmation_bar_id: str | int | None,
    leverage: int,
    risk_percent: float,
    amount: float,
    target_plan: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    normalized_side = str(side or "").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("proposal side must be BUY or SELL")
    numeric = {
        "proposed_entry": float(proposed_entry or 0),
        "original_stop": float(original_stop or 0),
        "original_target": float(original_target or 0),
        "amount": float(amount or 0),
    }
    if min(numeric.values()) <= 0:
        raise ValueError("proposal entry/stop/target/amount must be positive")
    if normalized_side == "BUY" and not (
        numeric["original_stop"] < numeric["proposed_entry"]
        < numeric["original_target"]
    ):
        raise ValueError("invalid BUY proposal geometry")
    if normalized_side == "SELL" and not (
        numeric["original_target"] < numeric["proposed_entry"]
        < numeric["original_stop"]
    ):
        raise ValueError("invalid SELL proposal geometry")

    event = {
        "event_type": EVENT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "proposal_id": str(proposal_id),
        "symbol": str(symbol),
        "side": normalized_side,
        **numeric,
        "target_stages": list(target_stages or []),
        "timestamp": str(timestamp or _utc_now()),
        "thesis_id": str(thesis_id) if thesis_id else None,
        "setup_id": str(setup_id) if setup_id else None,
        "confirmation_bar_id": (
            str(confirmation_bar_id) if confirmation_bar_id is not None else None
        ),
        "leverage": int(leverage),
        "risk_percent": float(risk_percent),
        "target_plan": dict(target_plan or {}),
        "execution_authority": "DEMO_ONLY",
        "source": "CURRENT_TELEGRAM_RUNTIME_CENTRAL_EXECUTION_GATE",
    }
    identity = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    event["event_hash"] = hashlib.sha256(identity).hexdigest().upper()
    return event

