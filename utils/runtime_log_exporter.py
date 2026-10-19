import gzip
import heapq
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.log_security import redact_secrets
from core.logger import get_log_dir, logger


@dataclass
class ExportResult:
    paths: list
    summary: str
    no_data: bool = False
    temp_dir: str = ""


_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})Z\s+-")


def _parse_log_timestamp(line):
    match = _LOG_TS.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _iter_log_records(log_dir: Path, cutoff: datetime, now: datetime):
    """دمج ملفات السجل زمنياً دون تحميل محتواها كله في الذاكرة."""
    paths = [
        path for path in sorted(log_dir.glob("bot_activity.log*"), key=lambda p: p.name)
        if path.is_file()
    ]

    def _records(path, source_index):
        current_ts = None
        try:
            with _open_text(path) as handle:
                for line_index, raw in enumerate(handle):
                    parsed = _parse_log_timestamp(raw)
                    if parsed is not None:
                        current_ts = parsed
                    if current_ts is not None and cutoff <= current_ts <= now:
                        yield (
                            current_ts,
                            source_index,
                            line_index,
                            redact_secrets(raw.rstrip("\n")),
                        )
        except (OSError, gzip.BadGzipFile) as exc:
            logger.warning(f"[RuntimeExport] تعذر قراءة {path.name}: {exc}")

    iterators = [_records(path, index) for index, path in enumerate(paths)]
    if iterators:
        yield from heapq.merge(*iterators)


def _iter_audit_records(log_dir: Path, cutoff: datetime, now: datetime):
    """قراءة decision audit كسجل متدفق مرتب زمنياً وبذاكرة ثابتة تقريباً."""
    paths = [
        path for path in sorted(log_dir.glob("decision_audit.jsonl*"), key=lambda p: p.name)
        if path.is_file()
    ]

    def _records(path, source_index):
        try:
            with _open_text(path) as handle:
                for line_index, raw in enumerate(handle):
                    try:
                        event = json.loads(raw)
                        timestamp = datetime.fromisoformat(
                            str(event.get("timestamp", "")).replace("Z", "+00:00")
                        )
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=timezone.utc)
                        timestamp = timestamp.astimezone(timezone.utc)
                        if cutoff <= timestamp <= now:
                            safe_line = redact_secrets(
                                json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                            )
                            safe_event = json.loads(safe_line)
                            yield (
                                timestamp,
                                source_index,
                                line_index,
                                safe_line,
                                safe_event,
                            )
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
        except (OSError, gzip.BadGzipFile) as exc:
            logger.warning(f"[RuntimeExport] تعذر قراءة {path.name}: {exc}")

    iterators = [_records(path, index) for index, path in enumerate(paths)]
    if iterators:
        yield from heapq.merge(*iterators)


