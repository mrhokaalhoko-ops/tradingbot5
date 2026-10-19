"""Deterministic Demo discovery; venue eligibility is supplied by UniverseFilter."""

import math
import re
from datetime import datetime


ACTIVITY_SOURCE = "BINANCE_USDM_PUBLIC_TICKER"


def canonical_usdt_symbol(symbol):
    value = str(symbol or "").upper().strip()
    if ":" in value:
        value, settlement = value.split(":", 1)
        if settlement != "USDT":
            return ""
    if value.endswith("/USDT"):
        base = value[:-5]
    elif "/" not in value and value.endswith("USDT"):
        base = value[:-4]
    else:
        return ""
    return base + "/USDT" if re.fullmatch(r"[A-Z0-9]+", base) else ""


def select_demo_targets(eligible, *, forced, limit, snapshot, now_ts,
                        activity_slots=20, max_age_seconds=180):
    """Reserve a bounded discovery lane, then fill unused slots with liquidity."""
    limit = max(0, int(limit))
    by_symbol = {}
    for row in sorted(eligible, key=lambda item: (-item[2], item[0])):
        key = canonical_usdt_symbol(row[0])
        if key:
            by_symbol.setdefault(key, row)
    forced_keys = list(dict.fromkeys(
        key for symbol in forced
        if (key := canonical_usdt_symbol(symbol)) in by_symbol
    ))[:limit]
    baseline = list(dict.fromkeys(forced_keys + list(by_symbol)))[:limit]
    details = {
        "policy": "DEMO_ACTIVITY_V1", "execution_venue": "DEMO",
        "limit": limit, "eligible_count": len(by_symbol),
        "activity_source": ACTIVITY_SOURCE, "activity_status": "UNAVAILABLE",
        "activity_selected": [], "added_vs_liquidity": [], "displaced_vs_liquidity": [],
        "forced_unavailable": [s for s in forced if canonical_usdt_symbol(s) not in by_symbol],
    }
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    ranked = []
    if snapshot.get("status") == "OK" and snapshot.get("scanner_source") == ACTIVITY_SOURCE:
        try:
            observed = datetime.fromisoformat(snapshot["timestamp"].replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("Missing source timezone")
            age = float(now_ts) - observed.timestamp()
            fresh = math.isfinite(age) and 0 <= age <= max(0, float(max_age_seconds))
            details["activity_status"] = "FRESH" if fresh else "STALE_OR_FUTURE"
            details["snapshot_age_seconds"] = round(age, 3) if math.isfinite(age) else None
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            fresh = False
            details["activity_status"] = "INVALID_TIMESTAMP"
        if fresh:
            rows = snapshot.get("scanner_activity_candidates", [])
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                key = canonical_usdt_symbol(item.get("symbol"))
                try:
                    score = float(item.get("activity_score", 0))
                except (ValueError, TypeError, OverflowError):
                    continue
                if key in by_symbol and math.isfinite(score) and score > 0:
                    ranked.append((key, score))
            ranked.sort(key=lambda row: (-row[1], -by_symbol[row[0]][2], row[0]))
    reserved = min(max(0, int(activity_slots)), max(0, limit - len(forced_keys))) if ranked else 0
    core = baseline[:limit - reserved]
    chosen = []
    scores = {}
    for key, score in ranked:
        if key not in core and key not in chosen and len(chosen) < reserved:
            chosen.append(key)
            scores[key] = score
    keys = list(dict.fromkeys(core + chosen + baseline))[:limit]
    details.update({
        "activity_selected": [{"symbol": by_symbol[key][0], "score": scores[key]} for key in chosen],
        "added_vs_liquidity": [by_symbol[k][0] for k in keys if k not in baseline],
        "displaced_vs_liquidity": [by_symbol[k][0] for k in baseline if k not in keys],
        "selected_count": len(keys),
        "targets": [by_symbol[key][0] for key in keys],
    })
    return details["targets"], details


def subscription_symbols(targets, active_symbols, pending_symbols):
    """Keep managed positions subscribed even after discovery rotates them out."""
    return list(dict.fromkeys(
        key for symbol in list(targets) + sorted(active_symbols) + sorted(pending_symbols)
        if (key := canonical_usdt_symbol(symbol))
    ))
