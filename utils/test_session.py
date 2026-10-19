"""Persistent identity and configuration freeze for a 72-hour evidence run.

The session file is deliberately a small, secret-free JSON document on the
persistent volume.  It does not control trading.  It only separates cohorts
and makes configuration/code changes visible in the resulting evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.log_security import redact_secrets
from core.version import APP_VERSION, DEPLOYMENT_ID, GIT_BRANCH, GIT_COMMIT, GIT_REPO
from utils.runtime_health import RUN_ID


_lock = threading.RLock()
_DEFAULT_DATA_DIR = "." if os.name == "nt" else "/app/data"


def _utc_now(now=None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _data_dir() -> Path:
    configured = os.getenv("DATA_DIR", _DEFAULT_DATA_DIR)
    if os.name == "nt" and configured.replace("\\", "/") == "/app/data":
        configured = "data"
    return Path(configured).expanduser().resolve()


def _test_session_path() -> Path:
    configured = os.getenv("BOT_72H_TEST_SESSION_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else _data_dir() / "72h_test_session.json"


def _history_path(test_id: str) -> Path:
    return _data_dir() / "test_sessions" / f"{test_id}.json"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix="test_session_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def load_test_session(path=None) -> dict:
    target = Path(path) if path else _test_session_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _local_git_identity() -> dict:
    """Best-effort local identity; Railway source builds use environment IDs."""
    if GIT_COMMIT:
        return {
            "git_commit": GIT_COMMIT,
            "git_branch": GIT_BRANCH or None,
            "git_repo": GIT_REPO or None,
            "dirty_working_tree": False,
            "identity_source": "RAILWAY_GITHUB_DEPLOYMENT",
        }
    result = {
        "git_commit": None,
        "git_branch": None,
        "git_repo": None,
        "dirty_working_tree": None,
        "identity_source": "UNAVAILABLE",
    }
    try:
        root = Path(__file__).resolve().parents[1]
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=2, check=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=root, capture_output=True,
            text=True, timeout=2, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True,
            text=True, timeout=2, check=True,
        ).stdout.strip()
        result.update({
            "git_commit": commit or None,
            "git_branch": branch or None,
            "dirty_working_tree": bool(dirty),
            "identity_source": "LOCAL_GIT",
        })
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def code_identity() -> dict:
    return {
        "app_version": APP_VERSION,
        "deployment_id": DEPLOYMENT_ID or os.getenv("RAILWAY_DEPLOYMENT_ID") or None,
        **_local_git_identity(),
    }


def _safe_profile(state_manager=None) -> dict:
    from strategy.filter_profiles import get_filter_profile

    profile = get_filter_profile(state_manager)
    allowed = (
        "key", "label", "min_atr_pct", "min_rr", "max_ob_age", "require_ob",
        "require_liquidity_sweep", "require_sweep", "require_bos", "require_volume",
        "enable_fallback",
        "require_entry_sweep", "trigger_evidence_min", "allow_rr_fallback",
        "fallback_rr", "allow_countertrend_reversal",
    )
    return {key: profile.get(key) for key in allowed if key in profile}


def configuration_snapshot(state_manager=None) -> dict:
    """Whitelist trading-impacting values; never serialize the environment."""
    from core.config import Config

    state_get = state_manager.get if state_manager is not None else lambda key, default=None: default
    get_risk = getattr(state_manager, "get_risk_pct", None)
    get_leverage = getattr(state_manager, "get_leverage", None)
    get_max = getattr(state_manager, "get_max_open_trades", None)
    snapshot = {
        "captured_at": _utc_now(),
        "mode": "TESTNET" if bool(getattr(Config, "USE_TESTNET", False)) else "LIVE",
        "profile": _safe_profile(state_manager),
        "risk_percent": float(get_risk() if callable(get_risk) else getattr(Config, "RISK_PER_TRADE_PERCENT", 0)),
        "entry_margin_percent": float(getattr(Config, "ENTRY_MARGIN_PERCENT", 1.0)),
        "risk_model": {
            "structural_initial_stop_v1_enabled": bool(
                getattr(Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", False)
            ),
            "short_high_confidence_v1_enabled": bool(
                getattr(Config, "SHORT_HIGH_CONFIDENCE_V1_ENABLED", False)
            ),
            "atr_sl_multiplier": float(getattr(Config, "ATR_SL_MULTIPLIER", 1.5)),
            "max_sl_percent": float(getattr(Config, "MAX_SL_PCT", 0.05)),
            "max_sl_atr_multiple": float(getattr(Config, "MAX_SL_ATR_MULTIPLE", 4.0)),
            "max_notional_balance_fraction": float(getattr(Config, "MAX_NOTIONAL_BALANCE_PCT", 0.30)),
            "sizing_min_sl_distance_fraction": float(getattr(Config, "MIN_SL_DISTANCE_PCT", 0.003)),
        },
        "leverage": int(get_leverage() if callable(get_leverage) else getattr(Config, "LEVERAGE", 1)),
        "max_open_trades": int(get_max() if callable(get_max) else getattr(Config, "MAX_OPEN_TRADES", 1)),
        "scan_interval_seconds": int(state_get("scan_interval_seconds", getattr(Config, "SCAN_INTERVAL_SECONDS", 900))),
        "market_scan_limit": int(getattr(Config, "MARKET_SCAN_LIMIT", 50)),
        "sentiment_mode": str(state_get("sentiment_mode", "ta_only")),
        "ema_mode": str(state_get("ema_mode", "dual")),
        "automated_sentiment_enabled": bool(getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False)),
        "ai_news_sentiment_enabled": bool(getattr(Config, "AI_NEWS_SENTIMENT_ENABLED", False)),
        "ai_critique_enabled": bool(getattr(Config, "AI_CRITIQUE_ENABLED", False)),
        "entry_confirmation": {
            "timeframe": str(getattr(Config, "ENTRY_CONFIRMATION_TIMEFRAME", "5m")),
            "window_minutes": int(getattr(Config, "ENTRY_CONFIRMATION_WINDOW_MINUTES", 12)),
            "limit": int(getattr(Config, "ENTRY_CONFIRMATION_LIMIT", 80)),
        },
        "virtual_order_ttl_minutes": {
            "strict": int(getattr(Config, "VIRTUAL_ORDER_MAX_AGE_MINUTES_STRICT", 30)),
            "balanced": int(getattr(Config, "VIRTUAL_ORDER_MAX_AGE_MINUTES_MEDIUM", 20)),
            "relaxed": int(getattr(Config, "VIRTUAL_ORDER_MAX_AGE_MINUTES_RELAXED", 15)),
        },
        "cooldowns_minutes": {
            "timeout": int(getattr(Config, "SETUP_COOLDOWN_TIMEOUT_MINUTES", 60)),
            "invalidated": int(getattr(Config, "SETUP_COOLDOWN_INVALIDATED_MINUTES", 120)),
            "stop": int(getattr(Config, "SETUP_COOLDOWN_STOP_MINUTES", 240)),
            "manual": int(getattr(Config, "SETUP_COOLDOWN_MANUAL_MINUTES", 60)),
            "tp": int(getattr(Config, "SETUP_COOLDOWN_TP_MINUTES", 30)),
        },
        "risk_guards": {
            "daily_loss_limit_percent": float(getattr(Config, "DAILY_LOSS_LIMIT_PERCENT", 0)),
            "kill_switch_active": bool(state_get("kill_switch_active", False)),
            "sl_guard_enabled": bool(state_get("sl_guard_enabled", True)),
            "sl_guard_trade_limit": int(state_get("sl_guard_trade_limit", 3)),
            "sl_guard_pause_until": state_get("sl_guard_pause_until", None),
            "max_drawdown_enabled": bool(state_get("max_drawdown_enabled", True)),
            "max_drawdown_percent": float(state_get("max_drawdown_pct", 5.0)),
            "max_drawdown_pause_until": state_get("max_drawdown_pause_until", None),
            "low_profit_pairs_enabled": bool(state_get("low_profit_enabled", True)),
            "low_profit_loss_count": int(state_get("low_profit_loss_count", 2)),
        },
        "blacklisted_pairs": sorted(str(item) for item in (state_get("blacklisted_pairs", []) or [])),
        "trailing": {
            "tp_ladder_enabled": bool(getattr(Config, "TP_LADDER_ENABLED", True)),
            "move_sl_to_be_after_tp1": bool(getattr(Config, "MOVE_SL_TO_BE_AFTER_TP1", False)),
            "tp1_protection_percent": float(getattr(Config, "STEP_TRAIL_TP1_PROTECTION_PCT", 0.75)),
            "tp2_buffer_percent": float(getattr(Config, "STEP_TRAIL_TP2_BUFFER_PCT", 0.001)),
            "tp3_protection_percent": float(getattr(Config, "STEP_TRAIL_TP3_PROTECTION_PCT", 0.5)),
        },
        "derivatives_filter_mode": str(getattr(Config, "DERIVATIVES_FILTER_MODE", "off")),
    }
    return snapshot


def configuration_hash(snapshot: dict) -> str:
    stable = dict(snapshot or {})
    stable.pop("captured_at", None)
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _save_session(payload: dict) -> None:
    _atomic_write(_test_session_path(), payload)
    test_id = str(payload.get("test_id") or "unknown")
    _atomic_write(_history_path(test_id), payload)


def start_test_session(state_manager=None, *, source="TELEGRAM", now=None, force_new=False) -> dict:
    with _lock:
        current = load_test_session()
        if current.get("status") == "RUNNING" and not force_new:
            return {**current, "already_running": True}
        snapshot = configuration_snapshot(state_manager)
        identity = code_identity()
        payload = {
            "schema_version": 1,
            "test_id": f"test72_{uuid.uuid4().hex[:16]}",
            "status": "RUNNING",
            "started_at": _utc_now(now),
            "ended_at": None,
            "source": str(source),
            "initial_run_id": RUN_ID,
            "run_ids": [RUN_ID],
            "code_identity": identity,
            "configuration": snapshot,
            "configuration_hash": configuration_hash(snapshot),
            "changes": [],
            "interruptions": [],
        }
        _save_session(payload)
    from utils.decision_audit import audit_decision_event

    audit_decision_event(
        stage="TEST_SESSION", status="STARTED",
        details={
            "test_id": payload["test_id"], "started_at": payload["started_at"],
            "source": source, "code_identity": identity,
            "configuration": snapshot, "configuration_hash": payload["configuration_hash"],
        }, durable=True,
    )
    return payload


def join_active_test_session(state_manager=None) -> dict:
    """Attach a new process run and mark code/config drift as a new cohort."""
    with _lock:
        session = load_test_session()
        if session.get("status") not in ("RUNNING", "INTERRUPTED"):
            return session
        run_ids = list(session.get("run_ids") or [])
        if RUN_ID not in run_ids:
            run_ids.append(RUN_ID)
        session["run_ids"] = run_ids
        current_identity = code_identity()
        current_config = configuration_snapshot(state_manager)
        current_hash = configuration_hash(current_config)
        reasons = []
        old_commit = (session.get("code_identity") or {}).get("git_commit")
        new_commit = current_identity.get("git_commit")
        if old_commit and new_commit and old_commit != new_commit:
            reasons.append("CODE_COMMIT_CHANGED")
        if session.get("configuration_hash") and session.get("configuration_hash") != current_hash:
            reasons.append("CONFIGURATION_DRIFT")
        if reasons:
            session["status"] = "INTERRUPTED"
            session.setdefault("interruptions", []).append({
                "timestamp": _utc_now(), "run_id": RUN_ID, "reasons": reasons,
                "code_identity": current_identity, "configuration_hash": current_hash,
            })
        _save_session(session)
    from utils.decision_audit import audit_decision_event

    audit_decision_event(
        stage="TEST_SESSION",
        status="RUN_JOINED" if not reasons else "INTERRUPTED_NEW_COHORT_REQUIRED",
        details={"test_id": session.get("test_id"), "reasons": reasons},
        durable=True,
    )
    return session


def record_config_change(setting, old, new, *, source="RUNTIME", state_manager=None) -> dict:
    event = {
        "timestamp": _utc_now(), "setting": str(setting), "old": old, "new": new,
        "source": str(source), "run_id": RUN_ID,
    }
    from utils.decision_audit import audit_decision_event

    audit_decision_event(stage="CONFIG_CHANGED", status="CHANGED", details=event, durable=True)
    with _lock:
        session = load_test_session()
        if session.get("status") in ("RUNNING", "INTERRUPTED"):
            session.setdefault("changes", []).append(event)
            session["changes"] = session["changes"][-200:]
            session["status"] = "INTERRUPTED"
            session.setdefault("interruptions", []).append({
                "timestamp": event["timestamp"], "run_id": RUN_ID,
                "reasons": [f"CONFIG_CHANGED:{setting}"],
            })
            _save_session(session)
    return event


def record_run_start(state_manager, runtime_session: dict) -> dict:
    from utils.decision_audit import audit_decision_event

    snapshot = configuration_snapshot(state_manager)
    details = {
        "process_session": (runtime_session or {}).get("current", {}),
        "previous_process_session": (runtime_session or {}).get("previous"),
        "unexpected_restart": bool((runtime_session or {}).get("unexpected_restart")),
        "deployment_overlap": bool((runtime_session or {}).get("deployment_overlap")),
        "code_identity": code_identity(),
        "configuration": snapshot,
        "configuration_hash": configuration_hash(snapshot),
    }
    audit_decision_event(stage="RUN_SESSION", status="STARTED", details=details, durable=True)
    return join_active_test_session(state_manager)


def record_run_end(reason="NORMAL") -> None:
    from utils.decision_audit import audit_decision_event

    audit_decision_event(
        stage="RUN_SESSION", status="GRACEFUL_END",
        details={"reason": str(reason), "ended_at": _utc_now()}, durable=True,
    )


def complete_test_session(test_id=None, *, source="EVIDENCE_EXPORT", now=None) -> dict:
    """Close a cohort only after its evidence was delivered successfully."""
    with _lock:
        session = load_test_session()
        if not session:
            return {}
        if test_id and str(session.get("test_id")) != str(test_id):
            return {**session, "completion_refused": "TEST_ID_MISMATCH"}
        previous_status = str(session.get("status") or "UNKNOWN")
        if previous_status not in ("RUNNING", "INTERRUPTED"):
            return {**session, "already_completed": True}
        session["status"] = (
            "COMPLETED" if previous_status == "RUNNING" else "COMPLETED_INTERRUPTED"
        )
        session["status_before_completion"] = previous_status
        session["ended_at"] = _utc_now(now)
        session["completion_source"] = str(source)
        _save_session(session)

    from utils.decision_audit import audit_decision_event

    audit_decision_event(
        stage="TEST_SESSION",
        status=session["status"],
        details={
            "test_id": session.get("test_id"),
            "ended_at": session.get("ended_at"),
            "status_before_completion": previous_status,
            "source": source,
        },
        durable=True,
    )
    return session


def sanitized_session_text(payload: dict) -> str:
    return redact_secrets(json.dumps(payload or {}, ensure_ascii=False, indent=2, sort_keys=True))