def _update_decision_aggregate(aggregated, event):
    """تحديث ملخص قرار واحد؛ صالح للاستخدام المتدفق وبناء الاختبارات القديمة."""
    decision_id = event.get("decision_id")
    if not decision_id:
        return
    row = aggregated.setdefault(decision_id, {
            "decision_id": decision_id,
            "run_id": event.get("run_id"),
            "signal_id": event.get("signal_id") or decision_id,
            "setup_id": event.get("setup_id") or event.get("thesis_id"),
            "scan_id": event.get("scan_id"),
            "timestamp": event.get("timestamp"),
            "symbol": event.get("symbol"),
            "15m_signal": None,
            "htf_trend": None,
            "sentiment_source": None,
            "sentiment_result": None,
            "quality_filter": None,
            "derivatives_filter": None,
            "rr_filter": None,
            "sniper_result": None,
            "entry_confirmation": None,
            "entry_plan_revalidation": None,
            "setup_score": None,
            "candidate_state": None,
            "candidate_gate_class": None,
            "pipeline_progress_pct": None,
            "candidate_score": None,
            "candidate_score_band": None,
            "evidence_coverage_pct": None,
            "next_trigger": None,
            "setup_thesis_id": None,
            "virtual_order_id": event.get("virtual_order_id"),
            "exchange_order_id": event.get("exchange_order_id"),
            "position_id": event.get("position_id"),
            "market_regime": None,
            "target_type": None,
            "planned_rr": None,
            "entry_mode": None,
            "entry_zone_touch": None,
            "invalidation_state": None,
            "mfe_r": None,
            "mae_r": None,
            "final_r": None,
            "cooldown_result": None,
            "final_outcome": None,
            "trade_id": event.get("trade_id"),
        })
    stage = str(event.get("stage", ""))
    status = str(event.get("status", ""))
    details = event.get("details") or {}
    row["timestamp"] = event.get("timestamp") or row["timestamp"]
    row["run_id"] = event.get("run_id") or row.get("run_id")
    row["trade_id"] = event.get("trade_id") or row["trade_id"]
    for key in (
        "signal_id", "setup_id", "virtual_order_id", "exchange_order_id",
        "position_id",
    ):
        row[key] = event.get(key) or row.get(key)
    if stage == "HYBRID_DECISION":
        row["15m_signal"] = details.get("technical_signal")
        row["htf_trend"] = details.get("htf_trend")
        row["sentiment_source"] = details.get("sentiment_source")
        row["sentiment_result"] = details.get("sentiment_result")
        row["setup_score"] = details.get("setup_score")
        row["setup_thesis_id"] = details.get("setup_thesis_id")
        row["setup_id"] = row["setup_id"] or row["setup_thesis_id"]
        regime = details.get("market_regime") or {}
        row["market_regime"] = (
            regime.get("label") if isinstance(regime, dict) else regime
        )
        # BUY/SELL/HOLD is the signal, not the terminal result.  A later
        # FINAL_OUTCOME/SNIPER/EXECUTION event owns this field.
    elif stage == "QUALITY_FILTER":
        row["quality_filter"] = status
    elif stage == "DERIVATIVES_FILTER":
        row["derivatives_filter"] = status
    elif stage == "RR_FILTER":
        row["rr_filter"] = status
        target_plan = details.get("target_plan") or {}
        row["target_type"] = target_plan.get("target_type")
        row["planned_rr"] = target_plan.get("rr")
    elif stage == "ENTRY_CONFIRMATION":
        row["entry_confirmation"] = status
        confirmation = details.get("confirmation") or {}
        row["setup_score"] = confirmation.get("score", row["setup_score"])
        row["setup_thesis_id"] = details.get("setup_thesis_id") or row["setup_thesis_id"]
    elif stage == "ENTRY_PLAN_REVALIDATION":
        row["entry_plan_revalidation"] = status
    elif stage == "CANDIDATE_STATE":
        row["candidate_state"] = status
        row["candidate_gate_class"] = details.get("gate_class")
        row["pipeline_progress_pct"] = details.get("pipeline_progress_pct")
        row["candidate_score"] = details.get("candidate_score")
        row["candidate_score_band"] = details.get("candidate_score_band")
        row["evidence_coverage_pct"] = details.get("evidence_coverage_pct")
        row["next_trigger"] = details.get("next_trigger")
    elif stage == "SETUP_COOLDOWN":
        row["cooldown_result"] = status
    elif stage == "ENTRY_ZONE_TOUCH":
        row["entry_zone_touch"] = status
        row["entry_mode"] = "CLOSED_5M_CONFIRMATION"
    elif stage == "INVALIDATION_STATE":
        row["invalidation_state"] = status
    elif stage == "SNIPER":
        row["sniper_result"] = status
        row["final_outcome"] = status
    elif stage in ("FINAL_OUTCOME", "EXECUTION"):
        row["final_outcome"] = status
        if stage == "EXECUTION" and status in ("EXECUTED", "TRADE_EXECUTED"):
            row["entry_mode"] = "UNIFIED_CLOSED_5M"
            row["market_regime"] = details.get("market_regime") or row["market_regime"]
    elif stage == "POSITION_CLOSED":
        row["final_outcome"] = status
        row["mfe_r"] = details.get("mfe_r")
        row["mae_r"] = details.get("mae_r")
        row["final_r"] = details.get("final_r")
        row["market_regime"] = details.get("market_regime") or row["market_regime"]
    elif stage == "MOMENTUM_CONTINUATION":
        row["entry_mode"] = "FRESH_CONTINUATION_SHADOW"
        row["final_outcome"] = status
        plan = details.get("plan") or {}
        setup = details.get("setup") or {}
        row["setup_thesis_id"] = (
            setup.get("setup_id") or row.get("setup_thesis_id")
        )
        row["setup_id"] = row.get("setup_id") or row["setup_thesis_id"]
        row["target_type"] = plan.get("target_type") or row.get("target_type")
        row["planned_rr"] = plan.get("net_rr", row.get("planned_rr"))
        outcome = details.get("outcome") or {}
        row["mfe_r"] = outcome.get("mfe_r", row.get("mfe_r"))
        row["mae_r"] = outcome.get("mae_r", row.get("mae_r"))
        row["final_r"] = outcome.get("final_r", row.get("final_r"))


