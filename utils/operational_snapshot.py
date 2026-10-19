"""Safe, read-only operational evidence derived from the durable bot state."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from core.config import Config
from core.version import APP_VERSION, DEPLOYMENT_ID, GIT_BRANCH, GIT_COMMIT, GIT_REPO
from utils.telegram_proposal_runtime_identity import telegram_proposal_runtime_identity


def _as_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_proposal_record(proposal_id, record):
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    timeline = record.get("timeline") if isinstance(record.get("timeline"), list) else []
    return {
        "proposal_id": str(proposal_id),
        "symbol": proposal.get("symbol"),
        "side": proposal.get("side"),
        "proposal_time": proposal.get("timestamp") or record.get("visible_at"),
        "proposed_entry": _as_float(
            proposal.get("proposed_entry") or proposal.get("entry")
        ),
        "original_stop": _as_float(
            proposal.get("original_stop") or proposal.get("stop")
        ),
        "original_target": _as_float(
            proposal.get("original_target") or proposal.get("target")
        ),
        "status": record.get("status"),
        "visible_at": record.get("visible_at"),
        "updated_at": record.get("updated_at"),
        "entry_post_count": int(record.get("entry_post_count") or 0),
        "exchange_order_id": record.get("exchange_order_id"),
        "venue_status": record.get("venue_status"),
        "actual_entry": _as_float(record.get("actual_entry")),
        "filled_amount": _as_float(record.get("filled_amount") or record.get("executed_qty")),
        "sl": _as_float(record.get("sl")),
        "tp": _as_float(record.get("tp")),
        "realized_pnl": _as_float(record.get("realized_pnl")),
        "realized_r": _as_float(record.get("realized_r")),
        "fees": _as_float(record.get("fees")),
        "funding": _as_float(record.get("funding")),
        "reconciliation": record.get("reconciliation"),
        "timeline": timeline,
    }


def _safe_position_record(symbol, meta):
    proposal = meta.get("user_visible_trade_proposal")
    if not isinstance(proposal, dict):
        proposal = {}
    return {
        "symbol": symbol,
        "side": meta.get("side") or meta.get("position_side"),
        "trade_id": meta.get("trade_id") or meta.get("position_id"),
        "proposal_id": proposal.get("proposal_id") or meta.get("proposal_id"),
        "entry_price": _as_float(meta.get("entry_price")),
        "amount": _as_float(meta.get("original_amount") or meta.get("amount")),
        "initial_sl": _as_float(meta.get("initial_sl") or meta.get("sl")),
        "current_sl": _as_float(meta.get("current_sl") or meta.get("sl")),
        "target": _as_float(meta.get("tp")),
        "planned_rr": _as_float(meta.get("planned_rr")),
        "protection_status": meta.get("protection_status") or "UNKNOWN",
        "sl_order_id": meta.get("sl_order_id"),
        "tp_order_id": meta.get("tp_order_id"),
        "external_position": bool(meta.get("external_position", False)),
        "entry_time": meta.get("entry_time") or meta.get("timestamp"),
    }


def build_operational_snapshot(state_manager, client=None):
    """Return a secret-free snapshot suitable for Telegram exports/review."""
    state = state_manager.get_state() if state_manager is not None else {}
    ledger = state.get("user_visible_trade_proposals", {}) or {}
    metadata = state.get("entry_metadata", {}) or {}
    proposals = [
        _safe_proposal_record(proposal_id, record)
        for proposal_id, record in sorted(ledger.items())
        if isinstance(record, dict)
    ]
    positions = [
        _safe_position_record(symbol, meta)
        for symbol, meta in sorted(metadata.items())
        if isinstance(meta, dict)
    ]
    status_counts = Counter(str(item.get("status") or "UNKNOWN") for item in proposals)
    unprotected = [
        item["symbol"] for item in positions
        if not item["external_position"] and item["protection_status"] != "PROTECTED"
    ]
    duplicates = [
        item["proposal_id"] for item in proposals if item["entry_post_count"] > 1
    ]
    endpoint = {}
    if client is not None:
        proof = getattr(client, "_environment_proof", None)
        if isinstance(proof, dict):
            endpoint = {
                "is_demo": bool(proof.get("is_demo")),
                "mainnet": bool(proof.get("mainnet")),
                "allowed_host": proof.get("allowed_host"),
                "observed_hosts": list(proof.get("observed_hosts") or []),
                "order_calls_possible": bool(proof.get("order_calls_possible")),
            }

    identity = telegram_proposal_runtime_identity()
    warnings = []
    if bool(Config.ACCOUNT_MANAGEMENT_ENABLED) and not state.get("recovery_complete", False):
        warnings.append("RECOVERY_INCOMPLETE")
    if unprotected:
        warnings.append("MANAGED_POSITION_NOT_PROVEN_PROTECTED")
    if duplicates:
        warnings.append("DUPLICATE_ENTRY_POST_COUNT")

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": {
            "app_version": APP_VERSION,
            "repository": GIT_REPO,
            "branch": GIT_BRANCH,
            "commit": GIT_COMMIT,
            "deployment_id": DEPLOYMENT_ID,
            "runtime_build_hash": identity.get("runtime_build_hash"),
        },
        "mode": {
            "use_testnet": bool(Config.USE_TESTNET),
            "demo_execution_enabled": bool(Config.DEMO_EXECUTION_ENABLED),
            "telegram_polling_enabled": bool(Config.TELEGRAM_POLLING_ENABLED),
            "telegram_outbound_enabled": bool(Config.TELEGRAM_OUTBOUND_ENABLED),
            "account_management_enabled": bool(Config.ACCOUNT_MANAGEMENT_ENABLED),
            "effective_position_cap": (
                min(int(Config.TESTNET_MAX_OPEN_TRADES), int(Config.HARD_MAX_OPEN_TRADES))
                if Config.DEMO_EXECUTION_ENABLED else 0
            ),
        },
        "endpoint": endpoint,
        "reconciliation": {
            "recovery_complete": bool(state.get("recovery_complete", False)),
            "last_successful_at": state.get("last_successful_reconciliation_at"),
            "last_scan_completed_at": state.get("last_scan_completed_at"),
            "state_schema_version": state.get("schema_version"),
        },
        "proposal_summary": {
            **(state.get("telegram_proposal_demo_summary", {}) or {}),
            "status_counts": dict(sorted(status_counts.items())),
            "duplicate_entry_post_proposal_ids": duplicates,
        },
        "proposals": proposals,
        "open_positions": positions,
        "protection_summary": {
            "managed_position_count": sum(not item["external_position"] for item in positions),
            "external_position_count": sum(item["external_position"] for item in positions),
            "unprotected_managed_symbols": unprotected,
        },
        "warnings": warnings,
    }


def flatten_proposal_timelines(snapshot):
    """Produce reviewer-friendly chronological lifecycle rows."""
    rows = []
    for proposal in snapshot.get("proposals", []):
        timeline = proposal.get("timeline") or []
        if not timeline:
            rows.append({
                "proposal_id": proposal.get("proposal_id"),
                "symbol": proposal.get("symbol"),
                "side": proposal.get("side"),
                "timestamp": proposal.get("updated_at") or proposal.get("visible_at"),
                "status": proposal.get("status"),
                "details": {"legacy_record_without_timeline": True},
            })
            continue
        for event in timeline:
            rows.append({
                "proposal_id": proposal.get("proposal_id"),
                "symbol": proposal.get("symbol"),
                "side": proposal.get("side"),
                **event,
            })
    rows.sort(key=lambda item: str(item.get("timestamp") or ""))
    return rows