def _new_stream_stats():
    return {
        "scan_ids": set(),
        "signal_ids": set(),
        "executed_ids": set(),
        "rejections": Counter(),
        "warnings": 0,
        "errors": 0,
        "seen_events": set(),
        "raw_signals": set(),
        "independent_setups": set(),
        "quality_pass": set(),
        "rr_pass": set(),
        "entry_zone_touches": set(),
        "confirmation_pass": set(),
        "executed_positions": set(),
        "closed_positions": set(),
        "final_rs": [],
        "momentum_setups": set(),
        "momentum_paired_setups": set(),
        "momentum_paired_confirmations": set(),
        "momentum_paired_qualified_plans": set(),
        "momentum_paired_terminal_plans": set(),
        "momentum_paired_final_rs": [],
        "momentum_confirmations": set(),
        "momentum_qualified_plans": set(),
        "momentum_terminal_plans": set(),
        "momentum_final_rs": [],
        "active_altcoin_setups": set(),
        "active_altcoin_qualified_plans": set(),
        "active_altcoin_terminal_plans": set(),
        "active_altcoin_final_rs": [],
        "proposal_ids": set(),
        "proposal_statuses": Counter(),
        "execution_timeline_statuses": Counter(),
    }


def _update_stream_log_stats(stats, line):
    stats["warnings"] += int(" - WARNING - " in line)
    stats["errors"] += int(
        " - ERROR - " in line or " - CRITICAL - " in line
    )


def _event_identity(event, fields):
    return next((event.get(field) for field in fields if event.get(field)), None)


def _update_stream_event_stats(stats, event):
    event_id = event.get("event_id")
    if event_id and event_id in stats["seen_events"]:
        return False
    if event_id:
        stats["seen_events"].add(event_id)

    stage = str(event.get("stage", ""))
    status = str(event.get("status", "")).upper()
    decision_identity = _event_identity(event, ("decision_id", "event_id"))

    if stage == "USER_VISIBLE_TRADE_PROPOSAL":
        details = event.get("details") or {}
        proposal_id = details.get("proposal_id") or event.get("trade_id") or decision_identity
        if proposal_id:
            stats["proposal_ids"].add(str(proposal_id))
        stats["proposal_statuses"][status or "UNKNOWN"] += 1
    if stage == "EXECUTION_TIMELINE":
        stats["execution_timeline_statuses"][status or "UNKNOWN"] += 1

    if stage in ("SCAN_STARTED", "SCAN_ROUND") and status in ("", "STARTED"):
        if event.get("scan_id"):
            stats["scan_ids"].add(event.get("scan_id"))
    if stage in ("HYBRID_DECISION", "TECHNICAL_SIGNAL") and status in ("BUY", "SELL"):
        if decision_identity:
            stats["signal_ids"].add(decision_identity)
    if "REJECT" in status or "CANCEL" in status or "EXPIRED" in status:
        stats["rejections"][f"{stage or 'UNKNOWN'}:{status}"] += 1
    if stage == "EXECUTION" and status in ("EXECUTED", "TRADE_EXECUTED"):
        execution_id = _event_identity(event, ("trade_id", "decision_id", "event_id"))
        if execution_id:
            stats["executed_ids"].add(execution_id)

    mappings = (
        ("HYBRID_DECISION", ("BUY", "SELL"), "raw_signals", ("decision_id", "event_id")),
        ("SETUP_CREATED", None, "independent_setups", ("setup_id", "thesis_id")),
        ("QUALITY_FILTER", ("PASS",), "quality_pass", ("decision_id", "event_id")),
        ("RR_FILTER", ("PASS",), "rr_pass", ("decision_id", "event_id")),
        ("ENTRY_ZONE_TOUCH", ("TOUCHED",), "entry_zone_touches", ("virtual_order_id", "decision_id")),
        ("ENTRY_CONFIRMATION", ("PASS",), "confirmation_pass", ("virtual_order_id", "decision_id")),
        ("EXECUTION", ("EXECUTED", "TRADE_EXECUTED"), "executed_positions", ("position_id", "trade_id")),
    )
    for wanted_stage, statuses, bucket, fields in mappings:
        if stage != wanted_stage or (statuses and status not in statuses):
            continue
        identity = _event_identity(event, fields)
        if identity:
            stats[bucket].add(identity)

    if stage == "POSITION_CLOSED":
        identity = _event_identity(event, ("position_id", "trade_id"))
        if identity:
            stats["closed_positions"].add(identity)
        try:
            stats["final_rs"].append(float((event.get("details") or {}).get("final_r")))
        except (TypeError, ValueError):
            pass
    if stage == "MOMENTUM_CONTINUATION":
        details = event.get("details") or {}
        setup_id = _event_identity(event, ("setup_id", "thesis_id", "decision_id"))
        plan = details.get("plan") or {}
        setup = details.get("setup") or {}
        plan_id = plan.get("plan_id") or event.get("virtual_order_id") or setup_id
        source = str(
            plan.get("source")
            or setup.get("source")
            or details.get("source")
            or ""
        ).upper()
        paired_smc = source == "SMC_RR_PASS_PAIRED_SHADOW"
        if status in {"SETUP_CREATED", "SETUP_CREATED_EXTERNAL_SMC"} and setup_id:
            stats["momentum_setups"].add(setup_id)
        if status == "SETUP_CREATED_EXTERNAL_SMC" and setup_id:
            stats["momentum_paired_setups"].add(setup_id)
        if status == "CONFIRMATION_PASS" and setup_id:
            stats["momentum_confirmations"].add(setup_id)
            if paired_smc:
                stats["momentum_paired_confirmations"].add(setup_id)
        if status == "PLAN_QUALIFIED_SHADOW" and plan_id:
            stats["momentum_qualified_plans"].add(plan_id)
            if paired_smc:
                stats["momentum_paired_qualified_plans"].add(plan_id)
        if status.startswith("OUTCOME_") and plan_id:
            stats["momentum_terminal_plans"].add(plan_id)
            if paired_smc:
                stats["momentum_paired_terminal_plans"].add(plan_id)
            try:
                final_r = float((details.get("outcome") or {}).get("final_r"))
                if final_r == final_r:
                    stats["momentum_final_rs"].append(final_r)
                    if paired_smc:
                        stats["momentum_paired_final_rs"].append(final_r)
            except (TypeError, ValueError):
                pass
    if stage == "ACTIVE_ALTCOIN_SHADOW":
        details = event.get("details") or {}
        setup_id = _event_identity(event, ("setup_id", "thesis_id", "decision_id"))
        plan = details.get("plan") or {}
        plan_id = plan.get("plan_id") or event.get("virtual_order_id") or setup_id
        if status == "SETUP_CREATED" and setup_id:
            stats["active_altcoin_setups"].add(setup_id)
        if status == "PLAN_QUALIFIED_SHADOW" and plan_id:
            stats["active_altcoin_qualified_plans"].add(plan_id)
        if status.startswith("OUTCOME_") and plan_id:
            stats["active_altcoin_terminal_plans"].add(plan_id)
            try:
                net_r = float((details.get("outcome") or {}).get("net_r"))
                if net_r == net_r:
                    stats["active_altcoin_final_rs"].append(net_r)
            except (TypeError, ValueError):
                pass
    return True


def _build_stream_summary(stats, cutoff, now, window_title):
    reasons = "\n".join(
        f"- {reason}: {count}" for reason, count in stats["rejections"].most_common()
    ) or "- لا توجد حالات رفض مسجلة في decision_audit"
    return (
        f"Runtime Logs — Last {window_title}\n"
        f"UTC range: {cutoff.isoformat()} -> {now.isoformat()}\n"
        f"Scan rounds: {len(stats['scan_ids'])}\n"
        f"Technical signals (BUY/SELL): {len(stats['signal_ids'])}\n"
        f"Independent setup theses: {len(stats['independent_setups'])}\n"
        f"Executed trades: {len(stats['executed_ids'])}\n"
        f"User-visible trade proposals: {len(stats['proposal_ids'])}\n"
        f"Momentum shadow setups: {len(stats['momentum_setups'])}\n"
        f"Momentum paired SMC setups: {len(stats['momentum_paired_setups'])}\n"
        f"Momentum paired SMC confirmations: {len(stats['momentum_paired_confirmations'])}\n"
        f"Momentum paired SMC qualified plans: {len(stats['momentum_paired_qualified_plans'])}\n"
        f"Momentum paired SMC terminal plans: {len(stats['momentum_paired_terminal_plans'])}\n"
        f"Momentum shadow confirmations: {len(stats['momentum_confirmations'])}\n"
        f"Momentum qualified shadow plans: {len(stats['momentum_qualified_plans'])}\n"
        f"Momentum terminal shadow plans: {len(stats['momentum_terminal_plans'])}\n"
        f"Active-altcoin shadow setups: {len(stats['active_altcoin_setups'])}\n"
        f"Active-altcoin qualified plans: {len(stats['active_altcoin_qualified_plans'])}\n"
        f"Active-altcoin terminal plans: {len(stats['active_altcoin_terminal_plans'])}\n"
        f"Warning log lines: {stats['warnings']}\n"
        f"Error/Critical log lines: {stats['errors']}\n"
        "Rejection/terminal outcomes:\n"
        f"{reasons}\n"
    )


def _build_stream_funnel(stats):
    final_rs = stats["final_rs"]
    momentum_final_rs = stats["momentum_final_rs"]
    momentum_paired_final_rs = stats["momentum_paired_final_rs"]
    active_altcoin_final_rs = stats["active_altcoin_final_rs"]
    return {
        "schema_version": 1,
        "raw_signals": len(stats["raw_signals"]),
        "independent_setups": len(stats["independent_setups"]),
        "quality_pass": len(stats["quality_pass"]),
        "rr_pass": len(stats["rr_pass"]),
        "entry_zone_touches": len(stats["entry_zone_touches"]),
        "confirmation_pass": len(stats["confirmation_pass"]),
        "executed_positions": len(stats["executed_positions"]),
        "closed_positions": len(stats["closed_positions"]),
        "user_visible_trade_proposals": len(stats["proposal_ids"]),
        "proposal_event_statuses": dict(sorted(stats["proposal_statuses"].items())),
        "execution_timeline_statuses": dict(
            sorted(stats["execution_timeline_statuses"].items())
        ),
        "average_final_r": (sum(final_rs) / len(final_rs)) if final_rs else None,
        "positive_final_r": sum(value > 0 for value in final_rs),
        "negative_final_r": sum(value < 0 for value in final_rs),
        "momentum_shadow_setups": len(stats["momentum_setups"]),
        "momentum_paired_smc_setups": len(stats["momentum_paired_setups"]),
        "momentum_paired_smc_confirmations": len(stats["momentum_paired_confirmations"]),
        "momentum_paired_smc_qualified_plans": len(stats["momentum_paired_qualified_plans"]),
        "momentum_paired_smc_terminal_plans": len(stats["momentum_paired_terminal_plans"]),
        "momentum_paired_smc_average_final_r": (
            sum(momentum_paired_final_rs) / len(momentum_paired_final_rs)
            if momentum_paired_final_rs else None
        ),
        "momentum_shadow_confirmations": len(stats["momentum_confirmations"]),
        "momentum_shadow_qualified_plans": len(stats["momentum_qualified_plans"]),
        "momentum_shadow_terminal_plans": len(stats["momentum_terminal_plans"]),
        "momentum_shadow_average_final_r": (
            sum(momentum_final_rs) / len(momentum_final_rs)
            if momentum_final_rs else None
        ),
        "active_altcoin_shadow_setups": len(stats["active_altcoin_setups"]),
        "active_altcoin_shadow_qualified_plans": len(stats["active_altcoin_qualified_plans"]),
        "active_altcoin_shadow_terminal_plans": len(stats["active_altcoin_terminal_plans"]),
        "active_altcoin_shadow_average_final_r": (
            sum(active_altcoin_final_rs) / len(active_altcoin_final_rs)
            if active_altcoin_final_rs else None
        ),
    }


def _flush_logger_handlers():
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _export_temp_root(source_dir: Path) -> str:
    explicit = os.getenv("BOT_EXPORT_TMP_DIR", "").strip()
    if explicit:
        root = Path(explicit)
    elif source_dir.name.lower() == "logs":
        root = source_dir.parent / "tmp"
    else:
        root = source_dir / "tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    except OSError:
        return tempfile.gettempdir()


def export_runtime_logs_zip(
    hours=24,
    minutes=None,
    log_dir=None,
    now=None,
    max_zip_bytes=None,
    start_time=None,
    state_manager=None,
    client=None,
) -> ExportResult:
    """تصدير متدفق إلى القرص؛ لا يحتفظ بالسجل أو JSONL كاملاً في RAM."""
    from utils.runtime_health import log_memory_milestone

    if minutes is None:
        hours = int(hours)
        if hours < 1 or hours > 168:
            raise ValueError("hours must be between 1 and 168")
        window_delta = timedelta(hours=hours)
        window_tag = f"{hours}h"
        window_title = f"{hours} Hours"
        window_ar = f"{hours} ساعة"
        window_details = {"hours": hours}
    else:
        minutes = int(minutes)
        if minutes < 1 or minutes > 168 * 60:
            raise ValueError("minutes must be between 1 and 10080")
        window_delta = timedelta(minutes=minutes)
        window_tag = f"{minutes}m"
        window_title = f"{minutes} Minutes"
        window_ar = f"{minutes} دقيقة"
        window_details = {"minutes": minutes}

    log_memory_milestone("LOG_EXPORT_START", force=True, details=window_details)
    _flush_logger_handlers()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if start_time is not None:
        cutoff = start_time
        if isinstance(cutoff, str):
            cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        cutoff = cutoff.astimezone(timezone.utc)
        if cutoff > now:
            raise ValueError("start_time cannot be after now")
    else:
        cutoff = now - window_delta
    source_dir = Path(log_dir or get_log_dir())
    if not source_dir.exists():
        log_memory_milestone(
            "LOG_EXPORT_END", force=True,
            details={**window_details, "no_data": True},
        )
        return ExportResult(
            paths=[], summary=f"لا توجد سجلات متاحة خلال آخر {window_ar}.", no_data=True
        )

    limit = int(
        max_zip_bytes
        or os.getenv("TELEGRAM_LOG_ZIP_MAX_BYTES", str(45 * 1024 * 1024))
    )
    payload_limit = max(1024, int(limit * 0.85))
    temp_dir = tempfile.mkdtemp(
        prefix=f"runtime_logs_{window_tag}_",
        dir=_export_temp_root(source_dir),
    )
    timestamp = now.strftime("%Y%m%d_%H%M")
    current = None
    chunks = []
    stats = _new_stream_stats()
    record_count = 0

    def _new_chunk():
        index = len(chunks) + 1
        zip_path = os.path.join(
            temp_dir, f"runtime_logs_{window_tag}_{timestamp}_part{index:02d}.zip"
        )
        archive = zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        )
        return {
            "zip_path": zip_path,
            "archive": archive,
            # Stream records directly into the compressed entries.  Keeping
            # uncompressed staging files here can require a second full copy
            # of the retention window and was the cause of 72h export ENOSPC.
            # zipfile permits only one active write handle per archive, so the
            # exporter opens the log entry first and switches to the audit
            # entry after the log stream is complete.
            "log_handle": None,
            "audit_handle": None,
            "active_kind": None,
            "log_written": False,
            "audit_written": False,
            "size": 0,
            "decisions": {},
        }

    def _finish_chunk():
        nonlocal current
        if current is None:
            return
        for key in ("log_handle", "audit_handle"):
            handle = current.get(key)
            if handle is not None:
                handle.close()
                current[key] = None
        current["archive"].close()
        chunks.append(current)
        current = None

    def _ensure_stream_handle(kind):
        """Open one ZIP member at a time (ZipFile forbids concurrent writers)."""
        key = "log_handle" if kind == "log" else "audit_handle"
        if current["active_kind"] == kind and current[key] is not None:
            return current[key]
        previous_kind = current.get("active_kind")
        if previous_kind:
            previous_key = "log_handle" if previous_kind == "log" else "audit_handle"
            previous_handle = current.get(previous_key)
            if previous_handle is not None:
                previous_handle.close()
                current[previous_key] = None
        if current.get(key) is not None:
            raise RuntimeError(f"لا يمكن إعادة فتح عضو ZIP الجاري: {kind}")
        member = (
            f"bot_activity_{window_tag}.log"
            if kind == "log"
            else f"decision_audit_{window_tag}.jsonl"
        )
        current[key] = current["archive"].open(member, "w")
        current["log_written" if kind == "log" else "audit_written"] = True
        current["active_kind"] = kind
        return current[key]

    def _write_record(kind, line, event=None):
        nonlocal current, record_count
        encoded_size = len(line.encode("utf-8")) + 1
        if current is None:
            current = _new_chunk()
        if current["size"] and current["size"] + encoded_size > payload_limit:
            _finish_chunk()
            current = _new_chunk()
        handle = _ensure_stream_handle(kind)
        handle.write((line + "\n").encode("utf-8"))
        current["size"] += encoded_size
        record_count += 1
        if event is not None:
            _update_decision_aggregate(current["decisions"], event)

    try:
        try:
            os.chmod(temp_dir, 0o700)
        except OSError:
            pass

        for _, _, _, line in _iter_log_records(source_dir, cutoff, now):
            _update_stream_log_stats(stats, line)
            _write_record("log", line)

        for _, _, _, line, event in _iter_audit_records(source_dir, cutoff, now):
            if not _update_stream_event_stats(stats, event):
                continue
            _write_record("audit", line, event=event)
        _finish_chunk()

        if not record_count:
            shutil.rmtree(temp_dir, ignore_errors=True)
            log_memory_milestone(
                "LOG_EXPORT_END", force=True,
                details={**window_details, "no_data": True},
            )
            return ExportResult(
                paths=[],
                summary=f"لا توجد سجلات متاحة خلال آخر {window_ar}.",
                no_data=True,
            )

        summary = redact_secrets(
            _build_stream_summary(stats, cutoff, now, window_title)
        )
        funnel = _build_stream_funnel(stats)
        funnel_summary = redact_secrets(json.dumps(
            funnel, ensure_ascii=False, indent=2
        ))
        operational_snapshot = None
        lifecycle_payload = ""
        review_summary = {
            "schema_version": 1,
            "window": {
                "label": window_title,
                "utc_start": cutoff.isoformat(),
                "utc_end": now.isoformat(),
            },
            "funnel": funnel,
        }
        if state_manager is not None:
            from utils.operational_snapshot import (
                build_operational_snapshot,
                flatten_proposal_timelines,
            )

            operational_snapshot = build_operational_snapshot(
                state_manager, client=client
            )
            lifecycle_rows = flatten_proposal_timelines(operational_snapshot)
            lifecycle_rows_in_window = []
            for row in lifecycle_rows:
                try:
                    row_time = datetime.fromisoformat(
                        str(row.get("timestamp") or "").replace("Z", "+00:00")
                    )
                    if row_time.tzinfo is None:
                        row_time = row_time.replace(tzinfo=timezone.utc)
                    row_time = row_time.astimezone(timezone.utc)
                    if cutoff <= row_time <= now:
                        lifecycle_rows_in_window.append(row)
                except (TypeError, ValueError):
                    # Keep undated legacy records visible rather than silently
                    # losing evidence that predates the timeline schema.
                    lifecycle_rows_in_window.append(row)
            lifecycle_payload = "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in lifecycle_rows_in_window
            )
            review_summary["proposal_lifecycle_rows_in_window"] = len(
                lifecycle_rows_in_window
            )
            review_summary["current_runtime"] = {
                "runtime": operational_snapshot.get("runtime"),
                "mode": operational_snapshot.get("mode"),
                "endpoint": operational_snapshot.get("endpoint"),
                "reconciliation": operational_snapshot.get("reconciliation"),
                "proposal_summary": operational_snapshot.get("proposal_summary"),
                "protection_summary": operational_snapshot.get("protection_summary"),
                "warnings": operational_snapshot.get("warnings"),
            }
        operational_payload = redact_secrets(json.dumps(
            operational_snapshot or {
                "schema_version": 1,
                "status": "UNAVAILABLE",
                "reason": "state_manager was not supplied to the exporter",
            },
            ensure_ascii=False,
            indent=2,
        ))
        lifecycle_payload = redact_secrets(lifecycle_payload)
        review_payload = redact_secrets(json.dumps(
            review_summary, ensure_ascii=False, indent=2
        ))
        review_guide = (
            "دليل مراجعة سجل التشغيل\n"
            "========================\n"
            "1) ابدأ بـ review_summary.json لمعرفة Funnel وحالة التشغيل الحالية.\n"
            "2) operational_snapshot.json لقطة آمنة للحالة والمراكز والحماية والمصالحة.\n"
            "3) proposal_lifecycle.jsonl يربط كل Proposal مرئية بمراحل التنفيذ والإغلاق.\n"
            "4) decision_summary_*.jsonl ملخص Thesis/Decision، وdecision_audit_*.jsonl المصدر التفصيلي.\n"
            "5) bot_activity_*.log للتسلسل النصي والأخطاء والتحذيرات.\n"
            "تنبيه: Operational snapshot يمثل لحظة إنشاء الملف، أما بقية السجل فتغطي النافذة المطلوبة.\n"
            "جميع المخرجات تمر عبر حجب الأسرار قبل إضافتها إلى ZIP.\n"
        )
        output_paths = []
        log_memory_milestone(
            "ZIP_START", force=True,
            details={**window_details, "parts": len(chunks)},
        )
        for index, chunk in enumerate(chunks, start=1):
            decision_payload = []
            for row in chunk["decisions"].values():
                decision_payload.append(
                    redact_secrets(json.dumps(row, ensure_ascii=False))
                )
            decision_payload = "\n".join(decision_payload)
            if decision_payload:
                decision_payload += "\n"

            # The raw entries are already closed.  Append the small derived
            # summaries without recreating the raw staging files.
            with zipfile.ZipFile(
                chunk["zip_path"], "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                # Preserve the established per-part ZIP contract even when
                # the payload boundary falls between the log and audit
                # streams: an empty member is preferable to a missing member
                # for Telegram clients and downstream parsers.
                if not chunk["log_written"]:
                    archive.writestr(f"bot_activity_{window_tag}.log", "")
                if not chunk["audit_written"]:
                    archive.writestr(f"decision_audit_{window_tag}.jsonl", "")
                archive.writestr(
                    f"decision_summary_{window_tag}.jsonl", decision_payload
                )
                archive.writestr("funnel_summary.json", funnel_summary)
                archive.writestr("review_summary.json", review_payload)
                archive.writestr("operational_snapshot.json", operational_payload)
                archive.writestr("proposal_lifecycle.jsonl", lifecycle_payload)
                archive.writestr("REVIEW_GUIDE_AR.md", review_guide)
                archive.writestr("summary.txt", summary)

            zip_path = chunk["zip_path"]
            if len(chunks) == 1:
                final_path = os.path.join(
                    temp_dir, f"runtime_logs_{window_tag}_{timestamp}.zip"
                )
                os.replace(zip_path, final_path)
                zip_path = final_path
            if os.path.getsize(zip_path) > limit:
                raise RuntimeError(
                    f"جزء السجل {os.path.basename(zip_path)} تجاوز الحد بعد الضغط؛ "
                    "خفّض TELEGRAM_LOG_ZIP_MAX_BYTES"
                )
            output_paths.append(zip_path)
        log_memory_milestone(
            "ZIP_END", force=True,
            details={**window_details, "parts": len(output_paths)},
        )
        log_memory_milestone(
            "LOG_EXPORT_END", force=True,
            details={**window_details, "records": record_count},
        )
        return ExportResult(paths=output_paths, summary=summary, temp_dir=temp_dir)
    except Exception:
        if current is not None:
            for key in ("log_handle", "audit_handle"):
                try:
                    handle = current.get(key)
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass
            try:
                current["archive"].close()
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        log_memory_milestone(
            "LOG_EXPORT_FAILED", force=True, details=window_details
        )
        raise


def export_last_24h_zip(log_dir=None, now=None, max_zip_bytes=None) -> ExportResult:
    """واجهة توافقية للاستخدامات والاختبارات القديمة."""
    return export_runtime_logs_zip(
        hours=24,
        log_dir=log_dir,
        now=now,
        max_zip_bytes=max_zip_bytes,
    )


def cleanup_export(result: ExportResult):
    if result and result.temp_dir:
        shutil.rmtree(result.temp_dir, ignore_errors=True)
