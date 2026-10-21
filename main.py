import time
import os
import csv
import threading
import math
import uuid
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from core.logger import logger
from core.exchange import ExchangeClient
from engines.news_engine import NewsEngine
from engines.ta_engine import TAEngine
from engines.ws_candle_engine import SharedCandleStream
from engines.social_engine import SocialEngine
from strategy.hybrid_logic import HybridStrategy
from strategy.risk_manager import RiskManager, entry_sizing_balances
from strategy.trailing_stop import TrailingStopManager
from strategy.strategy_v2_evidence import (
    collect_authoritative_proposal_evidence,
    collect_closed_outcome_evidence,
    collect_post_exit_checkpoint,
)
from strategy.strategy_v2_shadow_storage import get_shadow_evidence_store
from strategy.alert_manager import TradeApproachAlertManager
from strategy.entry_confirmation import (
    build_setup_thesis_id,
    classify_virtual_invalidation,
    entry_zone_tolerance,
    evaluate_entry_confirmation,
)
from strategy.user_visible_trade_proposal import (
    EVENT_TYPE as USER_VISIBLE_TRADE_PROPOSAL,
    build_user_visible_trade_proposal,
)
from strategy.short_high_confidence import (
    POLICY_NAME as SHORT_HIGH_CONFIDENCE_V1,
    evaluate_short_high_confidence,
)
from strategy.market_regime import detect_market_regime
from strategy.candidate_assessment import enrich_scan_results
from utils.telegram_bot import TelegramNotifier
from utils.telegram_proposal_runtime_identity import (
    telegram_proposal_runtime_identity,
)
from utils.state_manager import StateManager
from utils.performance_tracker import PerformanceTracker
from utils.candle_sync import seconds_to_next_candle
from core.config import Config
from core.universe_filter import UniverseFilter
from filters.derivatives_risk_filter import DerivativesRiskFilter
from utils.api_status_monitor import APIStatusMonitor
from strategy.filter_profiles import get_filter_profile
from utils.trade_journal import TradeJournal
from webapp.server import start_webapp
from utils.db_manager import init_db, insert_trade, trade_exists, migrate_from_csv
from utils.forensics_logger import init_forensics_table, log_signal
from strategies.smc_rsi_strategy    import SMCRSIStrategy, StrategyContext
from strategies.dip_catcher_strategy  import DipCatcherStrategy
from strategies.obv_pump_dump_strategy import OBVPumpDumpStrategy
from strategies.bottom_fishing_strategy import BottomFishingStrategy
from utils.signal_dedup import SignalDeduplicator
from utils.decision_audit import audit_decision_event, close_decision_audit
from utils.lazy_ai_engine import LazyAIEngine
from utils.runtime_health import (
    begin_runtime_session,
    log_memory_milestone,
    mark_graceful_shutdown,
    start_resource_monitor,
    stop_resource_monitor,
)
from utils.single_writer import SingleWriterLease
from utils.test_session import record_run_end, record_run_start
from utils.hot_list_monitor import HotListShadowMonitor, get_latest_hot_list_snapshot
from core.scanner_universe import subscription_symbols
from utils.momentum_continuation_monitor import (
    MomentumContinuationShadowMonitor,
    register_external_smc_thesis,
)
from utils.active_altcoin_shadow_monitor import ActiveAltcoinShadowMonitor

signal_dedup = None  # تهيئة مبكرة للمتغير العالمي
_execution_lock = threading.RLock()  # ذرّية Scanner/Sniper من الفحص حتى حفظ الحالة
_PROCESS_INSTANCE_ID = uuid.uuid4().hex
CLOSE_ALREADY_PROCESSING = "ALREADY_PROCESSING"
CLOSE_PERSIST_RETRY_BASE_SECONDS = 60
CLOSE_PERSIST_RETRY_MAX_SECONDS = 15 * 60
CLOSE_PERSIST_NOTIFICATION_INTERVAL_SECONDS = 60 * 60


def _strategy_v2_shadow_store():
    """Lazily open the isolated research store only when the flag is enabled."""
    return get_shadow_evidence_store(
        Config.STRATEGY_V2_SHADOW_DIR,
        max_bytes=Config.STRATEGY_V2_SHADOW_MAX_BYTES,
    )


def _collect_strategy_v2_proposal_shadow(*, proposal, coin_data, audit_ids):
    """Best-effort observation hook; its result never participates in V1."""
    if not getattr(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", False):
        return False
    try:
        collect_authoritative_proposal_evidence(
            storage=_strategy_v2_shadow_store(),
            proposal=proposal,
            coin_data=coin_data,
            reset_evidence=_thesis_reset_evidence(
                coin_data.get("_ta_context") or {}
            ),
            audit_ids=audit_ids,
        )
        return True
    except Exception as shadow_error:
        audit_decision_event(
            **audit_ids,
            stage="STRATEGY_V2_SHADOW_DECISION",
            status="SHADOW_COLLECTION_ERROR_V1_UNCHANGED",
            details={"error_type": type(shadow_error).__name__},
            durable=True,
        )
        return False


def _market_regime_label(value):
    """Return the stable textual regime stored in the trades table."""
    if isinstance(value, dict):
        return str(value.get("label") or value.get("regime") or "UNKNOWN")
    return str(value or "UNKNOWN")


def _close_persistence_retry_due(entry_meta, now_ts=None):
    """Whether a closed position may retry durable persistence now."""
    now_ts = float(time.time() if now_ts is None else now_ts)
    retry_after = float((entry_meta or {}).get("close_persistence_retry_after") or 0.0)
    return now_ts >= retry_after


def _schedule_close_persistence_retry(entry_meta, now_ts=None):
    """Persist an exponential retry schedule without dropping close metadata."""
    now_ts = float(time.time() if now_ts is None else now_ts)
    attempts = int((entry_meta or {}).get("close_persistence_attempts") or 0) + 1
    delay = min(
        CLOSE_PERSIST_RETRY_MAX_SECONDS,
        CLOSE_PERSIST_RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 10)),
    )
    entry_meta["close_persistence_attempts"] = attempts
    entry_meta["close_persistence_last_failed_at"] = now_ts
    entry_meta["close_persistence_retry_after"] = now_ts + delay
    return delay


def _close_persistence_failure_notification_due(entry_meta, now_ts=None):
    now_ts = float(time.time() if now_ts is None else now_ts)
    last_notice = float(
        (entry_meta or {}).get("close_persistence_last_notification_at") or 0.0
    )
    if last_notice <= 0:
        return True
    return now_ts - last_notice >= CLOSE_PERSIST_NOTIFICATION_INTERVAL_SECONDS

def maybe_send_periodic_reports(notifier, state_manager, client=None):
    """
    يرسل تقارير Daily / Weekly / Monthly / Yearly مرة واحدة فقط لكل فترة.
    تعتمد على testnet_trades_log.csv كمصدر حقيقة.
    """
    now = datetime.now(timezone.utc)

    tracker = PerformanceTracker(state_manager)

    def get_marker_key(period):
        if period == "daily":
            return now.strftime("%Y-%m-%d")
        if period == "weekly":
            iso = now.isocalendar()
            return f"{iso.year}-W{iso.week}"
        if period == "monthly":
            return now.strftime("%Y-%m")
        if period == "yearly":
            return now.strftime("%Y")
        return now.strftime("%Y-%m-%d")

    def already_sent(period):
        markers = state_manager.get("report_markers", {})
        return markers.get(period) == get_marker_key(period)

    def mark_sent(period):
        markers = state_manager.get("report_markers", {})
        markers[period] = get_marker_key(period)
        state_manager.set("report_markers", markers)

    def send_report(period):
        log_memory_milestone("REPORT_START", force=True, details={"period": period})
        try:
            text = tracker.format_report(period, client=client)
            ok = notifier.send_message(text)
            if ok:
                mark_sent(period)
                logger.info(f"تم إرسال التقرير {period} بنجاح.")
        except Exception as e:
            logger.error(f"فشل إرسال التقرير {period}: {e}")
        finally:
            log_memory_milestone("REPORT_END", force=True, details={"period": period})

    # يومي: آخر اليوم
    if now.hour == 23 and now.minute >= 55 and not already_sent("daily"):
        send_report("daily")

    # أسبوعي: نهاية الأحد
    if now.weekday() == 6 and now.hour == 23 and now.minute >= 55 and not already_sent("weekly"):
        send_report("weekly")

    # شهري: آخر يوم من الشهر
    tomorrow = now + timedelta(days=1)
    is_last_day_of_month = tomorrow.month != now.month
    if is_last_day_of_month and now.hour == 23 and now.minute >= 55 and not already_sent("monthly"):
        send_report("monthly")

    # سنوي: 31 ديسمبر
    if now.month == 12 and now.day == 31 and now.hour == 23 and now.minute >= 55 and not already_sent("yearly"):
        send_report("yearly")


def get_wallet_equity_snapshot(client, state_manager=None):
    try:
        if client is None: return 0.0, 0.0
        
        simulated_cap = getattr(Config, "TESTNET_SIMULATED_BALANCE", 0)
        if getattr(Config, "USE_TESTNET", False) and simulated_cap > 0 and state_manager is not None:
            bal = client.get_balance()
            info = bal.get("info", {}) if bal else {}
            total_unrealized = float(info.get("totalUnrealizedProfit", 0) or 0)
            
            tracker = PerformanceTracker(state_manager)
            sim_wallet = tracker.get_wallet(unrealized_pnl=total_unrealized)
            return float(sim_wallet["equity"]), float(sim_wallet["available"])
        bal = client.get_balance()
        info = bal.get("info", {}) if bal else {}
        usdt_available = float(bal.get("USDT", {}).get("free", 0) if bal else 0)
        wallet_balance = float(info.get("totalWalletBalance", usdt_available) or 0)
        total_unrealized = float(info.get("totalUnrealizedProfit", 0) or 0)
        equity = float(info.get("totalMarginBalance", wallet_balance + total_unrealized) or 0)

        return equity, usdt_available
    except Exception as e:
        logger.error(f"فشل جلب لقطة المحفظة: {e}")
        return None, None

def normalize_reference_balance_in_state(state_manager):
    """
    يصحح أي reference_balance قديم داخل bot_state.json.
    هذا يمنع بقاء 50.0 في entry_metadata القديمة أو المحملة من جلسات سابقة.
    """
    try:
        ref = float(state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0)))
        state = state_manager.get_state()
        entry_metadata = state.get("entry_metadata", {}) or {}

        changed = False
        for symbol, meta in entry_metadata.items():
            if not isinstance(meta, dict):
                continue

            old_ref = float(meta.get("reference_balance", ref) or ref)
            if abs(old_ref - ref) > 1e-9:
                meta["reference_balance"] = ref
                changed = True
                logger.warning(
                    f"[StateFix] تم تصحيح reference_balance لـ {symbol}: "
                    f"{old_ref} -> {ref}"
                )

        if changed:
            state_manager.save_state()

    except Exception as e:
        logger.warning(f"[StateFix] فشل تصحيح reference_balance داخل state: {e}")


def enforce_daily_loss_limit(client, state_manager, notifier=None):
    """فحص مستقل لحد الخسارة دون استبدال Equity الحقيقي برصيد تخميني."""
    try:
        balance = client.get_balance()
        if not balance:
            raise RuntimeError("empty balance response")
        info = balance.get("info", {}) or {}
        equity = float(
            info.get("totalMarginBalance")
            or info.get("totalWalletBalance")
            or (balance.get("USDT", {}) or {}).get("total")
            or 0
        )
        if equity <= 0:
            raise RuntimeError(f"invalid equity={equity}")
        available = float(
            info.get("availableBalance")
            or (balance.get("USDT", {}) or {}).get("free")
            or 0
        )

        if state_manager.get("risk_equity_status") != "OK":
            state_manager.set("risk_equity_status", "OK")

        state_manager.update_daily_balance(equity)
        initial = state_manager.get_initial_balance()
        if initial <= 0:
            return {"status": "OK", "equity": equity, "available": available, "drawdown": 0.0}

        drawdown = max(0.0, ((initial - equity) / initial) * 100)
        limit = float(getattr(Config, "DAILY_LOSS_LIMIT_PERCENT", 3.0))
        if drawdown >= limit:
            already_active = bool(state_manager.get("kill_switch_active", False))
            state_manager.set("kill_switch_active", True)
            if notifier is not None and not already_active:
                notifier.send_message(
                    f"🛑 <b>Daily Loss Kill Switch</b>\n"
                    f"التراجع اليومي: <code>{drawdown:.2f}%</code>\n"
                    f"الحد: <code>{limit:.2f}%</code>\n"
                    "تم منع جميع أوامر الدخول الجديدة فوراً."
                )
        return {"status": "OK", "equity": equity, "available": available, "drawdown": drawdown}
    except Exception as e:
        if state_manager.get("risk_equity_status") != "UNKNOWN":
            state_manager.set("risk_equity_status", "UNKNOWN")
        logger.error(
            f"[DailyLoss] تعذر قراءة Equity الحقيقي ({e}) — سيُمنع الدخول حتى عودة البيانات."
        )
        return {"status": "UNKNOWN", "error": str(e)}

def log_testnet_trade(symbol, entry_meta, exit_details):
    """
    تسجيل الصفقات في SQLite (أساسي) + CSV (نسخة احتياطية للتوافق)
    """
    exit_time  = exit_details.get("exit_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry_time = entry_meta.get("entry_time", "")

    trade_id = entry_meta.get("trade_id") or (
        f"{symbol}|{entry_meta.get('side', '')}|{entry_time}|"
        f"{entry_meta.get('entry_price', 0.0)}|{exit_time}|{exit_details.get('exit_price', 0.0)}"
    )

    row = {
        "trade_id":               trade_id,
        "symbol":                 symbol,
        "side":                   entry_meta.get("side", ""),
        "entry_time":             entry_time,
        "entry_price":            entry_meta.get("entry_price", 0.0),
        "exit_time":              exit_time,
        "exit_price":             exit_details.get("exit_price", 0.0),
        "pnl":                    exit_details.get("pnl", 0.0),
        "pnl_pct":                exit_details.get("pnl_pct", 0.0),
        "exit_reason":            exit_details.get("exit_reason", ""),
        "entry_reason":           entry_meta.get("entry_reason", ""),
        "strategy_source":        entry_meta.get("strategy_source", "unknown"),
        "sentiment_score":        entry_meta.get("sentiment_score", 0.0),
        "adx":                    entry_meta.get("adx", 0.0),
        "ema_200":                entry_meta.get("ema_200", 0.0),
        "atr":                    entry_meta.get("atr", 0.0),
        "spread":                 entry_meta.get("spread", 0.0),
        "volume_24h":             entry_meta.get("volume_24h", 0.0),
        "risk_pct":               entry_meta.get("risk_pct", 0.0),
        "leverage":               entry_meta.get("leverage", 1.0),
        "slippage":               exit_details.get("slippage", 0.0),
        "amount":                 entry_meta.get("amount", 0.0),
        "filter_profile":         entry_meta.get("filter_profile", ""),
        "journal_dir":            entry_meta.get("journal_dir", ""),
        "wallet_equity_at_entry": entry_meta.get("wallet_equity_at_entry", 0.0),
        "wallet_pnl_pct":         exit_details.get("wallet_pnl_pct", 0.0),
        "reference_balance":      float(exit_details.get("reference_balance", entry_meta.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0)))),
        "pnl_ref_50":             exit_details.get("pnl_ref_50", 0.0),
        "setup_thesis_id":        entry_meta.get("setup_thesis_id", ""),
        "market_regime":          _market_regime_label(
            entry_meta.get("market_regime", "UNKNOWN")
        ),
        "target_type":            (entry_meta.get("target_plan") or {}).get("target_type", ""),
        "planned_rr":             entry_meta.get("planned_rr", 0.0),
        "mfe_r":                  entry_meta.get("mfe_r", 0.0),
        "mae_r":                  entry_meta.get("mae_r", 0.0),
        "final_r":                exit_details.get("final_r", 0.0),
        "realized_pnl_gross":     exit_details.get("realized_pnl_gross", 0.0),
        "entry_commission":       exit_details.get("entry_commission", 0.0),
        "exit_commission":        exit_details.get("exit_commission", 0.0),
        "commission":             exit_details.get("commission", 0.0),
        "funding":                exit_details.get("funding", 0.0),
        "funding_status":         exit_details.get("funding_status", "UNKNOWN"),
        "pnl_complete":           bool(exit_details.get("pnl_complete", False)),
    }

    # ── الكتابة الأساسية: SQLite ──────────────────────────────────
    if trade_exists(trade_id):
        logger.warning(f"⚠️ الصفقة {trade_id} مسجلة مسبقًا في SQLite، تم تجاهل التكرار.")
        return True

    db_ok = insert_trade(row)
    if db_ok:
        logger.info(f"✅ [{symbol}] صفقة مسجلة في SQLite.")

    # ── النسخة الاحتياطية: CSV (للتوافق مع الأدوات القديمة) ──────
    try:
        file_path  = getattr(Config, "TESTNET_TRADES_LOG", "testnet_trades_log.csv")
        os.makedirs(os.path.dirname(os.path.abspath(file_path)) or ".", exist_ok=True)
        file_exists = os.path.exists(file_path)
        headers = list(row.keys())
        if file_exists:
            # لا نغيّر عرض صف CSV قديم عند إضافة أعمدة SQLite جديدة؛ وإلا
            # يصبح الملف غير قابل للترحيل. SQLite يبقى المصدر الكامل.
            with open(file_path, 'r', newline='', encoding='utf-8') as existing:
                existing_headers = next(csv.reader(existing), [])
            if existing_headers:
                headers = existing_headers
        with open(file_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        logger.warning(f"[CSV-Backup] فشل كتابة النسخة الاحتياطية: {e}")

    return db_ok

def execute_and_protect_trade(*args, **kwargs):
    """بوابة تنفيذ ذرّية مشتركة بين Scanner وSniper."""
    with _execution_lock:
        return _execute_and_protect_trade_locked(*args, **kwargs)


def _proposal_parity_summary(ledger):
    """Derive Proposal→Demo counts from the durable proposal ledger only."""
    records = [value for value in (ledger or {}).values() if isinstance(value, dict)]
    visible = [record for record in records if record.get("visible_at")]
    entries = [
        record for record in visible
        if int(record.get("entry_post_count") or 0) > 0
    ]
    filled = [
        record for record in entries
        if float(record.get("filled_amount") or record.get("executed_qty") or 0) > 0
        or record.get("actual_entry")
    ]
    protected = [
        record for record in filled
        if record.get("status") in {"ENTRY_FILLED_AND_PROTECTED", "CLOSED_RECONCILED"}
    ]
    closed = [record for record in protected if record.get("status") == "CLOSED_RECONCILED"]
    return {
        "telegram_trade_proposals_count": len(visible),
        "demo_entry_posts_count": len(entries),
        "demo_fills_count": len(filled),
        "demo_protected_count": len(protected),
        "demo_closed_reconciled_count": len(closed),
        "proposal_without_entry_count": len(visible) - len(entries),
        "proposal_to_entry_target": "1:1",
    }


def _proposal_ledger_update(state_manager, proposal, status, **details):
    """Persist proposal visibility/execution state under the execution lock."""
    ledger = state_manager.get("user_visible_trade_proposals", {}) or {}
    proposal_id = str(proposal["proposal_id"])
    previous = dict(ledger.get(proposal_id) or {})
    updated_at = datetime.now(timezone.utc).isoformat()
    timeline = list(previous.get("timeline") or [])
    timeline_detail_keys = (
        "entry_post_count", "exchange_order_id", "entry_client_order_id",
        "venue_status", "actual_entry", "filled_amount", "executed_qty",
        "sl", "tp", "missing_legs", "reconciliation_required",
        "recovered_after_restart", "realized_pnl", "realized_r", "fees",
        "funding", "reconciliation", "reason", "error",
    )
    timeline.append({
        "timestamp": updated_at,
        "status": str(status),
        "details": {
            key: details.get(key)
            for key in timeline_detail_keys
            if key in details and details.get(key) is not None
        },
    })
    # The current record remains compact even after months of retries while
    # the append-only decision audit keeps the complete historical stream.
    timeline = timeline[-200:]
    previous.update({
        "proposal": dict(proposal),
        "status": str(status),
        "updated_at": updated_at,
        "timeline": timeline,
        **details,
    })
    ledger[proposal_id] = previous
    saved = bool(state_manager.set("user_visible_trade_proposals", ledger))
    if saved:
        summary = _proposal_parity_summary(ledger)
        state_manager.set("telegram_proposal_demo_summary", summary)
        logger.info(f"TELEGRAM_PROPOSAL_DEMO_PARITY {summary}")
    return saved


def _is_hybrid_decision_demo_proposal(coin_data, state_manager=None):
    """Return whether this candidate has explicit Demo hybrid authority."""
    if not Config.TELEGRAM_PROPOSAL_DEMO_MODE or not isinstance(coin_data, dict):
        return False
    if coin_data.get("proposal_execution_mode") != "HYBRID_DECISION_DEMO":
        return False
    if state_manager is None:
        return True
    getter = getattr(state_manager, "get_demo_signal_execution_mode", None)
    return bool(getter and getter() == "hybrid_decision")


def _hybrid_decision_demo_profile(base_profile):
    """Demo-only profile preserving stops and original trailing management."""
    profile = dict(base_profile or {})
    profile.update({
        "min_rr": 0.0,
        "allow_rr_fallback": True,
        "fallback_rr": 4.0,
        "nearest_liquidity_checkpoint_only": True,
        "proposal_execution_mode": "HYBRID_DECISION_DEMO",
    })
    return profile


def _short_confidence_counter(state_manager, name):
    increment = getattr(
        state_manager, "increment_short_high_confidence_counter", None
    )
    return increment(name) if callable(increment) else 0


def _audit_short_confidence(
    *, state_manager, symbol, evaluation, scan_id=None, decision_id=None,
    trade_id=None, thesis_id=None, virtual_order_id=None, count=True,
):
    """Persist one structured SHORT policy decision and its public counters."""
    status = str((evaluation or {}).get("status") or "WAIT_SHORT_CONFIRMATION")
    reason = str((evaluation or {}).get("reason") or "SHORT_REJECT_5M_WAITING")
    counter = None
    if status == "PASS":
        counter = "SHORT_HIGH_CONFIDENCE_PASS"
    elif status == "WAIT_SHORT_CONFIRMATION":
        counter = "SHORT_WAIT_CONFIRMATION"
    elif "HTF" in reason:
        counter = "SHORT_REJECT_HTF"
    elif reason == "SHORT_REJECT_LOCATION":
        counter = "SHORT_REJECT_LOCATION"
    elif reason == "SHORT_REJECT_SUPPORT_TOO_CLOSE":
        counter = "SHORT_REJECT_SUPPORT_TOO_CLOSE"
    elif "5M" in reason:
        counter = "SHORT_REJECT_5M"
    if count and counter:
        _short_confidence_counter(state_manager, counter)
    audit_decision_event(
        scan_id=scan_id,
        decision_id=decision_id,
        signal_id=decision_id,
        setup_id=thesis_id,
        thesis_id=thesis_id,
        virtual_order_id=virtual_order_id,
        trade_id=trade_id,
        symbol=symbol,
        stage=SHORT_HIGH_CONFIDENCE_V1,
        status=status,
        details=dict(evaluation or {}),
        durable=True,
    )


def _short_policy_rejection_label(reason):
    labels = {
        "SHORT_REJECT_HTF_BULLISH": "رفض SHORT: سياق 4H/1H صاعد بوضوح",
        "SHORT_REJECT_LOCATION": "رفض SHORT: لا توجد مقاومة/منطقة عرض منطقية",
        "SHORT_REJECT_SUPPORT_TOO_CLOSE": "رفض SHORT: دعم/طلب قوي قريب أسفل السعر",
        "SHORT_REJECT_STRUCTURAL_INVALIDATION": "رفض SHORT: إبطال هيكلي",
        "SHORT_REJECT_DATA": "رفض SHORT: بيانات السياق غير صالحة",
    }
    return labels.get(str(reason), f"رفض SHORT عالي الثقة: {reason}")


def _publish_authoritative_trade_proposal(
    *, state_manager, notifier, proposal, audit_ids
):
    """Publish exactly once; a notification failure prevents the entry POST."""
    proposal_id = str(proposal["proposal_id"])
    ledger = state_manager.get("user_visible_trade_proposals", {}) or {}
    existing = dict(ledger.get(proposal_id) or {})
    if existing:
        audit_decision_event(
            **audit_ids,
            stage=USER_VISIBLE_TRADE_PROPOSAL,
            status="DUPLICATE_SUPPRESSED",
            details={
                "proposal_id": proposal_id,
                "existing_status": existing.get("status"),
                "event_hash": proposal.get("event_hash"),
            },
            durable=True,
        )
        return False
    if not _proposal_ledger_update(
        state_manager, proposal, "PENDING_NOTIFICATION"
    ):
        return False
    sent = bool(notifier.send_trade_proposal(proposal))
    final_status = "VISIBLE" if sent else "NOTIFICATION_FAILED"
    _proposal_ledger_update(
        state_manager,
        proposal,
        final_status,
        visible_at=(datetime.now(timezone.utc).isoformat() if sent else None),
    )
    audit_decision_event(
        **audit_ids,
        stage=USER_VISIBLE_TRADE_PROPOSAL,
        status=final_status,
        details=proposal,
        durable=True,
    )
    return sent


def parse_entry_order_execution(order):
    """Normalize CCXT/Binance order fields without mistaking ACK for a fill."""
    if not isinstance(order, dict):
        return {
            "state": "UNKNOWN", "venue_status": "UNKNOWN",
            "executed_qty": 0.0, "leaves_qty": None, "fill_vwap": 0.0,
        }
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    venue_status = str(
        info.get("status") or order.get("status") or "UNKNOWN"
    ).upper()

    def _number(*values):
        for value in values:
            try:
                if value not in (None, ""):
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    executed = _number(
        order.get("filled"), info.get("executedQty"), info.get("cumQty")
    )
    requested = _number(order.get("amount"), info.get("origQty"))
    leaves = max(0.0, requested - executed) if requested > 0 else None
    fill_vwap = _number(order.get("average"), info.get("avgPrice"))
    if fill_vwap <= 0 and executed > 0:
        cost = _number(order.get("cost"), info.get("cumQuote"))
        fill_vwap = cost / executed if cost > 0 else _number(order.get("price"))

    terminal_no_fill = {
        "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"
    }
    if executed > 0 and (
        venue_status in {"PARTIALLY_FILLED", "PARTIALLYFILLED"}
        or (leaves is not None and leaves > 0)
    ):
        state = "PARTIALLY_FILLED"
    elif executed > 0 and venue_status in {"FILLED", "CLOSED"}:
        state = "ENTRY_FILLED"
    elif executed > 0:
        state = "PARTIALLY_FILLED" if leaves is None or leaves > 0 else "ENTRY_FILLED"
    elif venue_status in terminal_no_fill:
        state = "TERMINAL_NO_FILL"
    elif venue_status in {"NEW", "OPEN", "PENDING_NEW", "ACCEPTED"}:
        state = "ACKNOWLEDGED_NO_FILL"
    else:
        state = "UNKNOWN"
    return {
        "state": state,
        "venue_status": venue_status,
        "executed_qty": executed,
        "leaves_qty": leaves,
        "fill_vwap": fill_vwap,
    }


def evaluate_executable_entry_price(
    *, side, ticker, reference_price, optimal_entry, atr_val,
    max_spread_pct=None, planned_sl=None, planned_tp=None, min_rr=None,
    allow_confirmed_reclaim=False,
):
    """Validate the price that a MARKET order can actually consume.

    Entry discovery may observe Mark Price, while MARKET orders fill from the
    contract order book. This guard makes that basis difference explicit and
    rejects stale/misaligned plans before any order intent reaches Binance.
    """
    side = str(side or "").lower()
    ticker = ticker if isinstance(ticker, dict) else {}
    try:
        bid = float(ticker.get("bid") or 0.0)
        ask = float(ticker.get("ask") or 0.0)
        reference_price = float(reference_price or 0.0)
        optimal_entry = float(optimal_entry or 0.0)
        atr_val = float(atr_val or 0.0)
    except (TypeError, ValueError):
        bid = ask = reference_price = optimal_entry = atr_val = 0.0

    result = {
        "passed": False,
        "reason": "INVALID_EXECUTABLE_PRICE",
        "side": side,
        "bid": bid,
        "ask": ask,
        "reference_price": reference_price,
        "optimal_entry": optimal_entry,
        "allow_confirmed_reclaim": bool(allow_confirmed_reclaim),
    }
    numeric_basis = (bid, ask, reference_price, optimal_entry, atr_val)
    if (
        side not in ("buy", "sell")
        or not all(math.isfinite(value) for value in numeric_basis)
        or bid <= 0
        or ask <= 0
        or ask < bid
    ):
        return result
    if reference_price <= 0 or optimal_entry <= 0:
        result["reason"] = "MISSING_ENTRY_PRICE_BASIS"
        return result

    executable_price = ask if side == "buy" else bid
    spread_pct = (ask - bid) / bid
    tolerance = entry_zone_tolerance(optimal_entry, atr_val)
    basis_delta = abs(executable_price - reference_price)
    allowed_spread = float(
        Config.MAX_SPREAD_PCT if max_spread_pct is None else max_spread_pct
    )
    result.update({
        "executable_price": executable_price,
        "spread_pct": spread_pct,
        "max_spread_pct": allowed_spread,
        "entry_tolerance": tolerance,
        "basis_delta": basis_delta,
        "basis_delta_pct": basis_delta / reference_price,
        "distance_from_optimal_pct": abs(executable_price - optimal_entry) / optimal_entry,
    })
    try:
        planned_sl = float(planned_sl or 0.0)
        planned_tp = float(planned_tp or 0.0)
        min_rr = float(min_rr or 0.0)
    except (TypeError, ValueError):
        planned_sl = planned_tp = min_rr = 0.0
    if planned_sl > 0 and planned_tp > 0:
        if side == "buy":
            repriced_risk = executable_price - planned_sl
            repriced_reward = planned_tp - executable_price
        else:
            repriced_risk = planned_sl - executable_price
            repriced_reward = executable_price - planned_tp
        repriced_rr = (
            repriced_reward / repriced_risk
            if repriced_risk > 0 and repriced_reward > 0 else 0.0
        )
        result.update({
            "planned_sl": planned_sl,
            "planned_tp": planned_tp,
            "repriced_risk": repriced_risk,
            "repriced_reward": repriced_reward,
            "repriced_rr": repriced_rr,
            "min_rr": min_rr,
        })
    if spread_pct > allowed_spread:
        result["reason"] = "SPREAD_TOO_WIDE"
        return result
    if tolerance <= 0 or basis_delta > tolerance:
        result["reason"] = "PRICE_BASIS_DEVIATION"
        return result
    outside_entry_zone = abs(executable_price - optimal_entry) > tolerance
    result["outside_entry_zone"] = outside_entry_zone
    if outside_entry_zone:
        favorable_reclaim = (
            side == "buy" and executable_price >= optimal_entry
        ) or (
            side == "sell" and executable_price <= optimal_entry
        )
        result["favorable_reclaim_direction"] = favorable_reclaim
        if not (bool(allow_confirmed_reclaim) and favorable_reclaim):
            result["reason"] = (
                "EXECUTABLE_RR_BELOW_MINIMUM"
                if min_rr > 0 and result.get("repriced_rr", 0.0) < min_rr
                else "EXECUTABLE_OUTSIDE_ENTRY_ZONE"
            )
            return result
        if planned_sl <= 0 or planned_tp <= 0 or min_rr <= 0:
            result["reason"] = "MISSING_REPRICED_RR_FOR_CONFIRMED_RECLAIM"
            return result
        if result.get("repriced_rr", 0.0) < min_rr:
            result["reason"] = "EXECUTABLE_RR_BELOW_MINIMUM"
            return result
        result["confirmed_reclaim_override"] = True

    result["passed"] = True
    result["reason"] = "PASS"
    return result


def fetch_executable_contract_ticker(client, symbol, ticker=None):
    """Return a Futures ticker with a real top-of-book bid and ask.

    Binance Demo may return ``last`` from ``fetch_ticker`` while leaving
    ``bid``/``ask`` empty.  A MARKET decision cannot treat ``last`` or Mark
    Price as executable, so resolve the missing quotes from the contract order
    book and fail closed when neither source provides both sides.
    """
    resolved = dict(ticker) if isinstance(ticker, dict) else {}
    if not resolved:
        try:
            resolved = dict(client.exchange.fetch_ticker(symbol) or {})
        except Exception as exc:
            resolved = {"_ticker_error": str(exc)}

    try:
        bid = float(resolved.get("bid") or 0.0)
        ask = float(resolved.get("ask") or 0.0)
    except (TypeError, ValueError):
        bid = ask = 0.0
    if bid > 0 and ask >= bid:
        resolved["_executable_price_source"] = "FUTURES_TICKER_BID_ASK"
        return resolved

    try:
        book = client.exchange.fetch_order_book(symbol, limit=5) or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        book_bid = float(bids[0][0]) if bids and bids[0] else 0.0
        book_ask = float(asks[0][0]) if asks and asks[0] else 0.0
        if book_bid > 0 and book_ask >= book_bid:
            resolved["bid"] = book_bid
            resolved["ask"] = book_ask
            resolved["_executable_price_source"] = "FUTURES_ORDER_BOOK_TOP"
            return resolved
        resolved["_order_book_error"] = "EMPTY_OR_INVALID_TOP_OF_BOOK"
    except Exception as exc:
        resolved["_order_book_error"] = str(exc)
    resolved["_executable_price_source"] = "UNAVAILABLE"
    return resolved


def _execute_and_protect_trade_locked(symbol, final_decision, amount, current_price, trade_leverage, trade_risk_pct, atr_val, coin_data, sentiment, bars_df, ticker, ta_trend, client, risk_manager, trailing_manager, state_manager, notifier, active_symbols, scan_results, invalidation_level=None, ta_engine=None, trade_id=None):
    """
    دالة موحدة لتنفيذ أمر الماركت، حساب الستوب/الهدف الديناميكي (Sniper)، وضع الأوامر، وتحديث الحالة المحفوظة.
    """
    filter_profile = get_filter_profile(state_manager)
    hybrid_demo_proposal = _is_hybrid_decision_demo_proposal(
        coin_data, state_manager
    )
    if hybrid_demo_proposal:
        filter_profile = _hybrid_decision_demo_profile(filter_profile)
    audit_ids = {
        "scan_id": (coin_data or {}).get("_scan_id"),
        "decision_id": (coin_data or {}).get("_decision_id"),
        "setup_id": (coin_data or {}).get("_thesis_id"),
        "thesis_id": (coin_data or {}).get("_thesis_id"),
        "symbol": symbol,
        "trade_id": trade_id,
    }

    def _execution_event(status, details=None, durable=False, **ids):
        audit_decision_event(
            **audit_ids, **ids, stage="EXECUTION_TIMELINE", status=status,
            details=details or {}, durable=durable,
        )

    _execution_event(
        "GATE_ENTERED",
        {"side": str(final_decision).upper(), "requested_amount": amount,
         "expected_entry": current_price, "profile": filter_profile.get("key")},
    )

    # لا يمكن لأي caller مستقبلي تجاوز استرداد startup أو حالة state تالفة.
    if state_manager.get("recovery_complete", True) is not True:
        logger.critical(f"[{symbol}] رُفض التنفيذ: استرداد التشغيل غير مكتمل.")
        return False
    if (
        hasattr(state_manager, "is_state_recovery_blocked")
        and state_manager.is_state_recovery_blocked() is True
    ):
        logger.critical(f"[{symbol}] رُفض التنفيذ: ملف الحالة تالف وغير مسترد.")
        return False

    # حارس مركزي: لا يسمح لأي مسار تنفيذ بتجاوز حد الخسارة اليومية.
    if state_manager.get("kill_switch_active", False):
        logger.critical(f"[{symbol}] 🛑 رُفض التنفيذ: Daily Loss Kill Switch نشط.")
        return False
    if state_manager.get("risk_equity_status") == "UNKNOWN":
        logger.critical(
            f"[{symbol}] رُفض التنفيذ: Equity الحقيقي غير متاح، فلا يمكن ضمان حد الخسارة اليومي."
        )
        return False

    # Hard runtime policy: no caller, Telegram value, legacy state or future
    # execution path may exceed the reviewed portfolio limits.
    effective_risk_percent = float(trade_risk_pct or 0.0) * 100.0
    if not (
        float(Config.MIN_RISK_PER_TRADE_PERCENT)
        <= effective_risk_percent
        <= float(Config.HARD_MAX_RISK_PER_TRADE_PERCENT)
    ):
        logger.critical(
            f"[{symbol}] رُفض التنفيذ: risk={effective_risk_percent:.4f}% "
            "يتجاوز سياسة المخاطر الصلبة."
        )
        return False
    if not (
        int(Config.MIN_LEVERAGE)
        <= int(trade_leverage or 0)
        <= int(Config.MAX_LEVERAGE)
    ):
        logger.critical(
            f"[{symbol}] رُفض التنفيذ: leverage={trade_leverage} خارج "
            "سياسة الرافعة الصلبة."
        )
        return False

    pending_intent = state_manager.get_order_intent(symbol)
    if pending_intent:
        logger.warning(
            f"[{symbol}] رُفض تنفيذ جديد: توجد نية أمر سابقة بحالة "
            f"{pending_intent.get('status', 'UNKNOWN')} تحتاج حسمها أولاً."
        )
        return False

    position_state = client.has_open_position(symbol)
    if position_state is None:
        logger.critical(f"[{symbol}] رُفض التنفيذ: حالة المركز على Binance غير معروفة.")
        return False
    if position_state:
        logger.warning(f"[{symbol}] رُفض التنفيذ: يوجد مركز فعلي على Binance.")
        state_manager.add_active_symbol(symbol)
        active_symbols.add(symbol)
        return False

    # لا يوجد مسار Market مباشر بعد الآن: Scanner وSniper مطالبان بإرفاق
    # نتيجة الدالة المشتركة المبنية حصراً على شمعة 5m مغلقة وحديثة.
    entry_confirmation = (coin_data or {}).get("entry_confirmation") or {}
    if (
        not hybrid_demo_proposal
        and (
            not isinstance(entry_confirmation, dict)
            or not entry_confirmation.get("passed")
        )
    ):
        reason = str(entry_confirmation.get("reason") or "MISSING_SHARED_CONFIRMATION")
        logger.critical(
            f"[{symbol}] رُفض التنفيذ في البوابة المركزية: تأكيد 5m غير صالح ({reason})."
        )
        audit_decision_event(
            scan_id=(coin_data or {}).get("_scan_id"),
            decision_id=(coin_data or {}).get("_decision_id"),
            symbol=symbol,
            stage="ENTRY_CONFIRMATION",
            status="REJECTED_AT_EXECUTION_GATE",
            trade_id=trade_id,
            details={"reason": reason, "confirmation": entry_confirmation},
            durable=True,
        )
        if coin_data is not None and scan_results is not None:
            coin_data["decision"] = "🔴 REJECT"
            coin_data["reason"] = f"تأكيد دخول 5m غير صالح: {reason}"
            scan_results.append(coin_data)
        return False
    if hybrid_demo_proposal:
        audit_decision_event(
            scan_id=(coin_data or {}).get("_scan_id"),
            decision_id=(coin_data or {}).get("_decision_id"),
            symbol=symbol,
            stage="ENTRY_CONFIRMATION",
            status="NOT_REQUIRED_FOR_USER_VISIBLE_HYBRID_PROPOSAL",
            trade_id=trade_id,
            details={
                "authority": "HYBRID_DECISION",
                "execution_environment": "DEMO",
            },
            durable=True,
        )

    # بوابة جودة مركزية أخيرة لكل المسارات (Scanner/Sniper وأي مسار مستقبلي).
    if ta_engine is not None and bars_df is not None and not hybrid_demo_proposal:
        final_quality_audit = {}
        quality_ok, quality_reason = ta_engine.validate_entry_quality(
            bars_df=bars_df,
            side=final_decision,
            current_price=current_price,
            filter_profile=filter_profile,
            market_regime=((coin_data or {}).get("_ta_context") or {}).get(
                "market_regime", {}
            ),
            audit_details=final_quality_audit,
        )
        if not quality_ok:
            _execution_event(
                "REJECTED_FINAL_QUALITY",
                {"reason": quality_reason, "gates": final_quality_audit},
            )
            logger.warning(f"[{symbol}] رُفض التنفيذ في البوابة النهائية: {quality_reason}")
            if coin_data is not None and scan_results is not None:
                coin_data["decision"] = "🔴 REJECT"
                coin_data["reason"] = quality_reason
                scan_results.append(coin_data)
            return False

    # ── حارس مركزي: الحد الأقصى للصفقات (Single Source of Truth) ──────────────
    # يمنع أي مسار (Scanner / Sniper / Telegram) من تجاوز الحد مستقبلاً
    # Telegram Demo uses the same user-configured portfolio capacity as the
    # current bot.  The hard ceiling below still prevents values above the
    # documented ten-position limit.
    _max_trades = state_manager.get_max_open_trades()
    if not (1 <= int(_max_trades) <= int(Config.HARD_MAX_OPEN_TRADES)):
        logger.critical(
            f"[{symbol}] رُفض التنفيذ: max_open_trades={_max_trades} "
            f"يتجاوز الحد الصلب {Config.HARD_MAX_OPEN_TRADES}."
        )
        return False
    _active_now = state_manager.get_active_symbols()
    if len(_active_now) >= _max_trades:
        logger.warning(
            f"⚠️ [{symbol}] تم إحباط التنفيذ داخل execute_and_protect_trade: "
            f"الصفقات المفتوحة ({len(_active_now)}) = الحد ({_max_trades})"
        )
        if coin_data is not None and scan_results is not None:
            coin_data['decision'] = '🔴 REJECT'
            coin_data['reason'] = f"تجاوز الحد الأقصى ({_max_trades})"
            scan_results.append(coin_data)
        return False

    # MARKET fills consume bid/ask, not Mark Price. Fetch a fresh contract
    # ticker at the final gate and reject any price-basis drift larger than the
    # already-reviewed adaptive entry tolerance.
    optimal_entry = float(
        (coin_data or {}).get("optimal_entry")
        or ((coin_data or {}).get("_ta_context") or {}).get("optimal_entry")
        or 0.0
    )
    executable_ticker = fetch_executable_contract_ticker(client, symbol)
    if executable_ticker.get("_executable_price_source") == "UNAVAILABLE":
        logger.error(
            f"[{symbol}] تعذر جلب bid/ask حديث قبل التنفيذ: "
            f"ticker={executable_ticker.get('_ticker_error')} "
            f"order_book={executable_ticker.get('_order_book_error')}"
        )
    reviewed_target_plan = (coin_data or {}).get("target_plan") or {}
    confirmation_evidence = (
        entry_confirmation.get("evidence")
        if isinstance(entry_confirmation.get("evidence"), dict)
        else {}
    )
    allow_confirmed_reclaim = bool(
        entry_confirmation.get("passed")
        and confirmation_evidence.get("zone_touch_recorded")
        and confirmation_evidence.get("entry_location_valid")
        and confirmation_evidence.get("directional_close")
        and confirmation_evidence.get("reclaim")
    )
    executable_guard = evaluate_executable_entry_price(
        side=final_decision,
        ticker=executable_ticker,
        reference_price=current_price,
        optimal_entry=optimal_entry,
        atr_val=atr_val,
        planned_sl=reviewed_target_plan.get("sl"),
        planned_tp=reviewed_target_plan.get("tp"),
        min_rr=filter_profile.get("min_rr", 1.5),
        allow_confirmed_reclaim=allow_confirmed_reclaim,
    )
    audit_decision_event(
        **audit_ids,
        stage="EXECUTABLE_PRICE_GUARD",
        status="PASS" if executable_guard.get("passed") else "REJECTED",
        details=executable_guard,
        durable=True,
    )
    if not executable_guard.get("passed"):
        logger.warning(
            f"[{symbol}] رُفض التنفيذ قبل Binance: {executable_guard.get('reason')} | "
            f"reference={current_price} bid={executable_guard.get('bid')} "
            f"ask={executable_guard.get('ask')}"
        )
        if coin_data is not None and scan_results is not None:
            coin_data["decision"] = "🔴 REJECT"
            coin_data["reason"] = (
                "سعر التنفيذ الفعلي لا يطابق منطقة الدخول: "
                f"{executable_guard.get('reason')}"
            )
            scan_results.append(coin_data)
        return False

    ticker = executable_ticker
    current_price = float(executable_guard["executable_price"])
    coin_data["price"] = current_price
    coin_data["executable_price_guard"] = executable_guard
    price_tick_size = client.get_price_tick_size(symbol)
    executable_spread = max(
        0.0,
        float(executable_ticker.get("ask") or 0.0)
        - float(executable_ticker.get("bid") or 0.0),
    )

    # حساب الستوب والهدف بناءً على السعر الحالي أولاً للتأكد من جودة الـ RR
    sl, tp = risk_manager.calculate_sl_tp(
        final_decision, 
        current_price, 
        support=coin_data.get('support'), 
        resistance=coin_data.get('resistance'),
        atr=atr_val,
        invalidation_level=invalidation_level,
        market_levels=coin_data.get('market_levels', {}),
        filter_profile=filter_profile,
        setup_anchor=((coin_data.get('_ta_context') or {}).get('setup_anchor')),
        tick_size=price_tick_size or 0.0,
        spread=executable_spread,
    )
    
    if tp is None or sl is None:
        _execution_event(
            "REJECTED_TRADE_PLAN",
            {"reason": risk_manager.last_block_reason,
             "target_plan": dict(getattr(risk_manager, "last_plan", {}) or {})},
        )
        logger.warning(f"[{symbol}] ❌ تم إحباط التنفيذ: الـ RR سيء أو لا يوجد هدف سيولة مقابل.")
        coin_data['decision'] = '🔴 REJECT'
        coin_data['reason'] = "RR ضعيف حسب SMC Targets"
        scan_results.append(coin_data)
        return False
    target_plan = dict(getattr(risk_manager, "last_plan", {}) or {})
    coin_data["target_plan"] = target_plan
    stop_plan = dict(target_plan.get("stop_plan") or {})
    if stop_plan.get("policy") in {
        "STRUCTURAL_INITIAL_STOP_V1", "STRUCTURAL_INITIAL_STOP_V2"
    }:
        # Use the exact quantity Binance will accept, but do not increase or
        # otherwise change sizing.  Structure stays fixed; excess risk skips.
        rounded_amount = client.preview_order_amount(symbol, amount, current_price)
        balance_snapshot = client.get_balance()
        risk_equity, _ = entry_sizing_balances(balance_snapshot or {})
        risk_check = risk_manager.evaluate_structural_stop_account_risk(
            equity=risk_equity,
            amount=rounded_amount or 0.0,
            entry_price=current_price,
            stop_price=sl,
        )
        stop_plan["account_risk_check"] = risk_check
        target_plan["stop_plan"] = stop_plan
        coin_data["target_plan"] = target_plan
        _execution_event(
            "STRUCTURAL_INITIAL_STOP_RISK_CHECK",
            {"stop_plan": stop_plan},
            durable=True,
        )
        if not risk_check.get("valid"):
            reason = str(risk_check.get("reason") or "SKIP_STRUCTURAL_STOP_RISK_TOO_HIGH")
            logger.warning(f"[{symbol}] {reason}: {risk_check}")
            coin_data["decision"] = "🔴 REJECT"
            coin_data["reason"] = reason
            coin_data["rejection_stage"] = stop_plan.get("policy")
            scan_results.append(coin_data)
            return False
        amount = float(rounded_amount)
    pre_submit_sl = float(sl)
    pre_submit_tp = float(tp)
    # ── Spot/Futures Router ─────────────────────────────────────────
    # مستوحى من isFuturesPair() في bot_runner.ts
    # CCXT يضيف ":" للفيوتشرز مثل "BTC/USDT:USDT"، بينما الـ Spot بدون ":"
    is_futures = ':' in symbol or getattr(Config, 'DEFAULT_MARKET_TYPE', 'futures') == 'futures'
    market_type = 'futures' if is_futures else 'spot'
    logger.info(f"[Router] {symbol} → وضع التداول: {market_type.upper()}")

    # حفظ نية الأمر قبل الإرسال يجعل timeout قابلاً للاسترداد بعد عودة
    # الشبكة بدلاً من ظهور مركز حقيقي بلا سجل أو حماية.
    intent_now = time.time()
    proposal = None
    if Config.TELEGRAM_PROPOSAL_DEMO_MODE:
        is_bottom_fishing = (
            coin_data.get("winning_strategy")
            == "Macro Bottom Fishing & Trend Rider Strategy"
            or bool(coin_data.get("bottom_fishing_active", False))
        )
        proposal_stages = risk_manager.get_tp_steps(
            final_decision,
            float(current_price),
            float(sl),
            tp3_override=float(tp),
            is_bottom_fishing=is_bottom_fishing,
        )
        proposal = build_user_visible_trade_proposal(
            proposal_id=str(trade_id or f"proposal-{uuid.uuid4().hex}"),
            symbol=symbol,
            side=final_decision,
            proposed_entry=float(current_price),
            original_stop=float(sl),
            original_target=float(tp),
            target_stages=proposal_stages,
            thesis_id=coin_data.get("_thesis_id"),
            setup_id=coin_data.get("_thesis_id"),
            confirmation_bar_id=entry_confirmation.get("bar_id"),
            leverage=int(trade_leverage or 1),
            risk_percent=float(trade_risk_pct * 100),
            amount=float(amount),
            target_plan=target_plan,
        )
        coin_data["user_visible_trade_proposal"] = proposal
        _collect_strategy_v2_proposal_shadow(
            proposal=proposal, coin_data=coin_data, audit_ids=audit_ids
        )

    # تعطيل إرسال أوامر Demo لا يعطّل تحليل السوق أو عرض الاقتراحات. ننشر
    # الاقتراح النهائي بعد اكتمال جميع فحوص الاستراتيجية/المخاطر، ثم نتوقف
    # قبل حفظ نية إرسال أمر وقبل Entry POST. بذلك تبقى واجهة Telegram قابلة
    # للمراجعة في وضع observation-only مع ضمان order_calls_possible=false.
    if (
        Config.TELEGRAM_PROPOSAL_DEMO_MODE
        and not Config.DEMO_EXECUTION_ENABLED
    ):
        proposal_visible = bool(
            proposal
            and _publish_authoritative_trade_proposal(
                state_manager=state_manager,
                notifier=notifier,
                proposal=proposal,
                audit_ids=audit_ids,
            )
        )
        _execution_event(
            "DEMO_EXECUTION_DISABLED",
            {
                "order_calls_possible": False,
                "proposal_id": proposal.get("proposal_id") if proposal else None,
                "proposal_visible": proposal_visible,
            },
            durable=True,
        )
        logger.info(
            f"[{symbol}] Demo execution disabled; proposal_visible="
            f"{proposal_visible}; no order intent or Entry POST was created."
        )
        return False

    intent_saved = state_manager.save_order_intent(symbol, {
        "trade_id": trade_id,
        "status": "SUBMITTING",
        "created_at": intent_now,
        "updated_at": intent_now,
        "owner_instance_id": _PROCESS_INSTANCE_ID,
        "side": final_decision.upper(),
        "position_side": "LONG" if final_decision == "buy" else "SHORT",
        "requested_amount": float(amount),
        "expected_entry": float(current_price),
        "sl": float(sl or 0),
        "tp": float(tp or 0),
        "leverage": int(trade_leverage or 1),
        "risk_pct": float(trade_risk_pct * 100),
        "atr": float(atr_val or 0),
        "entry_reason": coin_data.get("reason", ""),
        "strategy_source": coin_data.get("winning_strategy", "unknown"),
        "filter_profile": filter_profile.get("key", "strict"),
        "setup_thesis_id": coin_data.get("_thesis_id"),
        "entry_confirmation": entry_confirmation,
        "target_plan": target_plan,
        "min_rr": float(filter_profile.get("min_rr", 1.5)),
        "proposal_id": proposal.get("proposal_id") if proposal else None,
        "user_visible_trade_proposal": proposal,
    })
    if not intent_saved:
        _execution_event("ORDER_INTENT_PERSIST_FAILED", durable=True)
        logger.critical(
            f"[{symbol}] رُفض التنفيذ: تعذر حفظ نية الأمر كتابةً على القرص قبل Binance."
        )
        if trade_id:
            try:
                from utils.forensics_logger import update_signal_outcome
                update_signal_outcome(trade_id, "REJECTED_INTENT_PERSISTENCE", 0.0)
            except Exception as exc:
                logger.error(f"[{symbol}] فشل تحديث Forensics لرفض نية الأمر: {exc}")
        return False

    if proposal:
        if not _publish_authoritative_trade_proposal(
            state_manager=state_manager,
            notifier=notifier,
            proposal=proposal,
            audit_ids=audit_ids,
        ):
            state_manager.archive_order_intent(
                symbol,
                "PROPOSAL_NOT_VISIBLE_OR_DUPLICATE",
                proofs={
                    "proposal_id": proposal["proposal_id"],
                    "event_hash": proposal["event_hash"],
                    "order_sent": False,
                },
            )
            _execution_event(
                "PROPOSAL_PUBLICATION_FAILED",
                {"proposal_id": proposal["proposal_id"], "order_sent": False},
                durable=True,
            )
            return False

    _execution_event(
        "ORDER_INTENT_PERSISTED",
        {"intent_status": "SUBMITTING", "requested_amount": float(amount)},
        durable=True,
    )

    _execution_event(
        "ENTRY_ORDER_SUBMITTING",
        {"client_order_id": trade_id, "market_type": market_type,
         "side": final_decision.upper(), "amount": float(amount)},
        durable=True,
    )
    if proposal:
        _proposal_ledger_update(
            state_manager,
            proposal,
            "ENTRY_DISPATCHING",
            entry_client_order_id=trade_id,
            dispatch_started_at=datetime.now(timezone.utc).isoformat(),
        )
    if (coin_data or {}).get("short_execution_policy") == SHORT_HIGH_CONFIDENCE_V1:
        # This is immediately adjacent to the one and only Entry POST.  It is
        # intentionally not incremented for a mere PASS or saved wait.
        _short_confidence_counter(state_manager, "SHORT_ENTRY_SUBMITTED")
    order = client.execute_trade(
        symbol, 
        final_decision, 
        amount, 
        current_price=current_price, 
        leverage=trade_leverage if is_futures else 1,
        market_type=market_type,
        client_order_id=trade_id,
    )
    if order:
        parsed_execution = parse_entry_order_execution(order)
        if proposal:
            _proposal_ledger_update(
                state_manager,
                proposal,
                "ENTRY_ACKNOWLEDGED",
                entry_post_count=1,
                exchange_order_id=order.get("id") if isinstance(order, dict) else None,
                venue_status=parsed_execution.get("venue_status"),
            )
        _execution_event(
            "ENTRY_ORDER_ACKNOWLEDGED",
            {
                "exchange_order_id": order.get("id") if isinstance(order, dict) else None,
                "client_order_id": order.get("clientOrderId") if isinstance(order, dict) else trade_id,
                "status": order.get("status") if isinstance(order, dict) else None,
                "filled": order.get("filled") if isinstance(order, dict) else None,
                "average": order.get("average") if isinstance(order, dict) else None,
                "parsed_execution": parsed_execution,
            },
            durable=True,
            exchange_order_id=order.get("id") if isinstance(order, dict) else None,
        )
        actual_entry = parsed_execution.get("fill_vwap") or current_price
        if not actual_entry or actual_entry == 0:
            actual_entry = current_price

        # لا نستخدم order.amount أو الكمية المطلوبة كدليل تنفيذ.  ACK/NEW لا
        # يعني fill، و503 قد يعني أن حالة الإرسال غير معروفة.
        actual_amount = float(parsed_execution.get("executed_qty") or 0.0)
        position_side = "LONG" if final_decision == "buy" else "SHORT"
        position_status = client.get_position_status(
            symbol,
            position_side=position_side if getattr(client, '_is_hedge_mode', False) else None,
        )
        if position_status.get('status') == 'OPEN':
            actual_amount = float(position_status.get('amount') or actual_amount)
            position_rows = position_status.get('positions') or []
            if position_rows and float(position_rows[0].get('entry_price') or 0) > 0:
                actual_entry = float(position_rows[0]['entry_price'])
            if parsed_execution["state"] not in {"PARTIALLY_FILLED", "ENTRY_FILLED"}:
                parsed_execution["state"] = "ENTRY_FILLED"
            parsed_execution["resolution_source"] = "POSITION_OPEN"
        else:
            parsed_execution["resolution_source"] = "ORDER_RESPONSE"
        _execution_event(
            "POSITION_VERIFICATION",
            {
                "position_status": position_status.get("status"),
                "position_side": position_side,
                "actual_entry": actual_entry,
                "actual_amount": actual_amount,
            },
            durable=True,
            exchange_order_id=order.get("id") if isinstance(order, dict) else None,
            position_id=trade_id,
        )

        if (
            parsed_execution["state"] == "TERMINAL_NO_FILL"
            and position_status.get("status") == "CLOSED"
        ):
            logger.warning(
                f"[{symbol}] أمر الدخول انتهى نهائياً بلا تنفيذ "
                f"({parsed_execution['venue_status']})."
            )
            state_manager.archive_order_intent(
                symbol,
                "ENTRY_TERMINAL_NO_FILL",
                proofs={"order": parsed_execution, "position_status": "CLOSED"},
            )
            _execution_event(
                "ENTRY_TERMINAL_NO_FILL", parsed_execution, durable=True,
                exchange_order_id=order.get("id"),
            )
            if proposal:
                _proposal_ledger_update(
                    state_manager,
                    proposal,
                    "ENTRY_TERMINAL_NO_FILL",
                    entry_post_count=1,
                    venue_status=parsed_execution.get("venue_status"),
                )
            return False

        if actual_amount <= 0:
            next_status = (
                "ENTRY_ACKNOWLEDGED_NO_FILL"
                if parsed_execution["state"] == "ACKNOWLEDGED_NO_FILL"
                else "ENTRY_STATUS_UNKNOWN"
            )
            state_manager.transition_order_intent(
                symbol,
                "SUBMITTING",
                next_status,
                updates={
                    "venue_status": parsed_execution["venue_status"],
                    "executed_qty": 0.0,
                    "leaves_qty": parsed_execution["leaves_qty"],
                    "exchange_order_id": order.get("id"),
                    "retry_after": time.time() + 15,
                },
            )
            logger.critical(
                f"[{symbol}] Binance أقرت الطلب دون دليل fill "
                f"(status={parsed_execution['venue_status']}, "
                f"position={position_status.get('status')}). ستبقى النية للاسترداد."
            )
            if proposal:
                _proposal_ledger_update(
                    state_manager,
                    proposal,
                    next_status,
                    entry_post_count=1,
                    venue_status=parsed_execution.get("venue_status"),
                    reconciliation_required=True,
                )
            return False

        if position_status.get("status") == "CLOSED":
            # توجد أدلة fill لكن لا يوجد مركز الآن (قد يكون أُغلق سريعاً). لا
            # ننشئ حماية على مركز غائب ولا نمحو الحدث من السجل.
            state_manager.archive_order_intent(
                symbol,
                "ENTRY_FILLED_POSITION_CLOSED",
                proofs={"order": parsed_execution, "position_status": "CLOSED"},
            )
            _execution_event(
                "ENTRY_FILLED_POSITION_CLOSED", parsed_execution, durable=True,
                exchange_order_id=order.get("id"),
            )
            logger.critical(
                f"[{symbol}] الأمر يحمل fill={actual_amount} لكن المركز صفر؛ "
                "حُفظ الحدث للمصالحة دون إنشاء حماية أو صفقة محلية."
            )
            if proposal:
                _proposal_ledger_update(
                    state_manager,
                    proposal,
                    "ENTRY_FILLED_POSITION_CLOSED",
                    entry_post_count=1,
                    executed_qty=float(actual_amount),
                )
            return False
        amount = actual_amount

        intent_transitioned = state_manager.transition_order_intent(
            symbol,
            "SUBMITTING",
            parsed_execution["state"],
            updates={
                "actual_entry": float(actual_entry),
                "actual_amount": float(amount),
                "executed_qty": float(amount),
                "leaves_qty": parsed_execution.get("leaves_qty"),
                "venue_status": parsed_execution.get("venue_status"),
                "resolution_source": parsed_execution.get("resolution_source"),
                "exchange_order_id": order.get("id") if isinstance(order, dict) else None,
            },
        )
        if not intent_transitioned:
            # المركز أصبح حقيقياً؛ نستمر فوراً في الحماية ولا نتركه عارياً، لكن
            # نبقي النية القائمة ليستردها monitor إذا تعطلت بقية العملية.
            logger.critical(
                f"[{symbol}] تعذر تثبيت حالة fill على القرص؛ ستستمر الحماية المتزامنة."
            )
            
        # [تسجيل منع التكرار - Signal Dedup]
        global signal_dedup
        if signal_dedup is not None:
            try:
                # قراءة timestamp الحقيقي من العمود (يدعم مصدرَيْ WS وREST)
                row = bars_df.iloc[-2]
                if 'time' in bars_df.columns and pd.notna(row.get('time')):
                    bar_open_time = int(row['time'])                             # WS: int ms
                elif 'timestamp' in bars_df.columns and pd.notna(row.get('timestamp')):
                    ts = row['timestamp']
                    bar_open_time = int(ts.timestamp() * 1000) if hasattr(ts, 'timestamp') else int(ts)  # REST: datetime
                else:
                    bar_open_time = None
                if bar_open_time is not None:
                    signal_dedup.register_signal(symbol, bar_open_time)
            except Exception as e:
                logger.warning(f"[{symbol}] فشل تسجيل Signal Dedup: {e}")
                
        # إعادة الضبط على السعر الفعلي بعد الانزلاق (Slippage)
        sl, tp = risk_manager.calculate_sl_tp(
            final_decision, 
            actual_entry, 
            support=coin_data.get('support'), 
            resistance=coin_data.get('resistance'),
            atr=atr_val,
            invalidation_level=invalidation_level,
            market_levels=coin_data.get('market_levels', {}),
            filter_profile=filter_profile,
            setup_anchor=((coin_data.get('_ta_context') or {}).get('setup_anchor')),
            tick_size=price_tick_size or 0.0,
            spread=executable_spread,
        )
        if tp is None or sl is None:
            # لا نغيّر أطروحة الصفقة بعد التعبئة ولا نختلق هدف ATR. إذا أفسد
            # الانزلاق الـRR، فالخروج الفوري أكثر أماناً من إدارة خطة لم تعد صالحة.
            logger.critical(
                f"[{symbol}] الخطة فقدت RR صالحاً بعد التعبئة "
                f"(pre_sl={pre_submit_sl}, pre_tp={pre_submit_tp}, fill={actual_entry}). "
                "جاري إغلاق المركز بدلاً من اختلاق هدف."
            )
            close_confirmed = client.close_position(symbol, final_decision, amount)
            audit_decision_event(
                scan_id=coin_data.get("_scan_id"),
                decision_id=coin_data.get("_decision_id"),
                symbol=symbol,
                stage="EXECUTION",
                status="POST_FILL_PLAN_INVALIDATED",
                trade_id=trade_id,
                details={
                    "actual_entry": float(actual_entry),
                    "pre_submit_sl": pre_submit_sl,
                    "pre_submit_tp": pre_submit_tp,
                    "close_confirmed": bool(close_confirmed),
                    "block_reason": risk_manager.last_block_reason,
                },
                durable=True,
            )
            if close_confirmed:
                proposal = coin_data.get("user_visible_trade_proposal") or {}
                if proposal:
                    _proposal_ledger_update(
                        state_manager,
                        proposal,
                        "POST_FILL_PLAN_INVALIDATED_CLOSED",
                        entry_post_count=1,
                        executed_qty=float(amount),
                        actual_entry=float(actual_entry),
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                state_manager.remove_order_intent(symbol)
                notifier.send_message(
                    f"⚠️ <b>إلغاء صفقة بعد التعبئة</b>\n"
                    f"الزوج: <code>{symbol}</code>\n"
                    "غيّر الانزلاق صلاحية RR، فأُغلق المركز فوراً دون هدف مصطنع."
                )
                return False

            emergency_meta = {
                "trade_id": trade_id,
                "side": final_decision.upper(),
                "position_side": "LONG" if final_decision == "buy" else "SHORT",
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": float(actual_entry),
                "amount": float(amount),
                "original_amount": float(amount),
                "sl": 0.0,
                "tp": 0.0,
                "entry_reason": "POST_FILL_PLAN_INVALID_CLOSE_UNCONFIRMED",
                "strategy_source": "EMERGENCY_UNPROTECTED",
                "filter_profile": filter_profile.get("key", "strict"),
                "protection_status": "UNPROTECTED",
                "setup_thesis_id": coin_data.get("_thesis_id"),
                "user_visible_trade_proposal": coin_data.get(
                    "user_visible_trade_proposal"
                ),
            }
            proposal = coin_data.get("user_visible_trade_proposal") or {}
            if proposal:
                _proposal_ledger_update(
                    state_manager,
                    proposal,
                    "POST_FILL_PLAN_INVALID_CLOSE_UNCONFIRMED",
                    entry_post_count=1,
                    executed_qty=float(amount),
                    actual_entry=float(actual_entry),
                    reconciliation_required=True,
                )
            state_manager.save_entry_metadata(symbol, emergency_meta)
            state_manager.add_active_symbol(symbol)
            active_symbols.add(symbol)
            notifier.send_message(
                f"🚨 <b>مركز بلا خطة وإغلاقه غير مؤكد</b>\n"
                f"الزوج: <code>{symbol}</code>\nالتدخل اليدوي مطلوب فوراً."
            )
            return False

        _execution_event(
            "POST_FILL_PLAN_RECOMPUTED",
            {
                "actual_entry": float(actual_entry), "sl": float(sl), "tp": float(tp),
                "target_plan": dict(getattr(risk_manager, "last_plan", {}) or {}),
            },
            durable=True,
            exchange_order_id=order.get("id") if isinstance(order, dict) else None,
            position_id=trade_id,
        )
        
        logger.info(f"!!! تم تنفيذ إشارة تداول !!! لـ {symbol}")
        logger.info(f"سعر التنفيذ الفعلي: {actual_entry:.4f} | الهدف: {tp if tp is not None else 'N/A'} | الوقف: {sl if sl is not None else 'N/A'}")

        # ── بناء tp_steps في مسار التنفيذ الفوري (مثل مسار Virtual Order) ──
        if getattr(Config, "TP_LADDER_ENABLED", True) and sl:
            is_bf = (coin_data.get('winning_strategy') == "Macro Bottom Fishing & Trend Rider Strategy") or bool(coin_data.get('bottom_fishing_active', False))
            imm_tp_steps = risk_manager.get_tp_steps(final_decision, float(actual_entry), float(sl), tp3_override=float(tp) if tp else None, is_bottom_fishing=is_bf)
            if imm_tp_steps:
                coin_data["tp_steps"] = imm_tp_steps
                tp = imm_tp_steps[-1]["price"]
                milestones = " | ".join(
                    f"{step.get('label', f'TP{idx + 1}')}={step['price']:.5f}"
                    for idx, step in enumerate(imm_tp_steps)
                )
                logger.info(
                    f"[TPLadder] {'[Bottom Fishing]' if is_bf else ''} ✅ "
                    f"{symbol}: {milestones} (لا إغلاق جزئي)"
                )

        
        sentiment_label = (
            sentiment.get('label', 'neutral') if sentiment
            else "disabled" if not getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False)
            else "neutral"
        )
        logger.info("جاري وضع أوامر الحماية...")

        protection_meta = {}
        state_manager.transition_order_intent(
            symbol,
            ("SUBMITTING", "ENTRY_CONFIRMED", "PARTIALLY_FILLED", "ENTRY_FILLED"),
            "PROTECTING",
            updates={
                "actual_entry": float(actual_entry),
                "actual_amount": float(amount),
                "sl": float(sl or 0),
                "tp": float(tp or 0),
            },
        )
        # سياسة ثابتة: لا إغلاق جزئي. TP1/TP2 يحركان SL فقط، وTP3 يغلق كامل المركز.
        sl_tp_result = client.place_sl_tp(
            symbol,
            final_decision,
            amount,
            sl,
            tp,
            client_order_prefix=trade_id,
        )
        sl_ok = sl_tp_result.get('sl_ok', False)
        tp_ok = sl_tp_result.get('tp_ok', False)
        protection_meta = sl_tp_result
        _execution_event(
            "PROTECTION_RESULT",
            {
                key: protection_meta.get(key) for key in (
                    "sl_ok", "tp_ok", "sl_order_id", "sl_client_id",
                    "sl_actual_order_id", "tp_order_id", "tp_client_id",
                    "tp_actual_order_id", "working_type",
                )
            },
            durable=True,
            exchange_order_id=order.get("id") if isinstance(order, dict) else None,
            position_id=trade_id,
        )

        # A proposal is considered protected only when *both* original
        # protection legs are proven at the venue.  Running with SL-only or
        # TP-only would violate the user-visible contract and leave restart
        # recovery with a partially protected position.
        if sl_ok and tp_ok:
            state_manager.transition_order_intent(
                symbol,
                ("ENTRY_CONFIRMED", "PARTIALLY_FILLED", "ENTRY_FILLED", "PROTECTING"),
                "PROTECTED",
                updates={
                    "sl_order_id": protection_meta.get("sl_order_id"),
                    "sl_client_id": protection_meta.get("sl_client_id"),
                    "sl_actual_order_id": protection_meta.get("sl_actual_order_id"),
                    "tp_order_id": protection_meta.get("tp_order_id"),
                    "tp_client_id": protection_meta.get("tp_client_id"),
                    "tp_actual_order_id": protection_meta.get("tp_actual_order_id"),
                },
            )

        if sl_ok != tp_ok:
            missing_leg = "TP" if sl_ok else "SL"
            logger.critical(
                f"⚠️ [{symbol}] حماية جزئية غير مقبولة؛ الساق المفقودة: "
                f"{missing_leg}. سيُغلق مركز Demo بأمان."
            )
            notifier.send_message(
                f"⚠️ <b>حماية Demo غير مكتملة [{symbol}]</b>\n"
                f"الساق المفقودة: <code>{missing_leg}</code>\n"
                "تم إيقاف هذا المسار وبدأ الإغلاق الآمن."
            )

        sl_tp_success = sl_ok and tp_ok

        if not sl_tp_success:
            logger.critical(f"⚠️ فشل وضع أوامر الحماية للزوج {symbol}! جاري إغلاق الصفقة...")
            close_confirmed = client.close_position(symbol, final_decision, amount)
            if not close_confirmed:
                # لا ننظف مركزاً لم تؤكد Binance إغلاقه. نحفظه كحالة طوارئ
                # حتى يواصل monitor إعادة التحقق ويظهر للمستخدم كمركز عارٍ.
                emergency_meta = {
                    "trade_id": trade_id,
                    "side": final_decision.upper(),
                    "position_side": "LONG" if final_decision == "buy" else "SHORT",
                    "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": float(actual_entry),
                    "amount": float(amount),
                    "original_amount": float(amount),
                    "sl": float(sl or 0),
                    "tp": float(tp or 0),
                    "entry_reason": "PROTECTION_FAILED_CLOSE_UNCONFIRMED",
                    "strategy_source": "EMERGENCY_UNPROTECTED",
                    "filter_profile": filter_profile.get("key", "strict"),
                    "protection_status": "UNPROTECTED",
                    "user_visible_trade_proposal": coin_data.get(
                        "user_visible_trade_proposal"
                    ),
                }
                proposal = coin_data.get("user_visible_trade_proposal") or {}
                if proposal:
                    _proposal_ledger_update(
                        state_manager,
                        proposal,
                        "PROTECTION_FAILED_CLOSE_UNCONFIRMED",
                        entry_post_count=1,
                        executed_qty=float(amount),
                        actual_entry=float(actual_entry),
                        sl_ok=bool(sl_ok),
                        tp_ok=bool(tp_ok),
                        reconciliation_required=True,
                    )
                state_manager.save_entry_metadata(symbol, emergency_meta)
                state_manager.add_active_symbol(symbol)
                active_symbols.add(symbol)
                notifier.send_message(
                    f"🚨🚨 <b>صفقة غير محمية وإغلاقها غير مؤكد</b>\n"
                    f"الزوج: <code>{symbol}</code>\n"
                    "فشل SL وفشل تأكيد أمر الإغلاق. الحالة ستبقى تحت المراقبة، "
                    "لكن التدخل اليدوي مطلوب فوراً."
                )
                return False

            proposal = coin_data.get("user_visible_trade_proposal") or {}
            if proposal:
                _proposal_ledger_update(
                    state_manager,
                    proposal,
                    "PROTECTION_FAILED_EMERGENCY_CLOSED",
                    sl_ok=bool(sl_ok),
                    tp_ok=bool(tp_ok),
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )

            notifier.send_message(
                f"⚠️ <b>تنبيه طارئ:</b> تم إغلاق صفقة {symbol} وتأكيد صفر المركز "
                "بسبب فشل وضع الوقف والهدف."
            )
            active_symbols.discard(symbol)
            state_manager.remove_active_symbol(symbol)
            state_manager.remove_order_intent(symbol)
            
            # عند فشل الحماية، احفظ تقرير فشل دخول/حماية
            try:
                emergency_exit_details = {
                    "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_price": actual_entry,
                    "pnl": 0.0,
                    "pnl_pct": 0.0,
                    "exit_reason": "PROTECTION_FAILED_EMERGENCY_CLOSE",
                    "slippage": 0.0,
                }

                TradeJournal().finalize_trade(
                    client=client,
                    symbol=symbol,
                    entry_meta={
                        "trade_id": trade_id,
                        "side": final_decision.upper(),
                        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_price": float(actual_entry),
                        "amount": float(amount),
                        "sl": float(sl),
                        "tp": float(tp),
                        "entry_reason": "Protection failed after entry",
                        "strategy_source": "PROTECTION_FAILED",
                        "filter_profile": filter_profile.get("key", "strict"),
                        "reference_balance": float(state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0))),
                    },
                    exit_details=emergency_exit_details,
                )
                if trade_id:
                    try:
                        from utils.forensics_logger import update_signal_outcome
                        update_signal_outcome(
                            trade_id,
                            "PROTECTION_FAILED_EMERGENCY_CLOSE",
                            0.0,
                            root_cause="PROTECTION_FAILED",
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[TradeJournal] فشل تسجيل الإغلاق الطارئ لـ {symbol}: {e}")

            return False
        
        try:
            latest_bar = bars_df.iloc[-2] if bars_df is not None and len(bars_df) > 1 else {}
            wallet_equity_at_entry, wallet_available_at_entry = get_wallet_equity_snapshot(client, state_manager=state_manager)
            wallet_equity_at_entry = float(wallet_equity_at_entry or 0.0)
            wallet_available_at_entry = float(wallet_available_at_entry or 0.0)
            reference_balance = float(state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0)))

            entry_meta = {
                "trade_id": trade_id,
                "scan_id": coin_data.get("_scan_id"),
                "decision_id": coin_data.get("_decision_id"),
                "proposal_id": (
                    coin_data.get("user_visible_trade_proposal") or {}
                ).get("proposal_id"),
                "user_visible_trade_proposal": coin_data.get(
                    "user_visible_trade_proposal"
                ),
                "wallet_equity_at_entry": wallet_equity_at_entry,
                "wallet_available_at_entry": wallet_available_at_entry,
                "reference_balance": reference_balance,
                "exchange_order_id": order.get("id") if isinstance(order, dict) else None,
                "exchange_order": {
                    "id": order.get("id"),
                    "clientOrderId": order.get("clientOrderId"),
                    "status": order.get("status"),
                    "type": order.get("type"),
                    "side": order.get("side"),
                    "price": order.get("price"),
                    "average": order.get("average"),
                    "filled": order.get("filled"),
                    "amount": order.get("amount"),
                    "cost": order.get("cost"),
                } if isinstance(order, dict) else {},
                "side": final_decision.upper(),
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": float(actual_entry),
                "amount": float(amount),
                "sl": float(sl) if sl is not None else 0.0,
                "initial_sl": float(sl) if sl is not None else 0.0,
                "tp": float(tp) if tp is not None else 0.0,
                "entry_reason": coin_data.get('reason', f"فني:{ta_trend}"),
                "strategy_source": coin_data.get('winning_strategy', 'unknown'),
                "tp_steps":     coin_data.get("tp_steps", []),
                "sl_timeout":   {},    # يُملأ عند لمس SL
                "ttp_state":    {},    # يُملأ عند تفعيل TTP
                "sentiment_score": float(sentiment.get('score') or sentiment.get('galaxy_score') or 0.0) if sentiment else 0.0,
                "adx": float(latest_bar.get('adx') or 0.0),
                "ema_200": float(latest_bar.get('ema_200') or 0.0),
                "atr": float(latest_bar.get('atr') or 0.0),
                "spread": round((float(ticker.get('ask') or 0) - float(ticker.get('bid') or 0)) / float(ticker.get('bid')), 6) if ticker and ticker.get('bid') and float(ticker.get('bid')) > 0 else 0.0,
                "volume_24h": float(ticker.get('quoteVolume') or 0.0) if ticker else 0.0,
                "risk_pct": float(trade_risk_pct * 100),
                "leverage": int(trade_leverage or 1),
                "step_trail_active": False,
                "highest_tp_step_reached": 0,
                "tp1": float((coin_data.get("tp_steps") or [{"price": tp}])[0]["price"]),
                "sl_order_id": protection_meta.get("sl_order_id"),
                "sl_client_id": protection_meta.get("sl_client_id"),
                "sl_actual_order_id": protection_meta.get("sl_actual_order_id"),
                "tp_order_id": protection_meta.get("tp_order_id"),
                "tp_client_id": protection_meta.get("tp_client_id"),
                "tp_actual_order_id": protection_meta.get("tp_actual_order_id"),
                "protection_working_type": protection_meta.get("working_type", "MARK_PRICE"),
                "protection_status": "PROTECTED",
                "original_amount": float(amount),
                "current_sl": float(sl),
                "filter_profile": filter_profile.get("key", "strict"),
                "setup_thesis_id": coin_data.get("_thesis_id"),
                "market_regime": (coin_data.get("_ta_context") or {}).get(
                    "market_regime", "UNKNOWN"
                ),
                "mfe_r": 0.0,
                "mae_r": 0.0,
                "entry_confirmation": entry_confirmation,
                "target_plan": dict(getattr(risk_manager, "last_plan", {}) or target_plan),
            }
            # ==================================================
            # Trade Journal: تبرير الدخول وسجل الشموع
            # ==================================================
            try:
                planned_risk = abs(float(actual_entry) - float(sl)) if sl is not None else 0.0
                planned_reward = abs(float(tp) - float(actual_entry)) if tp is not None else 0.0
                planned_rr = planned_reward / planned_risk if planned_risk > 0 else 0.0
                entry_meta["planned_rr"] = round(planned_rr, 6)
                entry_meta["planned_risk_usdt"] = round(
                    planned_risk * float(amount), 8
                )

                decision_context = {
                    "symbol": symbol,
                    "scan_id": coin_data.get("_scan_id"),
                    "decision_id": coin_data.get("_decision_id"),
                    "side": final_decision,
                    "filter_profile": filter_profile,
                    "ta_trend": ta_trend,
                    "entry_reason": coin_data.get("reason", ""),
                    "sentiment": sentiment or {},
                    "entry_confirmation": entry_confirmation,
                    "setup_thesis_id": coin_data.get("_thesis_id"),
                    "setup_context": coin_data.get("_ta_context", {}),
                    "target_plan": coin_data.get("target_plan", target_plan),
                    "support": coin_data.get("support"),
                    "resistance": coin_data.get("resistance"),
                    "invalidation_level": invalidation_level,
                    "risk_plan": {
                        "entry": float(actual_entry),
                        "sl": float(sl),
                        "tp": float(tp),
                        "planned_rr": round(planned_rr, 4),
                        "atr": float(atr_val or 0.0),
                        "risk_pct": float(trade_risk_pct * 100),
                        "leverage": int(trade_leverage),
                    },
                    "market_levels_summary": {
                        "bullish_obs": len((coin_data.get("market_levels") or {}).get("bullish_obs", [])),
                        "bearish_obs": len((coin_data.get("market_levels") or {}).get("bearish_obs", [])),
                        "bullish_fvgs": len((coin_data.get("market_levels") or {}).get("bullish_fvgs", [])),
                        "bearish_fvgs": len((coin_data.get("market_levels") or {}).get("bearish_fvgs", [])),
                    },
                }

                entry_meta["decision_context"] = decision_context

                journal_dir = TradeJournal().record_entry(
                    client=client,
                    symbol=symbol,
                    entry_meta=entry_meta,
                    decision_context=decision_context,
                )

                if journal_dir:
                    entry_meta["journal_dir"] = journal_dir

            except Exception as e:
                logger.error(f"[TradeJournal] فشل تسجيل مبررات دخول الصفقة {symbol}: {e}")

            saved = state_manager.save_entry_metadata(symbol, entry_meta)
            if saved:
                logger.info(f"✅ تم حفظ بيانات دخول صفقة {symbol} بنجاح.")
                state_manager.remove_order_intent(symbol)
                _execution_event(
                    "ENTRY_METADATA_PERSISTED",
                    {"journal_dir": entry_meta.get("journal_dir"), "protection_status": "PROTECTED"},
                    durable=True,
                    exchange_order_id=order.get("id") if isinstance(order, dict) else None,
                    position_id=trade_id,
                )
                # ── Decision Observer: تسجيل فتح الصفقة ─────────────────
                # يحدث فقط في مسار النجاح الثلاثي: order ناجح + SL/TP وُضعا + save نجح
                try:
                    import builtins as _bi
                    _observer = getattr(_bi, "_global_observer", None)
                    _critic   = getattr(_bi, "_global_ai_critic", None)
                    if _observer is not None:
                        _planned_rr = 0.0
                        try:
                            _risk_abs = abs(float(actual_entry) - float(sl))
                            _rwd_abs  = abs(float(tp) - float(actual_entry))
                            if _risk_abs > 0:
                                _planned_rr = round(_rwd_abs / _risk_abs, 4)
                        except Exception:
                            pass
                        _snap_15m = None
                        try:
                            if bars_df is not None and len(bars_df) > 1:
                                _r = bars_df.iloc[-2]
                                _snap_15m = {
                                    "close":   float(_r.get("close",   0) or 0),
                                    "rsi":     round(float(_r.get("rsi",     0) or 0), 2),
                                    "atr":     round(float(_r.get("atr",     0) or 0), 6),
                                    "adx":     round(float(_r.get("adx",     0) or 0), 2),
                                    "ema_200": round(float(_r.get("ema_200", 0) or 0), 6),
                                }
                        except Exception:
                            pass
                        _observer.record_significant_event(
                            event={
                                "event_type":          "TRADE_OPENED",
                                "symbol":              symbol,
                                "side":                final_decision,
                                "bot_decision":        f"OPEN_{final_decision.upper()}",
                                "bot_reasoning":       coin_data.get("reason", ""),
                                "entry_quality_score": float(
                                    ((coin_data or {}).get("_ta_context") or {}).get("score", 0) or 0
                                ),
                                "price_at_decision":   float(actual_entry),
                                "linked_trade_id":     trade_id,
                                "entry_price":         float(actual_entry),
                                "sl_price":            float(sl) if sl else 0.0,
                                "tp_price":            float(tp) if tp else 0.0,
                                "planned_rr":          _planned_rr,
                                "sl_source":           coin_data.get("sl_source"),
                                "mtf_snapshot":        {"15m": _snap_15m},
                            },
                            ai_engine_critic=_critic,
                        )
                except Exception as _obs_exc:
                    logger.debug(f"[DecisionObserver] فشل تسجيل TRADE_OPENED: {_obs_exc}")
            else:
                _execution_event(
                    "ENTRY_METADATA_PERSIST_FAILED",
                    {"position_remains_managed_by_order_intent": True},
                    durable=True,
                    exchange_order_id=order.get("id") if isinstance(order, dict) else None,
                    position_id=trade_id,
                )
                logger.error(f"❌ فشل حفظ entry_metadata للصفقة {symbol}! سيكون التسجيل غير دقيق عند الإغلاق.")
        except Exception as e:
            logger.error(f"خطأ أثناء حفظ بيانات دخول الصفقة: {e}")
        
        trailing_manager.register_trade(
            symbol=symbol,
            side=final_decision,
            entry_price=actual_entry,
            initial_sl=sl,
            amount=amount,
        )
    
        emoji = '🟢 BUY' if final_decision == 'buy' else '🔴 SELL'
        coin_data['decision'] = emoji
        coin_data['reason'] = f"تم التنفيذ | السعر: {actual_entry:.4f}"
        scan_results.append(coin_data)
        
        state_manager.add_active_symbol(symbol)
        active_symbols.add(symbol)  # تحديث المتغير المحلي فقط
        # لا ننتظر Telegram بينما المركز بلا حماية. التنبيه يأتي فقط بعد وضع
        # SL وحفظ metadata/active_symbols بنجاح أو بقائها قابلة للاسترداد.
        notifier.send_trade_alert(
            symbol, final_decision, amount, actual_entry, sl, tp, sentiment_label
        )
        proposal = coin_data.get("user_visible_trade_proposal") or {}
        if proposal:
            _proposal_ledger_update(
                state_manager,
                proposal,
                "ENTRY_FILLED_AND_PROTECTED",
                actual_entry=float(actual_entry),
                filled_amount=float(amount),
                sl=float(sl),
                tp=float(tp),
            )
        audit_decision_event(
            scan_id=coin_data.get("_scan_id"),
            decision_id=coin_data.get("_decision_id"),
            signal_id=coin_data.get("_decision_id"),
            setup_id=coin_data.get("_thesis_id"),
            thesis_id=coin_data.get("_thesis_id"),
            exchange_order_id=(order.get("id") if isinstance(order, dict) else None),
            position_id=trade_id,
            symbol=symbol,
            stage="EXECUTION",
            status="EXECUTED",
            trade_id=trade_id,
            details={
                "side": final_decision.upper(),
                "entry_price": float(actual_entry),
                "setup_score": (coin_data.get("_ta_context") or {}).get("score"),
                "trend_context": {
                    "htf_trend": (coin_data.get("_ta_context") or {}).get("htf_trend"),
                    "countertrend": (coin_data.get("_ta_context") or {}).get("countertrend"),
                },
                "structure": (coin_data.get("_ta_context") or {}).get("setup_anchor"),
                "entry_reason": coin_data.get("reason"),
                "confirmation": entry_confirmation,
                "liquidity_target": dict(
                    getattr(risk_manager, "last_plan", {}) or target_plan
                ),
                "expected_rr": (
                    dict(getattr(risk_manager, "last_plan", {}) or target_plan).get("rr")
                ),
                "invalidation": invalidation_level,
                "setup_thesis_id": coin_data.get("_thesis_id"),
                "market_regime": (coin_data.get("_ta_context") or {}).get(
                    "market_regime", "UNKNOWN"
                ),
                "reentry_status": "NEW_OR_COOLDOWN_CLEARED",
                "risk_pct": float(trade_risk_pct * 100),
                "leverage": int(trade_leverage or 1),
            },
            durable=True,
        )
        logger.info(f"تم فتح صفقة لـ {symbol}. إجمالي الصفقات المفتوحة: {len(active_symbols)}")
        return True
    execution_failure = dict(
        getattr(client, "last_execution_failure", None) or {}
    )
    local_pre_submit = bool(
        execution_failure
        and execution_failure.get("send_attempted") is False
    )
    failure_code = str(execution_failure.get("code") or "")
    explicit_exchange_reject = failure_code == "ENTRY_REJECTED_BY_EXCHANGE"
    ambiguous_transport = failure_code == "ORDER_SUBMISSION_UNRESOLVED"
    intent_archived = False
    if local_pre_submit or explicit_exchange_reject:
        # لم يصل create_order إلى Binance إطلاقاً، لذلك يمكن إنهاء النية
        # بأمان. الرفض الصريح من Binance نهائي أيضاً ولا يحتاج reconciliation.
        terminal_status = (
            "ENTRY_REJECTED_BY_EXCHANGE"
            if explicit_exchange_reject else "ENTRY_LOCAL_PRECHECK_FAILED"
        )
        intent_archived = state_manager.archive_order_intent(
            symbol,
            terminal_status,
            proofs={
                "execution_failure": execution_failure,
                "order_sent": bool(explicit_exchange_reject),
                "reconciliation_required": False,
            },
        )
    else:
        terminal_status = (
            "ENTRY_TRANSPORT_AMBIGUOUS"
            if ambiguous_transport else "ENTRY_STATUS_UNKNOWN"
        )
    proposal = coin_data.get("user_visible_trade_proposal") or proposal or {}
    if proposal:
        _proposal_ledger_update(
            state_manager,
            proposal,
            terminal_status,
            entry_post_count=0 if local_pre_submit else 1,
            execution_failure=execution_failure,
            reconciliation_required=not (local_pre_submit or explicit_exchange_reject),
        )
    _execution_event(
        terminal_status,
        {
            "execution_failure": execution_failure,
            "intent_archived": intent_archived,
        },
        durable=True,
    )
    return False

# ── قفل عالمي في الذاكرة لمنع الإغلاق المزدوج ──────────────────────
# يمنع دخول handle_trade_close مرتين لنفس الرمز (حتى لو كانت metadata فارغة)
_closing_lock: set = set()
_closing_guard = threading.Lock()


def persist_bot_close_intent(state_manager, symbol, reason):
    """حفظ سبب الإغلاق الذي طلبه البوت قبل إرسال أمر السوق."""
    meta = state_manager.get_entry_metadata(symbol)
    if not meta:
        return False
    meta["pending_close_reason"] = str(reason).upper()
    meta["pending_close_requested_at"] = time.time()
    return bool(state_manager.save_entry_metadata(symbol, meta))


def update_trade_excursions(entry_meta, current_price, symbol=None):
    """تحديث MFE/MAE بوحدة R باستخدام وقف الدخول الأصلي، لا الوقف المتحرك."""
    try:
        price = float(current_price or 0.0)
        entry = float(entry_meta.get("entry_price") or 0.0)
        initial_sl = float(
            entry_meta.get("initial_sl") or entry_meta.get("sl") or 0.0
        )
        risk_per_unit = abs(entry - initial_sl)
        if price <= 0 or entry <= 0 or risk_per_unit <= 0:
            return False

        side = str(entry_meta.get("side") or "BUY").upper()
        sign = 1.0 if side in ("BUY", "LONG") else -1.0
        directional_r = sign * (price - entry) / risk_per_unit
        favorable_r = max(0.0, directional_r)
        adverse_r = max(0.0, -directional_r)
        changed = False
        old_mfe_bucket = int(float(entry_meta.get("mfe_r") or 0.0) * 4)
        old_mae_bucket = int(float(entry_meta.get("mae_r") or 0.0) * 4)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        if favorable_r > float(entry_meta.get("mfe_r") or 0.0):
            entry_meta["mfe_r"] = round(favorable_r, 6)
            entry_meta["mfe_price"] = price
            entry_meta["mfe_at"] = now_iso
            changed = True
        if adverse_r > float(entry_meta.get("mae_r") or 0.0):
            entry_meta["mae_r"] = round(adverse_r, 6)
            entry_meta["mae_price"] = price
            entry_meta["mae_at"] = now_iso
            changed = True
        new_mfe_bucket = int(float(entry_meta.get("mfe_r") or 0.0) * 4)
        new_mae_bucket = int(float(entry_meta.get("mae_r") or 0.0) * 4)
        if symbol and (new_mfe_bucket > old_mfe_bucket or new_mae_bucket > old_mae_bucket):
            audit_decision_event(
                scan_id=entry_meta.get("scan_id"),
                decision_id=entry_meta.get("decision_id"),
                setup_id=entry_meta.get("setup_thesis_id"),
                thesis_id=entry_meta.get("setup_thesis_id"),
                position_id=entry_meta.get("trade_id"),
                trade_id=entry_meta.get("trade_id"),
                symbol=symbol,
                stage="POSITION_EXCURSION",
                status="HIGH_WATER_0_25R",
                details={
                    "current_price": price,
                    "mfe_r": entry_meta.get("mfe_r", 0.0),
                    "mae_r": entry_meta.get("mae_r", 0.0),
                },
            )
        return changed
    except (TypeError, ValueError):
        return False


POST_EXIT_HORIZONS_MINUTES = (15, 30, 60, 120)
STOP_THEN_RUN_THRESHOLDS_R = (0.5, 1.0, 1.5, 2.0)


def register_post_exit_observation(state_manager, symbol, entry_meta, exit_details, *, is_stop):
    """Register a read-only post-exit observation; it has no execution authority."""
    entry = float(entry_meta.get("entry_price") or 0.0)
    initial_sl = float(entry_meta.get("initial_sl") or entry_meta.get("sl") or 0.0)
    exit_price = float(exit_details.get("exit_price") or 0.0)
    risk = abs(entry - initial_sl)
    if min(entry, initial_sl, exit_price, risk) <= 0:
        return False
    side = str(entry_meta.get("side") or "BUY").upper()
    trade_id = str(
        entry_meta.get("trade_id") or entry_meta.get("position_id")
        or f"{symbol}:{int(time.time())}"
    )
    now = time.time()
    sign = 1.0 if side in ("BUY", "LONG") else -1.0
    exit_r_from_entry = sign * (exit_price - entry) / risk
    item = {
        "observation_id": trade_id,
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_price": entry,
        "initial_sl": initial_sl,
        "initial_risk_per_unit": risk,
        "exit_price": exit_price,
        "exit_at_epoch": now,
        "exit_time": exit_details.get("exit_time"),
        "exit_reason": exit_details.get("exit_reason"),
        "realized_r": float(exit_details.get("final_r") or 0.0),
        "exit_r_from_entry": float(exit_r_from_entry),
        "is_stop_exit": bool(is_stop),
        "post_exit_mfe_r": float(exit_r_from_entry),
        "post_exit_mae_r": float(exit_r_from_entry),
        "max_post_exit_price": exit_price,
        "min_post_exit_price": exit_price,
        "checkpoints": {},
        "stop_then_run": [],
        "next_check_at": now,
        "complete": False,
        "read_only": True,
    }
    saved = state_manager.register_post_exit_observation(item)
    if saved:
        audit_decision_event(
            symbol=symbol, trade_id=trade_id, position_id=trade_id,
            stage="POST_EXIT_OBSERVATION", status="REGISTERED",
            details={
                "is_stop_exit": bool(is_stop), "exit_price": exit_price,
                "realized_r": item["realized_r"],
                "horizons_minutes": list(POST_EXIT_HORIZONS_MINUTES),
            }, durable=True,
        )
    return saved


def monitor_post_exit_observations(client, state_manager, now=None):
    """Measure post-exit MFE/MAE at fixed horizons without placing any order."""
    now = float(now or time.time())
    observations = state_manager.get_post_exit_observations()
    if not isinstance(observations, dict) or not observations:
        return 0
    changed = 0
    for key, item in observations.items():
        if item.get("complete") or now < float(item.get("next_check_at") or 0.0):
            continue
        try:
            price = float(client.get_current_price(item["symbol"]) or 0.0)
            if price <= 0:
                item["next_check_at"] = now + 60.0
                continue
            entry = float(item["entry_price"])
            risk = float(item["initial_risk_per_unit"])
            side = str(item.get("side") or "BUY").upper()
            sign = 1.0 if side in ("BUY", "LONG") else -1.0
            directional_r = sign * (price - entry) / risk
            item["post_exit_mfe_r"] = max(
                float(item.get("post_exit_mfe_r") or directional_r), directional_r
            )
            item["post_exit_mae_r"] = min(
                float(item.get("post_exit_mae_r") or directional_r), directional_r
            )
            item["max_post_exit_price"] = max(
                float(item.get("max_post_exit_price") or price), price
            )
            item["min_post_exit_price"] = min(
                float(item.get("min_post_exit_price") or price), price
            )
            elapsed_minutes = max(0.0, (now - float(item["exit_at_epoch"])) / 60.0)
            checkpoints = item.setdefault("checkpoints", {})
            for horizon in POST_EXIT_HORIZONS_MINUTES:
                label = f"{horizon}m"
                if elapsed_minutes >= horizon and label not in checkpoints:
                    checkpoints[label] = {
                        "observed_at_epoch": now,
                        "price": price,
                        "directional_r": directional_r,
                        "post_exit_mfe_r": item["post_exit_mfe_r"],
                        "post_exit_mae_r": item["post_exit_mae_r"],
                    }
                    audit_decision_event(
                        symbol=item["symbol"], trade_id=item.get("trade_id"),
                        position_id=item.get("trade_id"),
                        stage="POST_EXIT_MFE", status=label.upper(),
                        details=checkpoints[label], durable=True,
                    )
                    if getattr(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", False):
                        try:
                            collect_post_exit_checkpoint(
                                storage=_strategy_v2_shadow_store(),
                                trade_id=str(item.get("trade_id") or ""),
                                horizon=label,
                                checkpoint=checkpoints[label],
                            )
                        except Exception as shadow_error:
                            audit_decision_event(
                                symbol=item.get("symbol"),
                                trade_id=item.get("trade_id"),
                                stage="STRATEGY_V2_SHADOW_OUTCOME",
                                status="SHADOW_COLLECTION_ERROR_V1_UNCHANGED",
                                details={"error_type": type(shadow_error).__name__},
                            )
            if item.get("is_stop_exit"):
                achieved = [
                    level for level in STOP_THEN_RUN_THRESHOLDS_R
                    if float(item["post_exit_mfe_r"]) >= level
                ]
                previous = set(float(v) for v in item.get("stop_then_run", []))
                new_levels = [level for level in achieved if level not in previous]
                if new_levels:
                    item["stop_then_run"] = sorted(previous.union(new_levels))
                    audit_decision_event(
                        symbol=item["symbol"], trade_id=item.get("trade_id"),
                        position_id=item.get("trade_id"),
                        stage="STOP_THEN_RUN", status="DETECTED",
                        details={
                            "new_thresholds_r": new_levels,
                            "post_exit_mfe_r": item["post_exit_mfe_r"],
                            "realized_r": item.get("realized_r"),
                        }, durable=True,
                    )
            item["next_check_at"] = now + 60.0
            if elapsed_minutes >= max(POST_EXIT_HORIZONS_MINUTES):
                item["complete"] = True
                item["completed_at_epoch"] = now
            changed += 1
        except Exception as exc:
            item["last_error"] = type(exc).__name__
            item["next_check_at"] = now + 120.0
            logger.warning(
                f"[PostExit] تعذر قياس {item.get('symbol')}: {type(exc).__name__}"
            )
    if changed:
        state_manager.save_post_exit_observations(observations)
    return changed


def _run_post_close_enrichment(client, notifier, symbol, entry_meta, exit_details):
    """أعمال التقارير البطيئة؛ تعمل بعد تثبيت الإغلاق وتنظيف الحالة."""
    try:
        journal_dir = TradeJournal().finalize_trade(
            client=client,
            symbol=symbol,
            entry_meta=dict(entry_meta),
            exit_details=dict(exit_details),
        )
        if journal_dir:
            logger.info(f"[TradeJournal] ملفات تحليل الصفقة محفوظة في: {journal_dir}")

        try:
            feedback = TradeJournal().build_post_trade_feedback(entry_meta, exit_details)
            feedback_text = feedback.get("feedback_text", "")
            if feedback_text:
                if notifier:
                    notifier.send_message(feedback_text)
                else:
                    logger.info(f"[Feedback] {feedback_text}")
        except Exception as exc:
            logger.warning(f"[Feedback] فشل بناء التغذية الراجعة: {exc}")

        if not getattr(Config, "AI_CRITIQUE_ENABLED", False):
            return
        try:
            import builtins as _bi
            ai_engine = getattr(_bi, "_global_ai_engine", None)
            timeframe = getattr(Config, "AI_CRITIQUE_TIMEFRAME", "15m")
            csv_path = os.path.join(journal_dir or "", f"trade_window_{timeframe}.csv")
            if ai_engine and journal_dir and os.path.exists(csv_path):
                candles_df = pd.read_csv(csv_path)
                critique = ai_engine.generate_trade_critique(
                    symbol=symbol,
                    entry_meta=entry_meta,
                    exit_details=exit_details,
                    candles_df=candles_df,
                )
                if critique:
                    if notifier:
                        notifier.send_message(critique)
                    else:
                        logger.info(f"[AI Critique] {critique}")
        except Exception as exc:
            logger.warning(f"[AI Critique] فشل توليد نقد الذكاء الاصطناعي: {exc}")
    except Exception as exc:
        logger.error(f"[TradeJournal] فشل إنهاء سجل الصفقة {symbol}: {exc}")

def handle_trade_close(alert, state_manager, client=None, notifier=None):
    """
    معالجة إغلاق الصفقة: لا نحذف بيانات الدخول إلا بعد نجاح التسجيل.
    """
    global _closing_lock
    symbol = alert["symbol"].split(":")[0]

    # ── فحص القفل العالمي أولاً (يحمي حتى عند غياب entry_meta) ─────
    with _closing_guard:
        if symbol in _closing_lock:
            logger.debug(f"[{symbol}] الإغلاق قيد المعالجة بالفعل.")
            return CLOSE_ALREADY_PROCESSING
        _closing_lock.add(symbol)

    try:
        return _handle_trade_close_inner(alert, state_manager, client, symbol, notifier=notifier)
    finally:
        with _closing_guard:
            _closing_lock.discard(symbol)

def _handle_trade_close_inner(alert, state_manager, client, symbol, notifier=None):
    """المنطق الداخلي لإغلاق الصفقة — يُستدعى من handle_trade_close فقط."""
    # نقرأ بيانات الدخول بدون حذفها أولاً
    entry_meta = state_manager.get_entry_metadata(symbol)

    if entry_meta and entry_meta.get("_closing"):
        logger.warning(f"[{symbol}] تجاهل: الصفقة قيد الإغلاق بالفعل (_closing flag).")
        return CLOSE_ALREADY_PROCESSING
    # ضع العلامة فوراً لتجنب التسجيل المزدوج
    if entry_meta:
        entry_meta["_closing"] = True
        state_manager.save_entry_metadata(symbol, entry_meta)

    if not entry_meta:
        # ── منع تكرار تسجيل الصفقة المغلقة ──
        try:
            from utils.db_manager import get_recent_trades
            recent_trades = get_recent_trades(limit=30)
            alert_pnl = float(alert.get("pnl", 0.0) or 0.0)
            alert_price = float(alert.get("price", 0.0) or 0.0)
            
            already_logged = False
            for trade in recent_trades:
                if trade.get("symbol") == symbol:
                    pnl_match = abs(float(trade.get("pnl", 0.0) or 0.0) - alert_pnl) < 0.0001
                    price_match = abs(float(trade.get("exit_price", 0.0) or 0.0) - alert_price) < 0.0001
                    if pnl_match and price_match:
                        exit_time_str = trade.get("exit_time", "")
                        if exit_time_str:
                            try:
                                exit_time = datetime.strptime(exit_time_str, "%Y-%m-%d %H:%M:%S")
                                if (datetime.now() - exit_time).total_seconds() < 24 * 3600:
                                    already_logged = True
                                    break
                            except Exception:
                                already_logged = True
                                break
            
            if already_logged:
                logger.info(f"[{symbol}] تجاهل الإغلاق المزدوج: الصفقة مغلقة ومسجلة بالفعل في قاعدة البيانات.")
                return CLOSE_ALREADY_PROCESSING
        except Exception as check_err:
            logger.error(f"[DuplicateCheck] خطأ أثناء التحقق من تكرار تسجيل إغلاق الصفقة لـ {symbol}: {check_err}")

        logger.warning(f"⚠️ لا توجد entry_metadata للصفقة المغلقة {symbol}. سيتم استخدام بيانات طوارئ من التنبيه.")
        # Fallback: بناء entry_meta طوارئ من بيانات التنبيه المتاحة
        entry_meta = {
            "side": alert.get("side", "").upper(),
            "entry_time": alert.get("entry_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "entry_price": float(alert.get("entry_price", 0.0) or 0.0),
            "amount": float(alert.get("qty", 0.0) or 0.0),
            "sl": 0.0,
            "tp": 0.0,
            "entry_reason": "METADATA_LOST_FALLBACK",
            "strategy_source": "METADATA_LOST",
            "_metadata_source": "LOST_FALLBACK",
            "wallet_equity_at_entry": 0.0,
            "reference_balance": float(state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0))),
            "filter_profile": "unknown",
        }
        logger.info(f"[Fallback] تم بناء entry_meta طوارئ للصفقة {symbol}: {entry_meta}")

        # ── محاولة استرداد entry_price من المنصة مباشرة ────────────
        if entry_meta.get("entry_price", 0) == 0 and client is not None:
            try:
                positions = client.exchange.fetch_positions([symbol])
                for pos in (positions or []):
                    ep = float(
                        pos.get("entryPrice")
                        or pos.get("info", {}).get("entryPrice", 0)
                        or 0
                    )
                    if ep > 0:
                        entry_meta["entry_price"] = ep
                        entry_meta["amount"] = float(
                            pos.get("contracts")
                            or pos.get("info", {}).get("positionAmt", 0)
                            or entry_meta.get("amount", 0)
                        )
                        logger.info(
                            f"[MetadataRecovery] ✅ استُعيد entry_price={ep} "
                            f"لـ {symbol} من المنصة"
                        )
                        break
            except Exception as rec_err:
                logger.warning(
                    f"[MetadataRecovery] فشل استرداد entry_price لـ {symbol}: {rec_err}"
                )

    close_type = str(alert.get("type", "UNKNOWN_CLOSE")).upper()
    is_sl = "STOP" in close_type
    is_tp = "TAKE_PROFIT" in close_type or close_type == "TP"
    if is_sl:
        target_exit = entry_meta.get("current_sl") or entry_meta.get("sl")
    elif is_tp:
        target_exit = entry_meta.get("tp")
    else:
        target_exit = None

    actual_exit = float(alert.get("price", 0.0) or 0.0)
    if actual_exit > 0:
        update_trade_excursions(entry_meta, actual_exit, symbol=symbol)

    slippage_exit = 0.0
    if target_exit and float(target_exit) > 0 and actual_exit > 0:
        slippage_exit = abs(actual_exit - float(target_exit)) / float(target_exit)

    pnl_val = float(alert.get("pnl", 0.0) or 0.0)
    prior_reduction_pnl = float(
        entry_meta.get("prior_reduction_realized_pnl")
        or entry_meta.get("partial_realized_pnl")
        or 0.0
    )
    pnl_val += prior_reduction_pnl

    entry_price = float(entry_meta.get("entry_price", 0.0) or 0.0)
    amount = float(entry_meta.get("original_amount") or entry_meta.get("amount", 0.0) or 0.0)
    entry_notional = abs(entry_price * amount)
    leverage = float(entry_meta.get("leverage", 1) or 1)
    margin = entry_notional / leverage if leverage > 0 else entry_notional
    pnl_pct = (pnl_val / margin * 100) if margin > 0 else 0.0
    planned_risk_usdt = float(entry_meta.get("planned_risk_usdt") or 0.0)
    if planned_risk_usdt <= 0:
        initial_sl = float(
            entry_meta.get("initial_sl") or entry_meta.get("sl") or 0.0
        )
        planned_risk_usdt = abs(entry_price - initial_sl) * amount
    final_r = pnl_val / planned_risk_usdt if planned_risk_usdt > 0 else 0.0

    wallet_equity_at_entry = float(entry_meta.get("wallet_equity_at_entry", 0.0) or 0.0)
    reference_balance = float(state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0)))

    # حزام أمان: إذا كان الرصيد المسجل وقت الدخول صفراً لأي سبب، استخدم reference_balance
    if wallet_equity_at_entry <= 0:
        wallet_equity_at_entry = reference_balance

    wallet_pnl_pct = (pnl_val / wallet_equity_at_entry * 100) if wallet_equity_at_entry > 0 else 0.0
    pnl_ref_50 = (wallet_pnl_pct / 100.0) * reference_balance

    exit_reason = close_type
    if prior_reduction_pnl != 0:
        exit_reason = f"{close_type} + PRIOR_POSITION_REDUCTION"

    exit_details = {
        "exit_time": alert.get("exit_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exit_price": actual_exit,
        "pnl": pnl_val,
        "realized_pnl_gross": float(alert.get("realized_pnl_gross", pnl_val) or 0.0),
        "commission": float(alert.get("commission", 0.0) or 0.0),
        "entry_commission": float(alert.get("entry_commission", 0.0) or 0.0),
        "exit_commission": float(alert.get("exit_commission", 0.0) or 0.0),
        "funding": float(alert.get("funding", 0.0) or 0.0),
        "funding_status": str(alert.get("funding_status") or "UNKNOWN"),
        "pnl_complete": bool(alert.get("pnl_complete", False)),
        "closed_qty": float(alert.get("qty", amount) or 0.0),
        "close_order_id": alert.get("order_id"),
        "close_client_order_id": alert.get("client_order_id"),
        "position_side": alert.get("position_side") or entry_meta.get("position_side"),
        "pnl_pct": pnl_pct,
        "wallet_pnl_pct": wallet_pnl_pct,
        "reference_balance": reference_balance,
        "pnl_ref_50": pnl_ref_50,
        "exit_reason": exit_reason,
        "slippage": slippage_exit,
        "mfe_r": float(entry_meta.get("mfe_r") or 0.0),
        "mae_r": float(entry_meta.get("mae_r") or 0.0),
        "final_r": round(final_r, 6),
    }

    # ── تقريب قيم PnL إلى 4 أرقام عشرية (توحيد العرض في CSV/DB/Telegram) ──
    exit_details["pnl"]          = round(float(exit_details.get("pnl", 0) or 0), 4)
    exit_details["pnl_pct"]      = round(float(exit_details.get("pnl_pct", 0) or 0), 4)
    exit_details["wallet_pnl_pct"] = round(float(exit_details.get("wallet_pnl_pct", 0) or 0), 4)
    exit_details["pnl_ref_50"]   = round(float(exit_details.get("pnl_ref_50", 0) or 0), 4)

    if client is not None:
        enforce_daily_loss_limit(client, state_manager, notifier=notifier)

    # ── إلغاء أي أوامر حماية يتيمة بعد تأكيد إغلاق المركز ──
    if client is not None:
        position_side = str(
            alert.get("position_side")
            or entry_meta.get("position_side")
            or ("LONG" if str(entry_meta.get("side", "")).upper() in ("BUY", "LONG") else "SHORT")
        ).upper()
        if getattr(client, "_is_hedge_mode", False):
            client.cancel_position_protection_orders(symbol, position_side)
        else:
            client.cancel_all_orders(symbol)

    logged = log_testnet_trade(symbol, entry_meta, exit_details)
    if not logged:
        retry_now = time.time()
        retry_delay = _schedule_close_persistence_retry(entry_meta, retry_now)
        should_notify = _close_persistence_failure_notification_due(entry_meta, retry_now)
        if should_notify:
            entry_meta["close_persistence_last_notification_at"] = retry_now
        logger.error(
            f"❌ فشل التسجيل الدائم لإغلاق {symbol}. ستبقى metadata والصفقة النشطة "
            f"وسيُعاد التسجيل بعد {retry_delay:.0f} ثانية (المحاولة "
            f"{entry_meta['close_persistence_attempts']})."
        )
        entry_meta["_closing"] = False
        state_manager.save_entry_metadata(symbol, entry_meta)
        if should_notify and notifier is not None:
            notifier.send_message(
                f"⚠️ <b>تم رصد إغلاق صفقة لكن تعذر حفظها مؤقتاً</b>\n"
                f"الزوج: <code>{symbol}</code>\n"
                f"ستُعاد المحاولة تلقائياً بعد {retry_delay:.0f} ثانية؛ "
                "بيانات الإغلاق لم تُحذف."
            )
        return None

    decision_context = entry_meta.get("decision_context") or {}
    audit_decision_event(
        scan_id=decision_context.get("scan_id") or entry_meta.get("scan_id"),
        decision_id=decision_context.get("decision_id") or entry_meta.get("decision_id"),
        signal_id=decision_context.get("decision_id") or entry_meta.get("decision_id"),
        setup_id=entry_meta.get("setup_thesis_id"),
        thesis_id=entry_meta.get("setup_thesis_id"),
        exchange_order_id=entry_meta.get("exchange_order_id"),
        position_id=entry_meta.get("position_id") or entry_meta.get("trade_id"),
        symbol=symbol,
        stage="POSITION_CLOSED",
        status=exit_reason,
        trade_id=entry_meta.get("trade_id"),
        details={
            "exit_price": actual_exit,
            "pnl": exit_details["pnl"],
            "mfe_r": exit_details["mfe_r"],
            "mae_r": exit_details["mae_r"],
            "final_r": exit_details["final_r"],
            "planned_rr": entry_meta.get("planned_rr"),
            "market_regime": entry_meta.get("market_regime", "UNKNOWN"),
            "target_type": (entry_meta.get("target_plan") or {}).get("target_type"),
            "classification_source": alert.get("classification_source"),
        },
        durable=True,
    )

    proposal = entry_meta.get("user_visible_trade_proposal") or {}
    if proposal and getattr(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", False):
        try:
            shadow_store = _strategy_v2_shadow_store()
            collect_closed_outcome_evidence(
                storage=shadow_store,
                proposal_id=str(
                    proposal.get("proposal_id") or entry_meta.get("trade_id") or ""
                ),
                trade_id=str(entry_meta.get("trade_id") or ""),
                symbol=symbol,
                exit_details=exit_details,
                audit_ids={
                    "scan_id": decision_context.get("scan_id") or entry_meta.get("scan_id"),
                    "decision_id": decision_context.get("decision_id") or entry_meta.get("decision_id"),
                    "signal_id": decision_context.get("decision_id") or entry_meta.get("decision_id"),
                    "setup_id": entry_meta.get("setup_thesis_id"),
                    "thesis_id": entry_meta.get("setup_thesis_id"),
                    "position_id": entry_meta.get("position_id") or entry_meta.get("trade_id"),
                    "trade_id": entry_meta.get("trade_id"),
                    "symbol": symbol,
                },
            )
            thesis_id = entry_meta.get("setup_thesis_id")
            if thesis_id:
                shadow_store.record_closed_thesis(
                    symbol,
                    str(thesis_id),
                    side=str(entry_meta.get("side") or ""),
                    closed_at=str(
                        exit_details.get("exit_time")
                        or alert.get("closed_at")
                        or alert.get("timestamp")
                        or datetime.now(timezone.utc).isoformat()
                    ),
                    details={
                        "trade_id": entry_meta.get("trade_id"),
                        "pnl": pnl_val,
                        "close_type": close_type,
                    },
                )
        except Exception as shadow_error:
            audit_decision_event(
                symbol=symbol,
                trade_id=entry_meta.get("trade_id"),
                stage="STRATEGY_V2_SHADOW_OUTCOME",
                status="SHADOW_COLLECTION_ERROR_V1_UNCHANGED",
                details={"error_type": type(shadow_error).__name__},
                durable=True,
            )
    if proposal:
        _proposal_ledger_update(
            state_manager,
            proposal,
            "CLOSED_RECONCILED",
            entry_post_count=1,
            exit_time=exit_details.get("exit_time"),
            exit_price=exit_details.get("exit_price"),
            realized_pnl=exit_details.get("pnl"),
            realized_r=exit_details.get("final_r"),
            fees=exit_details.get("commission"),
            funding=exit_details.get("funding"),
            reconciliation="RECONCILED",
        )

    register_post_exit_observation(
        state_manager, symbol, entry_meta, exit_details, is_stop=is_sl
    )

    # نحذف metadata فور نجاح التسجيل الدائم. TradeJournal/AI لا يملكان حق
    # إبقاء الصفقة في حالة إغلاق معلقة؛ سيعملان في خيط منفصل بعد التنظيف.
    state_manager.pop_entry_metadata(symbol)

    try:
        tracker = PerformanceTracker(state_manager)
        wallet = tracker.get_wallet()
        logger.info(
            f"[SimWallet] الرصيد التراكمي بعد إغلاق {symbol}: "
            f"{wallet['wallet_balance']:.2f} USDT | "
            f"Realized: {wallet['realized_pnl']:+.2f} USDT"
        )
    except Exception as e:
        logger.error(f"[SimWallet] فشل تحديث المحفظة بعد الإغلاق: {e}")

    # --- تطبيق PairLocks (التبريد بعد الخسارة) ---
    is_profit = pnl_val > 0
    consecutive_losses = state_manager.record_trade_result(symbol, is_profit)

    if not is_profit:
        if consecutive_losses >= 3:
            lock_until = datetime.now().timestamp() + (24 * 3600)
            state_manager.lock_pair(symbol, lock_until, "خسارة 3 مرات متتالية")
            logger.warning(f"🔒 [PairLocks] تم قفل {symbol} لمدة 24 ساعة بسبب 3 خسائر متتالية.")
        elif consecutive_losses >= 2:
            lock_until = datetime.now().timestamp() + (4 * 3600)
            state_manager.lock_pair(symbol, lock_until, "خسارة مرتين متتاليتين")
            logger.warning(f"🔒 [PairLocks] تم قفل {symbol} لمدة 4 ساعات بسبب خسارتين متتاليتين.")

    # --- تحديث نتيجة الإشارة الـ forensics لربط التحليل ---
    trade_id = entry_meta.get("trade_id")
    if trade_id:
        try:
            from utils.forensics_logger import update_signal_outcome
            update_signal_outcome(
                trade_id,
                exit_reason,
                pnl_val,
                close_trigger=exit_reason,
                details={
                    "classification_source": alert.get("classification_source"),
                    "close_order_id": alert.get("order_id"),
                },
            )
        except Exception as e:
            logger.error(f"فشل تحديث نتيجة الصفقة {symbol} في التشريح: {e}")

    # SignalDedup يمنع تكرار الشمعة فقط. هذا القفل يمنع إعادة الفكرة الهيكلية
    # نفسها بعد SL أو إغلاق يدوي، مع السماح فوراً بـOB/BOS جديد ذي thesis_id جديد.
    thesis_id = entry_meta.get("setup_thesis_id")
    if thesis_id:
        if is_sl:
            cooldown_minutes = getattr(Config, "SETUP_COOLDOWN_STOP_MINUTES", 240)
            cooldown_reason = "STOP_LOSS"
        elif is_tp:
            cooldown_minutes = getattr(Config, "SETUP_COOLDOWN_TP_MINUTES", 30)
            cooldown_reason = "TAKE_PROFIT"
        else:
            cooldown_minutes = getattr(Config, "SETUP_COOLDOWN_MANUAL_MINUTES", 60)
            cooldown_reason = "MANUAL_OR_EXTERNAL_CLOSE"
        cooldown_saved = state_manager.set_setup_cooldown(
            symbol=symbol,
            thesis_id=thesis_id,
            duration_minutes=cooldown_minutes,
            reason=cooldown_reason,
            details={"trade_id": trade_id, "pnl": pnl_val, "close_type": close_type},
        )
        audit_decision_event(
            scan_id=(entry_meta.get("decision_context") or {}).get("scan_id"),
            decision_id=(entry_meta.get("decision_context") or {}).get("decision_id"),
            symbol=symbol,
            stage="SETUP_COOLDOWN",
            status="CREATED" if cooldown_saved else "PERSIST_FAILED",
            trade_id=trade_id,
            details={
                "setup_thesis_id": thesis_id,
                "duration_minutes": cooldown_minutes,
                "reason": cooldown_reason,
            },
            durable=True,
        )

    # تنظيف سجل منع التكرار للسماح بتحليل شمعة جديدة. قفل thesis أعلاه
    # يبقى هو المسؤول عن منع إعادة الإعداد نفسه.
    global signal_dedup
    if signal_dedup is not None:
        try:
            signal_dedup.clear_symbol(symbol)
        except Exception:
            pass

    # ── تنظيف state التنبيهات (AM-1 fix) ──────────────────────────
    # reset_alerts لم تكن تُستدعى سابقاً — alert state يتراكم إلى الأبد
    try:
        import builtins
        _am = getattr(builtins, "_global_alert_manager", None)
        if _am is not None:
            _am.reset_alerts(symbol, trade_id=entry_meta.get("trade_id"))
    except Exception:
        pass

    if client is not None:
        threading.Thread(
            target=_run_post_close_enrichment,
            args=(client, notifier, symbol, dict(entry_meta), dict(exit_details)),
            name=f"post-close-{symbol.replace('/', '-')}",
            daemon=True,
        ).start()

    return exit_details


def send_daily_performance_report(client, state_manager, notifier):
    """
    توليد وإرسال التقرير اليومي للمقارنة بين التداول التجريبي (Testnet) والاختبار العكسي (Backtest)
    """
    logger.info("📊 جاري توليد التقرير اليومي للأداء والمقارنة...")
    
    testnet_csv = "testnet_trades_log.csv"
    testnet_trades_count = 0
    testnet_win_rate = 0.0
    testnet_pf = 0.0
    testnet_max_dd = 0.0
    testnet_avg_slippage = 0.0
    testnet_net_return = 0.0
    
    if os.path.exists(testnet_csv):
        try:
            df_test = pd.read_csv(testnet_csv)
            if not df_test.empty:
                testnet_trades_count = len(df_test)
                pnls = pd.to_numeric(df_test['pnl'], errors='coerce').dropna().values
                winners = pnls[pnls > 0]
                losers = pnls[pnls <= 0]
                
                if testnet_trades_count > 0:
                    testnet_win_rate = len(winners) / testnet_trades_count * 100
                    
                sum_win = winners.sum() if len(winners) > 0 else 0
                sum_loss = abs(losers.sum()) if len(losers) > 0 else 0
                testnet_pf = sum_win / sum_loss if sum_loss > 0 else (float('inf') if sum_win > 0 else 0.0)
                
                equity = [100.0]
                pnl_pcts = pd.to_numeric(df_test.get('pnl_pct', df_test['pnl'] / 10.0), errors='coerce').fillna(0).values
                for p_pct in pnl_pcts:
                    equity.append(equity[-1] + p_pct)
                
                peak = equity[0]
                for val in equity:
                    if val > peak:
                        peak = val
                    dd = (peak - val) / peak * 100
                    if dd > testnet_max_dd:
                        testnet_max_dd = dd
                        
                slippages = pd.to_numeric(df_test['slippage'], errors='coerce').dropna().values
                if len(slippages) > 0:
                    testnet_avg_slippage = np.mean(slippages) * 100
                    
                testnet_net_return = sum(pnls)
        except Exception as e:
            logger.error(f"خطأ في معالجة سجل صفقات التجريبي: {e}")
            
    backtest_csv = "backtest_results.csv"
    bt_trades_count = 0
    bt_win_rate = 0.0
    bt_pf = 0.0
    bt_max_dd = 0.0
    bt_net_return = 0.0
    
    if os.path.exists(backtest_csv):
        try:
            df_bt = pd.read_csv(backtest_csv)
            if not df_bt.empty:
                bt_trades_count = len(df_bt)
                pnls_bt = pd.to_numeric(df_bt['pnl'], errors='coerce').dropna().values
                winners_bt = pnls_bt[pnls_bt > 0]
                losers_bt = pnls_bt[pnls_bt <= 0]
                
                if bt_trades_count > 0:
                    bt_win_rate = len(winners_bt) / bt_trades_count * 100
                    
                sum_win_bt = winners_bt.sum() if len(winners_bt) > 0 else 0
                sum_loss_bt = abs(losers_bt.sum()) if len(losers_bt) > 0 else 0
                bt_pf = sum_win_bt / sum_loss_bt if sum_loss_bt > 0 else (float('inf') if sum_win_bt > 0 else 0.0)
                
                equity_bt = [100.0]
                pnl_pcts_bt = pd.to_numeric(df_bt.get('pnl_pct', df_bt['pnl'] / 10.0), errors='coerce').fillna(0).values
                for p_pct in pnl_pcts_bt:
                    equity_bt.append(equity_bt[-1] + p_pct)
                
                peak_bt = equity_bt[0]
                for val in equity_bt:
                    if val > peak_bt:
                        peak_bt = val
                    dd = (peak_bt - val) / peak_bt * 100
                    if dd > bt_max_dd:
                        bt_max_dd = dd
                        
                bt_net_return = sum(pnls_bt)
        except Exception as e:
            logger.error(f"خطأ في معالجة سجل صفقات الباك تست: {e}")
            
    rejections = state_manager.get_rejection_counters()
    
    report = f"📊 <b>التقرير المقارن اليومي (Testnet vs Backtest)</b> 📊\n\n"
    report += f"🗓️ <b>التاريخ:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    report += f"〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
    
    report += f"🟢 <b>أداء التداول التجريبي الحقيقي (Live Testnet):</b>\n"
    report += f"• عدد الصفقات: <code>{testnet_trades_count}</code>\n"
    report += f"• نسبة الفوز: <code>{testnet_win_rate:.2f}%</code>\n"
    report += f"• معامل الربح (PF): <code>{testnet_pf:.2f}</code>\n"
    report += f"• أقصى تراجع (Max DD): <code>{testnet_max_dd:.2f}%</code>\n"
    report += f"• متوسط الانزلاق الفعلي: <code>{testnet_avg_slippage:.3f}%</code>\n"
    report += f"• صافي الأرباح/الخسائر: <code>{testnet_net_return:+.2f}</code> USDT\n\n"
    
    report += f"🔵 <b>المرجع التاريخي المقارن (Backtest 365d):</b>\n"
    report += f"• عدد الصفقات الكلي: <code>{bt_trades_count}</code>\n"
    report += f"• نسبة الفوز: <code>{bt_win_rate:.2f}%</code>\n"
    report += f"• معامل الربح (PF): <code>{bt_pf:.2f}</code>\n"
    report += f"• أقصى تراجع (Max DD): <code>{bt_max_dd:.2f}%</code>\n"
    report += f"• صافي العائد الكلي: <code>{bt_net_return:+.2f}</code> USDT\n\n"
    
    report += f"🚫 <b>إحصائيات الصفقات المرفوضة بالفلاتر:</b>\n"
    if rejections:
        for filter_name, count in rejections.items():
            report += f"• فلتر <code>{filter_name}</code>: تم إلغاء <code>{count}</code> صفقات\n"
    else:
        report += "• لا يوجد صفقات مرفوضة اليوم.\n"
        
    report += f"\n💡 <i>ملاحظة: البوت يحتاج إلى 7 إلى 14 يوماً من الأداء التجريبي المستقر للتحقق من الموثوقية الكاملة.</i>"
    
    notifier.send_message(report)
    state_manager.reset_rejection_counters()
    try:
        from utils.forensics_logger import cleanup_old_forensics
        cleanup_old_forensics(keep_days=365)
    except Exception as e:
        logger.error(f"فشل تنظيف سجلات التشريح اليومية القديمة: {e}")
    
    state_manager.set("last_daily_report_date", datetime.now().strftime("%Y-%m-%d"))


def reconcile_positions(client, state_manager):
    """
    التأكد من أن الصفقات المسجلة في الذاكرة تتطابق مع باينانس.
    لا تُحذف الصفقة المحلية المغلقة هنا؛ تبقى مسجلة حتى يلتقط مراقب
    الإغلاقات الـ fill الفعلي ويسجل النتيجة ثم ينظف الحالة.
    """
    log_memory_milestone("POSITION_RECONCILIATION_START")
    try:
        logger.info("🔄 جاري مزامنة الصفقات المفتوحة مع منصة باينانس...")
        positions = client.exchange.fetch_positions()
        # توحيد اسم العملة ليطابق النظام (بدون :USDT)
        exchange_positions = {}
        for position in positions or []:
            info = position.get('info', {}) or {}
            signed_amount = float(info.get('positionAmt', 0) or 0)
            if signed_amount == 0:
                continue
            clean_symbol = position.get('symbol', '').split(':')[0]
            exchange_positions.setdefault(clean_symbol, []).append(position)

        exchange_active = set(exchange_positions)

        # نموذج الحالة الحالي يملك leg واحداً فقط لكل symbol. لذلك وجود LONG
        # وSHORT معاً تعارض يجب اكتشافه دائماً، حتى إذا كانت metadata الخاصة
        # بجهة البوت موجودة مسبقاً ثم أضيفت الجهة المقابلة يدوياً.
        hedge_conflicts = set()
        for sym, rows in exchange_positions.items():
            observed_sides = set()
            total_amount = 0.0
            for row in rows:
                info = row.get("info", {}) or {}
                signed_amount = float(info.get("positionAmt", 0) or 0)
                if signed_amount == 0:
                    continue
                raw_side = str(info.get("positionSide") or "BOTH").upper()
                if raw_side == "BOTH":
                    raw_side = "LONG" if signed_amount > 0 else "SHORT"
                observed_sides.add(raw_side)
                total_amount += abs(signed_amount)
            if {"LONG", "SHORT"}.issubset(observed_sides):
                hedge_conflicts.add(sym)
                existing_meta = state_manager.get_entry_metadata(sym) or {}
                blocked_meta = {
                    **existing_meta,
                    "side": existing_meta.get("side") or "AMBIGUOUS",
                    "position_side": existing_meta.get("position_side") or "AMBIGUOUS",
                    "entry_time": existing_meta.get("entry_time")
                    or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": float(existing_meta.get("entry_price") or 0.0),
                    "amount": total_amount,
                    "original_amount": float(
                        existing_meta.get("original_amount") or total_amount
                    ),
                    "sl": float(existing_meta.get("sl") or 0.0),
                    "tp": float(existing_meta.get("tp") or 0.0),
                    "entry_reason": existing_meta.get("entry_reason")
                    or "EXTERNAL_HEDGE_CONFLICT",
                    "strategy_source": existing_meta.get("strategy_source")
                    or "EXTERNAL/MANUAL",
                    "management_blocked": True,
                    "management_blocked_reason": "EXTERNAL_HEDGE_CONFLICT",
                    "observed_position_sides": sorted(observed_sides),
                    "last_conflict_at": datetime.now(timezone.utc).isoformat(),
                }
                state_manager.save_entry_metadata(sym, blocked_meta)
                logger.critical(
                    f"[{sym}] رُصد LONG وSHORT معاً ({sorted(observed_sides)}). "
                    "أُوقفت كل الإدارة الآلية لهذا الرمز حتى إزالة التعارض."
                )

        # إذا زال تعارض سابق عن مركز يملكه البوت، يمكن إعادة الإدارة فقط عندما
        # تطابق الجهة الوحيدة المتبقية الجهة الأصلية. المراكز الخارجية تبقى
        # unmanaged ولا تُتبنى تلقائياً.
        for sym in exchange_active - hedge_conflicts:
            existing_meta = state_manager.get_entry_metadata(sym) or {}
            if existing_meta.get("management_blocked_reason") != "EXTERNAL_HEDGE_CONFLICT":
                continue
            rows = exchange_positions.get(sym, [])
            if len(rows) != 1 or existing_meta.get("external_position"):
                continue
            info = rows[0].get("info", {}) or {}
            signed_amount = float(info.get("positionAmt", 0) or 0)
            remaining_side = str(info.get("positionSide") or "BOTH").upper()
            if remaining_side == "BOTH":
                remaining_side = "LONG" if signed_amount > 0 else "SHORT"
            original_side = str(existing_meta.get("position_side") or "").upper()
            if original_side == remaining_side:
                existing_meta.pop("management_blocked", None)
                existing_meta.pop("management_blocked_reason", None)
                existing_meta.pop("observed_position_sides", None)
                state_manager.save_entry_metadata(sym, existing_meta)
                logger.warning(
                    f"[{sym}] زال تعارض Hedge وبقيت جهة البوت {remaining_side}; "
                    "أُعيدت الإدارة الآلية."
                )
        
        local_symbols = state_manager.get_active_symbols()  # set
        
        # إيجاد الصفقات الموجودة في الذاكرة لكنها غير مفتوحة على باينانس دون تعديل ال_set أثناء التكرار
        stale = {sym for sym in local_symbols if sym not in exchange_active}
        if stale:
            logger.warning(
                f"⚠️ صفقات لم تعد مفتوحة على باينانس وتحتاج تسجيل إغلاق قبل التنظيف: {stale}"
            )
            
        # إيجاد الصفقات المفتوحة في باينانس لكنها غير مسجلة في ذاكرة البوت (مثلاً فتحت يدوياً أو بسبب انقطاع)
        missing = {sym for sym in exchange_active if sym not in local_symbols}
        if missing:
            logger.info(f"➕ صفقات مفتوحة على باينانس غير مسجلة في البوت — سيتم إضافتها: {missing}")
            local_symbols = local_symbols.union(missing)

        metadata_missing = {
            sym for sym in exchange_active
            if sym not in hedge_conflicts and not state_manager.get_entry_metadata(sym)
        }
        if metadata_missing:
            # نحفظ بيانات موثوقة دنيا لأي مركز حي بلا metadata، حتى لو كان الرمز
            # موجوداً مسبقاً في active_symbols من restart غير مكتمل.
            # لا نضع حماية تلقائياً لمركز يدوي لأن ذلك يغيره دون تفويض.
            for sym in metadata_missing:
                rows = exchange_positions.get(sym, [])
                if not rows:
                    continue
                first = rows[0]
                info = first.get('info', {}) or {}
                signed_amount = float(info.get('positionAmt', 0) or 0)
                raw_position_side = str(info.get('positionSide') or 'BOTH').upper()
                if raw_position_side == 'BOTH':
                    raw_position_side = 'LONG' if signed_amount > 0 else 'SHORT'
                state_manager.save_entry_metadata(sym, {
                    "side": "BUY" if signed_amount > 0 else "SELL",
                    "position_side": raw_position_side,
                    "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": float(
                        first.get('entryPrice')
                        or info.get('entryPrice', 0)
                        or 0
                    ),
                    "amount": abs(signed_amount),
                    "original_amount": abs(signed_amount),
                    "sl": 0.0,
                    "tp": 0.0,
                    "entry_reason": "EXTERNAL_POSITION_DISCOVERED",
                    "strategy_source": "EXTERNAL/MANUAL",
                    "external_position": True,
                })
            
        if missing:
            state_manager.save_active_symbols(local_symbols)

        result = {
            "status": "OK",
            "exchange_active": exchange_active,
            "pending_close": stale,
            "missing": missing,
            "metadata_recovered": metadata_missing,
            "management_blocked": hedge_conflicts,
        }
        log_memory_milestone(
            "POSITION_RECONCILIATION_END",
            details={"positions": len(exchange_active)},
        )
        return result
            
    except Exception as e:
        logger.error(f"❌ فشل مزامنة الصفقات: {e}")
        log_memory_milestone("POSITION_RECONCILIATION_FAILED", details={"error": type(e).__name__})
        return {"status": "UNKNOWN", "error": str(e)}


def verify_recovered_position_protection(client, state_manager, live_symbols):
    """يتحقق على Binance من حماية المراكز التي يثبت state أن البوت أنشأها."""
    verified = []
    external = []
    errors = []
    for symbol in sorted(set(live_symbols or [])):
        meta = state_manager.get_entry_metadata(symbol) or {}
        if meta.get("external_position") or meta.get("management_blocked"):
            external.append(symbol)
            continue
        side = str(meta.get("side") or "").lower()
        amount = float(meta.get("amount") or meta.get("original_amount") or 0.0)
        sl = float(meta.get("current_sl") or meta.get("sl") or 0.0)
        tp = float(meta.get("tp") or 0.0)
        if side not in ("buy", "sell") or amount <= 0 or sl <= 0 or tp <= 0:
            errors.append({"symbol": symbol, "reason": "INCOMPLETE_LOCAL_PROTECTION_PLAN"})
            continue
        protection = client.ensure_position_protection(
            symbol,
            side,
            amount,
            sl,
            tp,
            client_order_prefix=meta.get("trade_id") or meta.get("position_id"),
        )
        if (
            protection.get("status") == "UNKNOWN"
            or not protection.get("sl_ok")
            or not protection.get("tp_ok")
        ):
            errors.append({
                "symbol": symbol,
                "reason": "PROTECTION_NOT_VERIFIED",
                "status": protection.get("status"),
                "sl_ok": bool(protection.get("sl_ok")),
                "tp_ok": bool(protection.get("tp_ok")),
            })
            continue
        meta.update({
            "protection_status": "PROTECTED",
            "protection_verified_at": datetime.now(timezone.utc).isoformat(),
            **{
                key: protection.get(key)
                for key in (
                    "sl_order_id", "sl_client_id", "sl_actual_order_id",
                    "tp_order_id", "tp_client_id", "tp_actual_order_id",
                )
                if protection.get(key)
            },
        })
        if not state_manager.save_entry_metadata(symbol, meta):
            errors.append({"symbol": symbol, "reason": "PROTECTION_STATE_PERSIST_FAILED"})
            continue
        verified.append(symbol)
    return {
        # المركز الخارجي لا نعدله تلقائياً، لكنه يمنع صفقات جديدة حتى يغلقه
        # المستخدم أو يعتمد له سياسة إدارة صريحة. تجاهله مع احتساب max trades
        # لا يضمن أن الحساب كله محمي.
        "status": "OK" if not errors and not external else "BLOCKED",
        "verified": verified,
        "external_unmanaged": external,
        "errors": errors,
    }


def restore_recovered_trailing_tracking(
    client, state_manager, trailing_manager, live_symbols
):
    """إعادة بناء Trailing للمراكز المدارة بعد restart بصورة idempotent."""
    restored = []
    errors = []
    for symbol in sorted(set(live_symbols or [])):
        meta = state_manager.get_entry_metadata(symbol) or {}
        if meta.get("external_position") or meta.get("management_blocked"):
            continue
        if trailing_manager.has_trade(symbol):
            continue

        side = str(meta.get("side") or "").lower()
        entry = float(meta.get("entry_price") or 0.0)
        initial_sl = float(meta.get("initial_sl") or meta.get("sl") or 0.0)
        current_sl = float(meta.get("current_sl") or meta.get("sl") or 0.0)
        amount = float(meta.get("amount") or meta.get("original_amount") or 0.0)
        if side not in ("buy", "sell") or min(entry, initial_sl, current_sl, amount) <= 0:
            errors.append({"symbol": symbol, "reason": "INCOMPLETE_TRAILING_STATE"})
            continue

        current_price = client.get_current_price(symbol)
        if not current_price or float(current_price) <= 0:
            errors.append({"symbol": symbol, "reason": "CURRENT_PRICE_UNAVAILABLE"})
            continue
        if not trailing_manager.restore_trade(
            symbol=symbol,
            side=side,
            entry_price=entry,
            initial_sl=initial_sl,
            current_sl=current_sl,
            amount=amount,
            current_price=float(current_price),
        ):
            errors.append({"symbol": symbol, "reason": "TRAILING_PERSIST_FAILED"})
            continue
        restored.append(symbol)
    return {
        "status": "OK" if not errors else "BLOCKED",
        "restored": restored,
        "errors": errors,
    }

def check_network_connection(client) -> bool:
    """
    مراقبة حالة الشبكة (Hummingbot Network Check).
    يتحقق من الاتصال بباينانس، وإذا فشل، يعود بـ False.
    """
    try:
        client.exchange.fetch_time()
        return True
    except Exception as e:
        logger.warning(f"⚠️ [Network Monitor] فشل التحقق من الاتصال بشبكة Binance: {e}")
        return False

def check_stoploss_guard(state_manager, notifier) -> bool:
    """يوقف فتح صفقات جديدة إذا تكررت خسائر SL في فترة قصيرة"""
    if not state_manager.get_sl_guard_enabled():
        return False
    pause_until = state_manager.get_sl_guard_pause()
    if pause_until and datetime.now() < pause_until:
        logger.info(f"[StoplossGuard] البوت موقوف حتى {pause_until.strftime('%H:%M')}")
        return True
    try:
        from utils.db_manager import get_recent_trades
        closed_trades = get_recent_trades(limit=50)
    except Exception:
        closed_trades = []
    limit = state_manager.get_sl_guard_limit()
    lookback = datetime.now() - timedelta(minutes=Config.SL_GUARD_LOOKBACK_MINUTES)
    recent_sl = [
        t for t in closed_trades
        if "stop" in str(t.get("exit_reason", "")).lower()
        and _parse_trade_time(t.get("exit_time")) >= lookback
    ]
    if len(recent_sl) >= limit:
        pause_until = datetime.now() + timedelta(minutes=Config.SL_GUARD_PAUSE_MINUTES)
        state_manager.set_sl_guard_pause(pause_until)
        notifier.send_message(
            f"🛑 <b>StoplossGuard مفعّل</b>\n"
            f"تم رصد <code>{len(recent_sl)}</code> خسائر SL خلال آخر {Config.SL_GUARD_LOOKBACK_MINUTES} دقيقة.\n"
            f"⏸️ البوت متوقف عن فتح صفقات حتى: <code>{pause_until.strftime('%H:%M')}</code>"
        )
        return True
    return False


def check_max_drawdown(state_manager, notifier, current_balance: float) -> bool:
    """حارس خسارة محققة متحرك؛ ليس مقياس Peak-to-Trough drawdown."""
    if not state_manager.get_max_drawdown_enabled():
        return False
    pause_until = state_manager.get_max_drawdown_pause()
    if pause_until and datetime.now() < pause_until:
        logger.info(f"[RollingRealizedLoss] البوت موقوف حتى {pause_until.strftime('%H:%M')}")
        return True
    try:
        from utils.db_manager import get_recent_trades
        closed_trades = get_recent_trades(limit=100)
    except Exception:
        closed_trades = []
    max_pct = state_manager.get_max_drawdown_pct()
    lookback = datetime.now() - timedelta(hours=Config.MAX_DRAWDOWN_LOOKBACK_HOURS)
    recent = [t for t in closed_trades if _parse_trade_time(t.get("exit_time")) >= lookback]
    if len(closed_trades) >= 100 and len(recent) >= 100:
        logger.critical(
            "[RollingRealizedLoss] كل الصفوف الـ100 المسترجعة تقع داخل نافذة الحارس؛ "
            "اكتمال السجل غير مضمون، لذلك مُنع الدخول احترازياً."
        )
        return True
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in recent)
    if total_pnl < 0 and current_balance > 0:
        rolling_loss_pct = (abs(total_pnl) / current_balance) * 100
        if rolling_loss_pct >= max_pct:
            pause_until = datetime.now() + timedelta(hours=Config.MAX_DRAWDOWN_PAUSE_HOURS)
            state_manager.set_max_drawdown_pause(pause_until)
            notifier.send_message(
                f"🔴 <b>حارس الخسارة المحققة المتحركة مفعّل</b>\n"
                f"خسارة آخر {Config.MAX_DRAWDOWN_LOOKBACK_HOURS} ساعة: "
                f"<code>{rolling_loss_pct:.1f}%</code> (الحد: {max_pct}%)\n"
                f"⏸️ متوقف حتى: <code>{pause_until.strftime('%H:%M')}</code>"
            )
            return True
    return False


def check_low_profit_pair(state_manager, notifier, symbol: str) -> bool:
    """يجمّد عملة بعد عدد من الخسائر المتتالية"""
    normalized_sym = symbol.split(":")[0]
    if not state_manager.get_low_profit_enabled():
        return False
    if state_manager.is_pair_frozen(normalized_sym):
        logger.info(f"[LowProfitPairs] {normalized_sym} مجمّدة حالياً.")
        return True
    count = state_manager.get_low_profit_loss_count()
    consecutive_losses = state_manager.get("consecutive_losses", {}).get(normalized_sym, 0)

    if consecutive_losses >= count:
        freeze_hours = float(getattr(Config, "LOW_PROFIT_FREEZE_HOURS", 2))
        state_manager.freeze_pair(normalized_sym, freeze_hours)
        notifier.send_message(
            f"❄️ <b>تجميد {normalized_sym}</b>\n"
            f"تم رصد <code>{consecutive_losses}</code> خسائر متتالية.\n"
            f"مجمّدة لمدة <code>{freeze_hours:.0f}</code> ساعات."
        )
        return True
    return False


def _parse_trade_time(time_str) -> datetime:
    """تحويل نص التوقيت إلى datetime بشكل آمن"""
    try:
        return datetime.strptime(str(time_str), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.min


def classify_scan_result_event(raw_status):
    """تصنيف نتيجة الجولة دون تحويل حالات التخطي إلى نتائج تداول طرفية."""
    raw_status = str(raw_status or "UNKNOWN")
    upper_status = raw_status.upper()
    if "ACTIVE" in upper_status:
        return "SCAN_SKIP", "ACTIVE_POSITION"
    if "PENDING" in upper_status and "V-LIMIT" not in upper_status:
        return "SCAN_SKIP", "PENDING_ORDER"
    if "BUY" in upper_status or "SELL" in upper_status:
        return "FINAL_OUTCOME", "EXECUTED"
    if "V-LIMIT" in upper_status:
        return "FINAL_OUTCOME", "PENDING"
    if "REJECT" in upper_status or "FAILED" in upper_status:
        return "FINAL_OUTCOME", "REJECTED"
    if "HOLD" in upper_status:
        return "FINAL_OUTCOME", "HOLD"
    return "FINAL_OUTCOME", (upper_status.strip() or "UNKNOWN")


def build_entry_ta_context(ta_details: dict) -> dict:
    """Small JSON-safe 15m setup snapshot shared by Scanner and Sniper."""
    details = ta_details or {}

    def _float(value):
        try:
            value = float(value)
            return value if math.isfinite(value) else 0.0
        except (TypeError, ValueError):
            return 0.0

    anchor = details.get("setup_anchor") or {}
    safe_anchor = {
        "type": str(anchor.get("type") or ""),
        "created_at": str(anchor.get("created_at") or ""),
        "age": int(_float(anchor.get("age"))),
        "low": _float(anchor.get("low")),
        "high": _float(anchor.get("high")),
    } if anchor else None
    return {
        "score": int(_float(details.get("score"))),
        "htf_trend": str(details.get("htf_trend") or "unknown"),
        "ema_50": _float(details.get("ema_50")),
        "ema_200": _float(details.get("ema_200")),
        "adx": _float(details.get("adx")),
        "rsi": _float(details.get("rsi")),
        "volume": _float(details.get("volume")),
        "volume_avg_20": _float(details.get("volume_avg_20")),
        "volume_median_20": _float(details.get("volume_median_20")),
        "rvol_20": _float(details.get("rvol_20")),
        "optimal_entry": _float(details.get("optimal_entry")),
        "invalidation_level": _float(details.get("invalidation_level")),
        "ob_low": _float(details.get("ob_low")),
        "ob_high": _float(details.get("ob_high")),
        "countertrend": bool(details.get("countertrend", False)),
        "market_regime": details.get("market_regime") or {
            "label": "UNKNOWN", "known": False
        },
        "recent_sell_side_sweep": bool(details.get("recent_sell_side_sweep", False)),
        "recent_buy_side_sweep": bool(details.get("recent_buy_side_sweep", False)),
        "bos_created_at": str(details.get("bos_created_at") or ""),
        "choch_created_at": str(details.get("choch_created_at") or ""),
        "liquidity_sweep_at": str(details.get("liquidity_sweep_at") or ""),
        "entry_zone_created_at": str(details.get("entry_zone_created_at") or ""),
        "htf_context_changed_at": str(details.get("htf_context_changed_at") or ""),
        "setup_anchor": safe_anchor,
    }


def _thesis_reset_evidence(ta_context: dict) -> list[dict]:
    """Expose only timestamps already carried by V1; never invent evidence."""
    context = ta_context or {}
    evidence = []
    anchor = context.get("setup_anchor") or {}
    if anchor.get("created_at"):
        evidence.append({
            "event": "NEW_ORDER_BLOCK",
            "timestamp": anchor["created_at"],
            "source": "setup_anchor.created_at",
        })
    for field, event in (
        ("bos_created_at", "NEW_BOS"),
        ("choch_created_at", "NEW_CHOCH"),
        ("liquidity_sweep_at", "NEW_LIQUIDITY_SWEEP"),
        ("entry_zone_created_at", "NEW_ENTRY_ZONE"),
        ("htf_context_changed_at", "MATERIAL_HTF_CONTEXT_CHANGE"),
    ):
        if context.get(field):
            evidence.append({
                "event": event,
                "timestamp": context[field],
                "source": field,
            })
    return evidence


def run_bot_iteration(client, hybrid_strategy, risk_manager, trailing_manager, state_manager, notifier, news_engine, ai_engine, ta_engine, social_engine, universe_filter, derivatives_filter, active_strategies, last_scan_time=None):

    if not getattr(Config, "ACCOUNT_MANAGEMENT_ENABLED", True):
        # A disabled secondary deployment must be genuinely passive.  It may
        # expose health/read-only endpoints, but it must not reconcile, scan,
        # open, close, trail, or cancel anything on the shared Demo account.
        return last_scan_time or 0.0

    scan_id = str(uuid.uuid4())
    scan_started_monotonic = time.monotonic()
    audit_decision_event(
        scan_id=scan_id,
        stage="SCAN_ROUND",
        status="STARTED",
        details={"last_scan_time": last_scan_time, "phase": "PRE_FLIGHT"},
    )

    def _finish_scan(status, reason=None, **details):
        audit_decision_event(
            scan_id=scan_id,
            stage="SCAN_ROUND",
            status=status,
            details={
                "reason": reason,
                "duration_seconds": round(time.monotonic() - scan_started_monotonic, 3),
                **details,
            },
        )

    # مزامنة الصفقات للتأكد من أن الذاكرة متوافقة مع منصة باينانس (لتنظيف الصفقات المغلقة أو غير المعروفة)
    reconcile_result = reconcile_positions(client, state_manager)
    if not reconcile_result or reconcile_result.get("status") != "OK":
        state_manager.set("recovery_complete", False)
        logger.critical(
            "[Safety] تعذر التحقق من المراكز على باينانس — تم إلغاء فتح صفقات جديدة لهذه الدورة."
        )
        _finish_scan("ABORTED_SAFE", "POSITION_RECONCILIATION_UNKNOWN")
        return last_scan_time or 0.0
    protection_recovery = verify_recovered_position_protection(
        client, state_manager, reconcile_result.get("exchange_active", set())
    )

    external_symbols = sorted(reconcile_result.get("missing") or [])
    if external_symbols:
        symbols_text = "، ".join(f"<code>{symbol}</code>" for symbol in external_symbols)
        try:
            notifier.send_message(
                "⚠️ <b>تم اكتشاف مركز خارجي أو غير متتبّع</b>\n"
                f"الأزواج: {symbols_text}\n"
                "لن يعدّل البوت هذا المركز تلقائياً، وتم إيقاف فتح صفقات جديدة "
                "حتى تغلق المركز أو تعتمد له سياسة إدارة صريحة."
            )
        except Exception as exc:
            logger.warning(f"تعذر إرسال تنبيه المركز الخارجي: {exc}")
        for symbol in external_symbols:
            audit_decision_event(
                decision_id=f"external:{symbol.replace('/', '')}",
                symbol=symbol,
                stage="POSITION_RECONCILIATION",
                status="UNTRACKED_POSITION_DISCOVERED_TRADING_PAUSED",
                details={"managed": False, "protection_added": False},
                durable=True,
            )

    if protection_recovery.get("status") != "OK":
        state_manager.set("recovery_complete", False)
        logger.critical(
            "[Safety] حماية مركز مسترد غير مؤكدة؛ لا صفقات جديدة: "
            f"errors={protection_recovery.get('errors')} "
            f"external={protection_recovery.get('external_unmanaged')}"
        )
        _finish_scan("ABORTED_SAFE", "RECOVERED_PROTECTION_UNVERIFIED")
        return last_scan_time or 0.0
    state_manager.set("recovery_complete", True)
    state_manager.set("last_successful_reconciliation_at", datetime.now(timezone.utc).isoformat())

    risk_check = enforce_daily_loss_limit(client, state_manager, notifier)
    if risk_check.get("status") != "OK":
        _finish_scan("ABORTED_SAFE", f"RISK_GUARD:{risk_check.get('status')}")
        return last_scan_time or 0.0
    
    active_symbols = state_manager.get_active_symbols()
    normalize_reference_balance_in_state(state_manager)

    # ── فحص Kill Switch — إذا نشط نتجاهل المسح ونكمل مراقبة المفتوحة ──
    if state_manager.get("kill_switch_active", False):
        logger.warning("[KillSwitch] 🛑 نشط — لا صفقات جديدة. الصفقات المفتوحة تُراقب بشكل طبيعي.")
        _finish_scan("ABORTED_SAFE", "KILL_SWITCH_ACTIVE")
        return 0.0

    logger.info("=========================================")
    logger.info("بدء جولة مسح السوق (Market Screener)...")
    logger.info("=========================================")
    
    # [مراقب الصفقات المغلقة]
    if last_scan_time:
        logger.info("جاري التحقق من الصفقات المغلقة (SL/TP) خلال الاستراحة...")
        # Reconciliation retains tracked closed positions until their fills
        # are recorded. Market ranking is unrelated to account recovery and
        # otherwise adds fill/funding requests for 15 unowned symbols.
        check_symbols = set(active_symbols)
        
        for sym in check_symbols:
            sym_meta = state_manager.get_entry_metadata(sym.split(":")[0])
            alerts = client.get_recent_sl_tp_fills(
                sym,
                last_scan_time,
                entry_meta=sym_meta,
            )
            for alert in alerts:
                sym_clean = alert["symbol"].split(":")[0]
                meta = state_manager.get_entry_metadata(sym_clean)

                # ── فحص إلزامي: هل المركز لا يزال مفتوحاً فعلياً على البورصة؟ ──
                # هذا يمنع حذف الرمز خطأ من active_symbols في حالات:
                # - partial TP (ربح جزئي بدون إغلاق كامل)
                # - أي تنفيذ بـ pnl != 0 لا يعني إغلاق المركز بالكامل
                real_amt_after = None
                try:
                    real_amt_after = client.get_position_amount(sym_clean)
                except Exception as e:
                    logger.warning(f"⚠️ [SafeClose] فشل فحص الكمية الفعلية لـ {sym_clean}: {e}")

                if real_amt_after is not None and real_amt_after > 0:
                    # المركز لا يزال مفتوحاً. البوت لا ينفذ إغلاقاً جزئياً، لكن قد
                    # يكون المستخدم خفّضه يدوياً؛ نسجل fill مرة واحدة ونبقي التتبع.
                    logger.info(
                        f"[PositionReduced] تم رصد تنفيذ إغلاق بينما {sym_clean} ما زال مفتوحاً، "
                        f"لكن الصفقة ما زالت مفتوحة (الكمية={real_amt_after}). لا يُحذف من active_symbols."
                    )

                    fill_id = str(alert.get('order_id') or alert.get('client_order_id') or '')
                    seen_ids = set(str(v) for v in meta.get('seen_reduction_order_ids', []))
                    if not fill_id or fill_id not in seen_ids:
                        partial_pnl = float(alert.get("pnl", 0.0))
                        previous_reduction = float(
                            meta.get("prior_reduction_realized_pnl")
                            or meta.get("partial_realized_pnl")
                            or 0.0
                        )
                        meta["prior_reduction_realized_pnl"] = previous_reduction + partial_pnl
                        if fill_id:
                            seen_ids.add(fill_id)
                            meta['seen_reduction_order_ids'] = sorted(seen_ids)
                    meta['amount'] = float(real_amt_after)
                    meta["position_reduced_externally"] = True

                    state_manager.save_entry_metadata(sym_clean, meta)

                    continue

                # ── المركز مغلق فعلاً فقط بعد نجاح التحقق من Binance ──
                if real_amt_after is None:
                    logger.warning(
                        f"⚠️ [SafeClose] حالة {sym_clean} غير معروفة بسبب فشل API — "
                        "لن نسجل إغلاقاً ولن نحذف الحالة حتى ينجح التحقق."
                    )
                    continue

                pnl_pct = 0.0
                exit_details = None
                try:
                    exit_details = handle_trade_close(
                        alert, state_manager, client=client, notifier=notifier
                    )
                    if exit_details == CLOSE_ALREADY_PROCESSING:
                        continue
                    if exit_details:
                        pnl_pct = exit_details.get("pnl_pct", 0.0)
                except Exception as e:
                    logger.error(f"خطأ أثناء تسجيل إغلاق الصفقة: {e}")
                    
                if exit_details:
                    notifier.send_pnl_alert(
                        alert['symbol'],
                        alert['type'],
                        exit_details.get('exit_price', alert.get('price', 0.0)),
                        exit_details.get('pnl', alert.get('pnl', 0.0)),
                        exit_details.get('pnl_pct', 0.0),
                        wallet_pnl_pct=exit_details.get("wallet_pnl_pct", 0.0),
                        pnl_ref_50=exit_details.get("pnl_ref_50", 0.0),
                        reference_balance=exit_details.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0)),
                    )
                else:
                    notifier.send_message(
                        f"⚠️ <b>تم رصد إغلاق صفقة لكن فشل تسجيلها</b>\n"
                        f"الزوج: <code>{alert.get('symbol')}</code>"
                    )
                logger.info(f"إشعار إغلاق صفقة: {alert['symbol']} | {alert['type']} | PnL: {alert['pnl']} | Pct: {pnl_pct}%")
                if exit_details:
                    if sym_clean in active_symbols:
                        active_symbols.discard(sym_clean)
                        state_manager.remove_active_symbol(sym_clean)
                    if trailing_manager.has_trade(sym_clean):
                        trailing_manager.unregister_trade(sym_clean)

    # نستخدم نفس لقطة Binance التي اجتازت فحص المخاطر؛ لا يوجد رصيد تخميني
    # يمكنه السماح بصفقة عند فشل API أو نقص الرصيد الفعلي.
    usdt_total = float(risk_check["equity"])
    usdt_free = float(risk_check.get("available", 0.0) or 0.0)

    logger.info(
        f"الرصيد المتاح: {usdt_free:.2f} USDT | "
        f"الإجمالي Equity: {usdt_total:.2f} USDT"
    )

    # الرصيد الحر صفر يعني عدم فتح صفقات جديدة، حتى لو كان Equity موجباً.
    if usdt_free <= 0:
        logger.error("لا يوجد رصيد حر فعلي كافٍ لفتح صفقة جديدة.")
        _finish_scan("ABORTED_SAFE", "NO_AVAILABLE_BALANCE")
        return usdt_free

    # التحقق من الحد الأقصى للصفقات المفتوحة

    max_trades = state_manager.get_max_open_trades()
    if len(active_symbols) >= max_trades:
        logger.warning(f"وصلنا للحد الأقصى للصفقات المفتوحة ({max_trades}). سنتوقف عن البحث حتى تُغلق صفقة.")
        _finish_scan(
            "ABORTED_SAFE", "MAX_OPEN_TRADES",
            active_positions=len(active_symbols), max_open_trades=max_trades,
        )
        return usdt_total

    # 2. مسح السوق - استخدام فلتر أمان الكتالوج (Universe Filter) لجلب أفضل العملات المشتعلة المؤهلة
    target_symbols = universe_filter.get_scanning_targets(
        limit=getattr(Config, 'MARKET_SCAN_LIMIT', 50),
        activity_snapshot=get_latest_hot_list_snapshot(),
    )
    audit_decision_event(
        scan_id=scan_id, stage="SCANNER_UNIVERSE", status="SELECTED",
        details=universe_filter.last_selection_details,
    )
    logger.info(f"العملات التي سيتم مراقبتها الآن: {', '.join(target_symbols)}")
    subscription_change = _candle_stream.set_symbols(subscription_symbols(
        target_symbols, active_symbols, state_manager.get_virtual_orders(),
    ))
    logger.info(
        f"[SharedCandle] اشتراك الجولة: total={subscription_change['total']} "
        f"added={len(subscription_change['added'])} removed={len(subscription_change['removed'])}"
    )
    log_memory_milestone(
        "AFTER_SYMBOL_UNIVERSE",
        force=True,
        details={"symbols": len(target_symbols)},
    )
    
    scan_results  = []
    audit_context_by_coin = {}
    trade_executed = False

    # ── فحوصات الحماية العامة (قبل أي عملة) ──────────────────────
    if check_stoploss_guard(state_manager, notifier):
        _finish_scan("ABORTED_SAFE", "STOPLOSS_GUARD")
        return usdt_total
    if check_max_drawdown(state_manager, notifier, usdt_total):
        _finish_scan("ABORTED_SAFE", "MAX_DRAWDOWN_GUARD")
        return usdt_total

    # ── قراءة البروفايل الحالي مرة واحدة قبل دورة المسح (ديناميكي من تيليجرام) ──
    from strategy.filter_profiles import get_filter_profile as _get_fp
    current_filter_profile = _get_fp(state_manager)
    logger.info(f"[FilterProfile] البروفايل المستخدم: {current_filter_profile.get('label', '')} | Fallback: {current_filter_profile.get('enable_fallback', False)} | max_ob_age: {current_filter_profile.get('max_ob_age', 80)}")

    # لقطة واحدة مغلقة لـBTC/ETH لكل دورة. هي سياسة تنفيذ وليست مولد إشارة.
    market_regime = detect_market_regime(client)
    logger.info(
        f"[MarketRegime] {market_regime.get('label')} | "
        f"reason={market_regime.get('reason')} | "
        f"high_vol={market_regime.get('high_volatility', False)}"
    )
    audit_decision_event(
        scan_id=scan_id,
        decision_id=f"{scan_id}:MARKET_REGIME",
        stage="MARKET_REGIME",
        status=str(market_regime.get("label") or "UNKNOWN"),
        details=market_regime,
    )

    # 3. دورة البحث عن الصفقات
    for symbol in target_symbols:
        symbol = symbol.split(":")[0]
        coin_name = symbol.split('/')[0]
        decision_id = f"{scan_id}:{symbol.replace('/', '')}"
        audit_context_by_coin[coin_name] = (decision_id, symbol)
        # تهيئة مبكرة لتجنب UnboundLocalError في أي مسار تنفيذ
        coin_data = {
            'coin': coin_name,
            'symbol': symbol,
            'price': 0,
            '_scan_id': scan_id,
            '_decision_id': decision_id,
        }
        ticker    = None
        audit_decision_event(
            scan_id=scan_id,
            decision_id=decision_id,
            symbol=symbol,
            stage="SCAN_STARTED",
            status="STARTED",
        )
        logger.info(f"--- فحص الزوج: {symbol} ---")

        # [فلتر مبكر]: تخطي العملة فوراً إذا كانت مفتوحة في active_symbols (قبل أي تحليل)
        if symbol in active_symbols:
            logger.info(f"[{symbol}] ⏭️ تخطي: صفقة مفتوحة مسبقاً في active_symbols.")
            scan_results.append({'coin': coin_name, 'decision': '⏭️ ACTIVE', 'reason': 'صفقة مفتوحة', 'price': 0, 'rsi': 0})
            continue

        # يمنع Scanner من تجاوز شروط Sniper أثناء وجود أمر افتراضي معلق.
        if symbol in state_manager.get_virtual_orders():
            logger.info(
                f"[{symbol}] ⏭️ تخطي Scanner: يوجد أمر Sniper معلق — "
                "التنفيذ مسموح فقط لمسار monitor_virtual_orders."
            )
            scan_results.append({
                'coin': coin_name,
                'decision': '⏳ PENDING',
                'reason': 'أمر Sniper معلق',
                'price': 0,
                'rsi': 0,
            })
            continue

        # [فلتر مبكر إضافي]: تحقق مباشر من باينانس كطبقة حماية ثانية ضد Race Condition
        # (صفقة فُتحت عبر Virtual Order أو Telegram ولم يتحدّث active_symbols بعد)
        try:
            open_state = client.has_open_position(symbol)
            if open_state is None:
                logger.warning(f"[{symbol}] ⏭️ تخطي آمن: تعذر التأكد من وجود مركز على باينانس.")
                scan_results.append({
                    'coin': coin_name,
                    'decision': '⚠️ UNKNOWN',
                    'reason': 'تعذر التحقق من المركز على Binance',
                    'price': 0,
                    'rsi': 0,
                })
                continue
            if open_state:
                logger.warning(f"[{symbol}] ⏭️ تخطي: صفقة مفتوحة فعلياً على باينانس ولكنها غير مُسجَّلة في active_symbols بعد — تصحيح تلقائي.")
                scan_results.append({'coin': coin_name, 'decision': '⏭️ ACTIVE', 'reason': 'صفقة مفتوحة (باينانس)', 'price': 0, 'rsi': 0})
                state_manager.add_active_symbol(symbol)
                active_symbols.add(symbol)  # تحديث المتغير المحلي فقط
                continue
        except Exception as _e:
            logger.debug(f"[{symbol}] تعذّر فحص has_open_position: {_e}")
        
        # [LowProfitPairs] - تجاوز العملة إذا كانت مجمّدة بسبب خسائر متتالية
        if check_low_profit_pair(state_manager, notifier, symbol):
            scan_results.append({'coin': coin_name, 'decision': '❄️ FROZEN', 'reason': 'مجمّدة بسبب خسائر متتالية', 'price': 0})
            continue

        # [تطبيق PairLocks] - تجاوز العملة إذا كانت محظورة بسبب الخسائر
        if state_manager.is_pair_locked(symbol):
            lock_info = state_manager.get_pair_lock_info(symbol)
            logger.warning(f"[{symbol}] 🔒 العملة محظورة مؤقتاً بسبب: {lock_info.get('reason', 'غير محدد')}. سيتم تجاوزها.")
            scan_results.append({
                'coin': coin_name, 'decision': '🔒 PAIR_LOCK',
                'reason': lock_info.get('reason', 'قفل مؤقت'), 'price': 0,
            })
            continue
        
        # [تطبيق Blacklist] - تجاوز العملة إذا كانت في القائمة السوداء الدائمة
        if state_manager.is_blacklisted(symbol):
            logger.info(f"[{symbol}] 🚫 تخطي: العملة في القائمة السوداء.")
            scan_results.append({'coin': coin_name, 'decision': '🚫 BLACKLIST', 'reason': 'في القائمة السوداء', 'price': 0})
            continue
        
        # تصنيف العملة وقواعد حماية Meme/New
        coin_type, classification_reason = universe_filter.classify_symbol(symbol)
        logger.info(f"[{symbol}] تصنيف العملة: {coin_type.upper()} ({classification_reason})")
        
        # إعدادات الرافعة والمخاطرة المخصصة
        trade_leverage = state_manager.get_leverage()

        trade_risk_pct = state_manager.get_risk_pct() / 100.0

        
        if coin_type in ['meme', 'new']:
            # لا نغيّر الرافعة هنا؛ الرافعة تتحكم بها Telegram/Config.
            # نخفض المخاطرة فقط للعملات عالية الخطورة.
            trade_risk_pct = getattr(Config, 'MEME_RISK_PER_TRADE_PERCENT', 0.25) / 100.0
            logger.info(f"[{symbol}] تطبيق إعدادات حماية الميم/الجديد: رافعة {trade_leverage}x | مخاطرة {trade_risk_pct*100:.2f}%")
            
        # تطبيق مضاعف مخاطرة Testnet إن وجد
        if getattr(Config, 'USE_TESTNET', False) and hasattr(Config, 'TESTNET_RISK_MULTIPLIER'):
            trade_risk_pct = trade_risk_pct * getattr(Config, 'TESTNET_RISK_MULTIPLIER', 1.0)
            logger.info(f"[{symbol}] تطبيق مضاعف مخاطرة التست نت: {getattr(Config, 'TESTNET_RISK_MULTIPLIER')}x -> مخاطرة مخفضة {trade_risk_pct*100:.3f}%")

        # أ) التحليل الفني الأساسي (15 دقيقة). اتصال واحد multiplexed يحل محل
        # Thread/WebSocket منفصل لكل رمز؛ تُحفظ فقط رموز جولة المسح الحالية.
        candle_health_before = _candle_stream.get_symbol_health(symbol)
        candle_source = candle_health_before.get("source", "UNAVAILABLE")
        bars_df = _candle_stream.get_candles(symbol)
        history_required = _candle_stream.MIN_DECISION_HISTORY
        if _candle_stream.needs_history_backfill(symbol, history_required):
            seed_df = client.fetch_ohlcv(symbol, timeframe='15m', limit=Config.DECISION_CANDLE_HISTORY)
            if seed_df is not None and not seed_df.empty:
                _candle_stream.replace_seed(symbol, seed_df)
                bars_df = seed_df
                candle_source = "BINANCE_USDM_FUTURES_REST_HISTORY_BACKFILL"
                if len(seed_df) >= history_required:
                    logger.info(
                        f"[SharedCandle] {symbol}: اكتمل تاريخ القرار من REST "
                        f"({len(seed_df)}/{history_required} شمعة)."
                    )
                else:
                    logger.warning(
                        f"[SharedCandle] {symbol}: تاريخ REST ما زال أقصر من حد القرار "
                        f"({len(seed_df)}/{history_required})؛ سيبقى محرك المؤشرات آمناً."
                    )
        elif _candle_stream.is_fresh(symbol, max_age_seconds=30):
            bars_df = _candle_stream.get_candles(symbol)
            candle_source = "BINANCE_USDM_FUTURES_SHARED_WS"
        else:
            # لا نتخذ قراراً من نافذة قديمة. REST Futures يعيد مزامنة هذا الرمز.
            bars_df = client.fetch_ohlcv(symbol, timeframe='15m', limit=Config.DECISION_CANDLE_HISTORY)
            if bars_df is not None and not bars_df.empty:
                _candle_stream.replace_seed(symbol, bars_df)
                candle_source = "BINANCE_USDM_FUTURES_REST_STALE_FALLBACK"
                logger.warning(f"[SharedCandle] {symbol}: استُخدم REST Futures بسبب WS غير حديث.")

        if bars_df is None or bars_df.empty:
            scan_results.append({'coin': coin_name, 'decision': '⚪ SKIP', 'reason': 'فشل جلب البيانات 15m', 'price': 0, 'rsi': 0, 'macd': 0, 'bb_low': 0, 'bb_high': 0})
            continue
            
        bars_df = ta_engine.add_indicators(
            bars_df,
            filter_profile=current_filter_profile,
        )
        ta_details = {}
        ema_mode = state_manager.get("ema_mode", "dual")
        htf_trend = ta_engine.get_htf_trend(symbol, client, ema_mode=ema_mode)
        engine_trend = ta_engine.evaluate_trend(bars_df, symbol=symbol, details=ta_details, htf_trend=htf_trend, ema_mode=ema_mode, filter_profile=current_filter_profile)
        ta_details["engine_trend"] = engine_trend
        ta_details["engine_reason"] = ta_details.get("reason", "")
        ta_details["market_regime"] = market_regime
        
        # ── [Signal Deduplication] - منع التكرار على نفس الشمعة ──
        global signal_dedup
        if signal_dedup is not None and len(bars_df) >= 2:
            try:
                # قراءة timestamp الحقيقي من العمود (يدعم مصدرَيْ WS وREST)
                row = bars_df.iloc[-2]
                if 'time' in bars_df.columns and pd.notna(row.get('time')):
                    bar_open_time = int(row['time'])                             # WS: int ms
                elif 'timestamp' in bars_df.columns and pd.notna(row.get('timestamp')):
                    ts = row['timestamp']
                    bar_open_time = int(ts.timestamp() * 1000) if hasattr(ts, 'timestamp') else int(ts)  # REST: datetime
                else:
                    bar_open_time = None

                if bar_open_time is not None and signal_dedup.is_duplicate(symbol, bar_open_time):
                    logger.debug(f"[{symbol}] ⏭️ تخطي (Dedup) — تم الدخول على هذه الشمعة مسبقاً. [bar_time={bar_open_time}]")
                    # إرجاع سعر افتراضي للاستمرار دون كسر scan_results
                    _ctx_price = float(bars_df.iloc[-2].get('close', 0)) if len(bars_df) > 1 else 0.0
                    scan_results.append({'coin': coin_name, 'decision': '⏭️ DEDUP', 'reason': 'تخطي تكرار الإشارة', 'price': _ctx_price})
                    continue
            except Exception as e:
                logger.warning(f"[{symbol}] خطأ في فحص منع التكرار: {e}")
        
        # ── Strategy Manager: First-Signal-Wins ──────────────────────
        # ملاحظة: coin_data و ticker لم يُعرَّفا بعد في هذه المرحلة (يأتيان لاحقاً)
        # نستخدم سعر آخر شمعة مغلقة كسعر مرجعي مؤقت للتحليل
        _ctx_price = float(bars_df.iloc[-2].get('close', 0)) if len(bars_df) > 1 else 0.0
        _ctx_coin_data = {'coin': coin_name, 'price': _ctx_price}
        ctx = StrategyContext(
            symbol=symbol,
            price=_ctx_price,
            bars_df=bars_df,
            ta_data=ta_details,
            coin_data=_ctx_coin_data,
            ticker=None,
            client=client,
        )

        ta_trend        = "hold"
        winning_strategy = "none"
        strategy_result  = {"signal": "hold", "reason": "لا توجد إشارة من أي استراتيجية", "ta_data": {}}

        for strat in active_strategies:
            try:
                # OBVPumpDumpStrategy تدعم state_manager للـ cooldown، بقية الاستراتيجيات لا
                if isinstance(strat, OBVPumpDumpStrategy):
                    result = strat.execute(ctx, state_manager=state_manager)
                else:
                    result = strat.execute(ctx)
                if result.get("signal") in ("buy", "sell"):
                    strategy_result  = result
                    ta_trend         = result["signal"]
                    winning_strategy = strat.get_description()
                    logger.info(f"[{symbol}] ✅ إشارة من: {winning_strategy} → {ta_trend.upper()}")
                    break
            except Exception as e:
                logger.warning(f"[{symbol}] خطأ في استراتيجية {strat.get_description()}: {e}")


        coin_data['strategy_reason']  = strategy_result["reason"]
        coin_data['winning_strategy'] = winning_strategy
        
        # إعادة استخدام اتجاه 1H الذي حسبه get_htf_trend أعلاه. لا نعيد جلب
        # الشموع ولا نستدعي evaluate_trend لأنها دالة إشارة دخول وليست دالة اتجاه HTF.
        trend_1h = htf_trend
        logger.info(f"[{symbol}] اتجاه فريم 1 ساعة (1H Trend): {trend_1h}")

        # evaluate_trend هو مصدر سياسة HTF الوحيد. فشل بيانات 1H يعيد unknown
        # ويمنع المرشح، أما التعارض الحقيقي فيبقى مرشح انعكاس فقط إذا سمح
        # البروفايل؛ بوابة 5m المشتركة ستطلب عندها sweep+volume+rejection معاً.
        if ta_details.get("countertrend"):
            logger.warning(
                f"[{symbol}] مرشح انعكاس عكس 1H؛ لن يُنفذ دون تأكيد 5m مشدد."
            )

        # إصلاح: استخدام الشمعة المغلقة [-2] بدلاً من الشمعة الحية [-1] لتجنب Lookahead Bias
        latest = bars_df.iloc[-2] if len(bars_df) >= 2 else bars_df.iloc[-1]
        latest_ema50 = float(latest.get('ema_50', 0) or 0)
        latest_ema200 = float(latest.get('ema_200', 0) or 0)
        latest_rsi = float(latest.get('rsi', 0) or 0)
        analysis_side = None
        if (
            trend_1h == "bullish"
            and latest_ema50 > latest_ema200 > 0
            and 42.0 <= latest_rsi <= 72.0
        ):
            analysis_side = "buy"
        elif (
            trend_1h == "bearish"
            and 0 < latest_ema50 < latest_ema200
            and 28.0 <= latest_rsi <= 58.0
        ):
            analysis_side = "sell"
        analysis_context_1h = (
            ta_engine.get_setup_context_1h(
                symbol, client, analysis_side, ema_mode=ema_mode
            )
            if analysis_side else {}
        )
        coin_data = {
            'coin': coin_name,
            'symbol': symbol,
            '_scan_id': scan_id,
            '_decision_id': decision_id,
            'price':    latest.get('close', 0),
            'open':     latest.get('open', 0),
            'rsi':      latest.get('rsi', 0),
            'macd':     latest.get('macd', 0),
            'volume':   latest.get('volume', 0),
            'volume_avg_20': latest.get('volume_avg_20', 0),
            'volume_median_20': latest.get('volume_median_20', 0),
            'rvol_20': latest.get('rvol_20', 0),
            'ema_50':   latest.get('ema_50', 0),
            'bb_low':   latest.get('bb_low', 0),
            'bb_high':  latest.get('bb_high', 0),
            'support':  ta_details.get('support', 0),
            'resistance': ta_details.get('resistance', 0),
            'market_levels': ta_details.get('market_levels', {}),
            '_ta_context': build_entry_ta_context(ta_details),
            # نحافظ على مصدر الاستراتيجية بعد إعادة بناء coin_data للتسجيل وإدارة المخاطر.
            'strategy_reason': strategy_result.get('reason', ''),
            'winning_strategy': winning_strategy,
            'setup_type': winning_strategy,
            'setup_score': ta_details.get('score', 0),
            'bottom_fishing_active': bool(ta_details.get('bottom_fishing_active', False)),
            'ob_mid': ta_details.get('ob_mid', latest.get('close', 0)),
            'invalidation_level': ta_details.get('invalidation_level', 0),
            # ميل مراقبة فقط لعرض أفضل الأفكار قبل اكتمال SMC. لا يُقرأ في
            # final_decision ولا يمنح أي صلاحية تنفيذ.
            'analysis_side': analysis_side,
            'analysis_side_source': 'HTF_1H_AND_15M_EMA_OBSERVATION' if analysis_side else None,
            'context_1h': analysis_context_1h,
        }
        if getattr(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", False):
            closed_15m_at = _closed_bar_end_utc(latest, 15)
            coin_data["_strategy_v2_shadow_sources"] = {
                "15m": {
                    "confirmed_at": (
                        closed_15m_at.isoformat() if closed_15m_at else ""
                    ),
                    "source": "run_bot_iteration.closed_15m_bar",
                }
            }
        audit_decision_event(
            scan_id=scan_id,
            decision_id=decision_id,
            symbol=symbol,
            stage="MARKET_CONTEXT",
            status="CAPTURED",
            details={
                "decision_time": datetime.now(timezone.utc).isoformat(),
                "timeframe": "15m",
                "candle_source": candle_source,
                "candle_health_before": candle_health_before,
                "analysis_candle_count": len(bars_df),
                "minimum_decision_candles": history_required,
                "closed_bar": {
                    "open_time": latest.get("timestamp", latest.get("time")),
                    "open": latest.get("open"), "high": latest.get("high"),
                    "low": latest.get("low"), "close": latest.get("close"),
                    "volume": latest.get("volume"),
                },
                "indicators": {
                    key: latest.get(key) for key in (
                        "rsi", "macd", "macd_signal", "atr", "adx",
                        "ema_50", "ema_100", "ema_200",
                    )
                },
                "htf_trend": trend_1h,
                "engine_trend": engine_trend,
                "market_regime": market_regime,
                "setup_context": coin_data.get("_ta_context"),
                "market_levels": coin_data.get("market_levels"),
            },
        )
        
        # ب) تحليل المشاعر فقط عند وجود إشارة فنية لتجنب استنزاف حصة API.
        sentiment = None
        if (
            ta_trend in ("buy", "sell")
            and getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False)
        ):
            # المرحلة 1: LunarCrush (الأسرع والأكثر تخصصاً للكريبتو)
            logger.info(f"[{symbol}] جاري سؤال LunarCrush عن مشاعر {coin_name}...")
            sentiment = social_engine.get_social_sentiment(coin_name)

            if sentiment and sentiment.get('source') == 'lunarcrush':
                logger.info(f"[LunarCrush] {coin_name}: {sentiment.get('label','?').upper()} | Galaxy={sentiment.get('galaxy_score','?')} | Raw={sentiment.get('raw_sentiment_pct','?')}%")
            else:
                # نتائج CMC عامة للسوق وليست خاصة بالعملة؛ نحفظها كآخر fallback
                # لكن نجرب أولاً خبراً خاصاً بالعملة وتحليله بالـ AI.
                social_fallback = sentiment
                news = []
                if getattr(Config, "AI_NEWS_SENTIMENT_ENABLED", False):
                    logger.info(
                        f"[{symbol}] لا توجد بيانات LunarCrush — تجربة NewsAPI+AI الاختيارية."
                    )
                    news = news_engine.fetch_news_for_coin(coin_name, page_size=2)
                if news and news[0].get('title'):
                    title = news[0]['title']
                    description = news[0].get('description', '')
                    full_text = f"{title}. {description}"
                    logger.info(f"أحدث خبر عن {coin_name}: {title}")
                    sentiment = ai_engine.analyze_sentiment(full_text)
                    if sentiment:
                        sentiment["source"] = "news_ai"
                        logger.info(f"[News/AI] {coin_name}: {sentiment['label'].upper()} (ثقة: {sentiment['score']:.2f})")
                    else:
                        sentiment = social_fallback
                else:
                    logger.debug(f"لا توجد أخبار AI مفعلة/حديثة عن {coin_name}.")
                    sentiment = social_fallback
        elif ta_trend in ("buy", "sell"):
            logger.debug(
                f"[{symbol}] المشاعر الآلية معطلة؛ القرار الفني لن ينتظر أي API اختياري."
            )
        else:
            logger.debug(f"[{symbol}] تخطي تحليل المشاعر — لا إشارة فنية (ta_trend={ta_trend}).")
        
        # ج) القرار النهائي (الاستراتيجية الهجينة)
        # نمرر coin_data ليقوم المحرك بمنع الدخول الأعمى إذا كانت المؤشرات سيئة جداً
        sentiment_mode = state_manager.get("sentiment_mode", "full")
        if not getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False):
            sentiment_mode = "ta_only"
        decision_obj = hybrid_strategy.decide(ta_trend, sentiment, ta_data=coin_data, sentiment_mode=sentiment_mode)
        final_decision = decision_obj.get("action", "hold")
        hybrid_reason = decision_obj.get("reason", "انتظار إشارة فنية")
        risk_multiplier = decision_obj.get("risk_multiplier", 1.0)
        
        # توليد معرف فريد للصفقة إذا كان هناك قرار دخول
        trade_id = None
        if final_decision in ("buy", "sell"):
            context_4h = ta_engine.get_market_context_4h(symbol, client)
            context_1h = ta_engine.get_setup_context_1h(
                symbol, client, final_decision, ema_mode=ema_mode
            )
            trade_id = f"tr_{uuid.uuid4().hex[:12]}"
            profile_key = current_filter_profile.get("key", "smc_strict")
            ta_context = build_entry_ta_context(ta_details)
            thesis_id = build_setup_thesis_id(
                symbol, final_decision, ta_context, profile_key
            )
            coin_data.update({
                "_trade_id": trade_id,
                "_ta_context": ta_context,
                "_thesis_id": thesis_id,
                # تحفظ هوية الفكرة حتى لا تضيع عند تحويل القرار إلى REJECT
                # في المراحل اللاحقة وعرضه في Telegram/Dashboard.
                "side": final_decision,
                "technical_signal": ta_trend,
                "htf_trend": trend_1h,
                "market_regime": market_regime,
                "context_4h": context_4h,
                "context_1h": context_1h,
            })
            coin_data["_ta_context"]["context_4h"] = context_4h
            coin_data["_ta_context"]["context_1h"] = context_1h
            audit_decision_event(
                scan_id=scan_id,
                decision_id=decision_id,
                symbol=symbol,
                stage="MARKET_CONTEXT_4H",
                status=context_4h.get("trend", "UNKNOWN"),
                trade_id=trade_id,
                details={**context_4h, "execution_enabled": False},
            )
            audit_decision_event(
                scan_id=scan_id,
                decision_id=decision_id,
                symbol=symbol,
                stage="SETUP_CONTEXT_1H",
                status=context_1h.get("selected_state", "UNKNOWN"),
                trade_id=trade_id,
                details={**context_1h, "execution_enabled": False},
            )

        audit_decision_event(
            scan_id=scan_id,
            decision_id=decision_id,
            signal_id=decision_id,
            setup_id=coin_data.get("_thesis_id"),
            thesis_id=coin_data.get("_thesis_id"),
            symbol=symbol,
            stage="HYBRID_DECISION",
            status=final_decision.upper(),
            trade_id=trade_id,
            details={
                "technical_signal": ta_trend.upper(),
                "htf_trend": trend_1h,
                "strategy": winning_strategy,
                "sentiment_source": sentiment.get("source") if sentiment else None,
                "sentiment_result": sentiment.get("label") if sentiment else None,
                "reason": hybrid_reason,
                "setup_score": ta_details.get("score", 0),
                "setup_thesis_id": coin_data.get("_thesis_id"),
                "countertrend": bool(ta_details.get("countertrend", False)),
                "market_regime": market_regime,
                "market_levels": coin_data.get("market_levels", {}),
            },
        )

        # ── تسجيل القرار في Forensics ──────────────────────────────
        log_signal(
            symbol       = symbol,
            signal       = ta_trend,
            reason       = f"[{coin_data.get('winning_strategy','none')}] {coin_data.get('strategy_reason', ta_trend)}",
            ta_data      = {
                **coin_data,
                "macd_cross_since": ta_details.get("macd_cross_since"),
                "rsi_cross_since":  ta_details.get("rsi_cross_since"),
            },
            sentiment    = sentiment or {},
            # هذه النقطة تسبق Quality/RR/Sniper/Binance؛ تسميتها executed كانت
            # تجعل تقارير SQLite تدّعي صفقات لم تُرسل أصلاً.
            action_taken = "candidate" if final_decision in ("buy", "sell") else ("rejected" if ta_trend in ("buy", "sell") else "held"),
            reject_reason= decision_obj.get("reason", ""),
            trade_id     = trade_id,
        )
        
        # تطبيق مضاعف المخاطرة الخاص بالذكاء الاصطناعي (Risk Reducer)
        if risk_multiplier < 1.0:
            trade_risk_pct = trade_risk_pct * risk_multiplier
            logger.info(f"[{symbol}] تم خفض المخاطرة بنسبة { (1 - risk_multiplier) * 100 }% بناءً على قرار الذكاء الاصطناعي.")
            
        logger.info(f"[{symbol}] القرار المدمج النهائي: {final_decision.upper()} | السبب: {hybrid_reason}")
        
        # طباعة التقرير التفصيلي اللوجي
        ema_200 = ta_details.get('ema_200', 0)
        close_price = ta_details.get('close', 0)
        ema_trend = 'neutral'
        if ema_200 > 0:
            ema_trend = 'bullish' if close_price > ema_200 else 'bearish'
        if getattr(Config, 'TESTNET_DEBUG_MODE', False):
            rejection_reason = hybrid_reason  # ✅ تعريف المتغير قبل استخدامه
            sentiment_lbl = sentiment.get('label', 'neutral') if sentiment else 'neutral'

            logger.info(f"--- [DECISION PIPELINE DEBUG : {symbol}] ---")
            logger.info(f"TA raw signal: {ta_trend.upper()}")
            logger.info(f"Score: {ta_details.get('score', 0)}")
            logger.info(f"ADX: {ta_details.get('adx', 0):.1f}")
            logger.info(f"EMA200 trend: {ema_trend}")
            logger.info(f"1H trend: {trend_1h}")
            logger.info(f"Sentiment: {sentiment_lbl.upper()}")
            logger.info(f"Derivatives mode: {Config.DERIVATIVES_FILTER_MODE}")
            logger.info(f"Initial Hybrid Decision: {final_decision.upper()}")
            logger.info(f"Rejection Reason (so far): {rejection_reason}")
            logger.info(f"--------------------------------------------")
        else:
            rejection_reason = hybrid_reason  # ✅ تعريف المتغير في المسار العادي أيضاً
            logger.info(f"📊 [ملخص فحص {symbol}] | النقاط: {ta_details.get('score', 0)} | ADX: {ta_details.get('adx', 0):.1f} | EMA200 ترند: {ema_trend} | فريم 1H ترند: {trend_1h} | القرار النهائي: {final_decision.upper()} | السبب: {rejection_reason}")

        
        if final_decision == "buy":
            pending_short = state_manager.get_virtual_orders().get(symbol)
            if (
                isinstance(pending_short, dict)
                and pending_short.get("execution_policy")
                == SHORT_HIGH_CONFIDENCE_V1
            ):
                finalize_virtual_order(
                    state_manager,
                    notifier,
                    symbol,
                    pending_short,
                    "SHORT_CANCEL_HYBRID_FLIPPED_BUY",
                )

        if final_decision in ['buy', 'sell']:
            cooldown = state_manager.get_setup_cooldown(
                symbol, coin_data.get("_thesis_id")
            )
            if cooldown:
                reason = (
                    "إعداد هيكلي مكرر تحت التهدئة بعد نتيجة سابقة: "
                    f"{cooldown.get('reason', 'UNKNOWN')}"
                )
                logger.warning(f"[{symbol}] رفض إعادة تدوير الإعداد: {reason}")
                audit_decision_event(
                    scan_id=scan_id,
                    decision_id=decision_id,
                    symbol=symbol,
                    stage="SETUP_COOLDOWN",
                    status="REJECTED",
                    trade_id=trade_id,
                    details={
                        "reason": reason,
                        "setup_thesis_id": coin_data.get("_thesis_id"),
                        "expires_at": cooldown.get("expires_at"),
                    },
                )
                coin_data.update({
                    "decision": "🔴 REJECT",
                    "reason": reason,
                    "rejection_stage": "SETUP_COOLDOWN",
                    "cooldown_expires_at": cooldown.get("expires_at"),
                })
                scan_results.append(coin_data)
                try:
                    from utils.forensics_logger import update_signal_outcome
                    update_signal_outcome(trade_id, "REJECTED_SETUP_COOLDOWN", 0.0)
                except Exception:
                    pass
                continue

            audit_decision_event(
                scan_id=scan_id,
                decision_id=decision_id,
                signal_id=decision_id,
                setup_id=coin_data.get("_thesis_id"),
                thesis_id=coin_data.get("_thesis_id"),
                symbol=symbol,
                stage="SETUP_CREATED",
                status="ACTIVE",
                trade_id=trade_id,
                details={
                    "side": final_decision.upper(),
                    "market_regime": market_regime,
                    "setup_score": ta_details.get("score", 0),
                    "setup_anchor": (coin_data.get("_ta_context") or {}).get("setup_anchor"),
                },
            )

            # تدقيق مشتقات Binance Futures Mainnet وتسجيله للمراقبة/المنع
            if Config.DERIVATIVES_FILTER_MODE != "off":
                deriv_result = derivatives_filter.evaluate_risk(symbol, final_decision, coin_data['price'], client=client)
                # نحتفظ بالسياق حتى في ALLOW/WARNING ليظهر في الدرجة
                # التشخيصية؛ غياب OI/Funding الاختياري لا يتحول إلى رفض.
                coin_data['derivatives_result'] = deriv_result
                derivatives_filter.log_audit_result(symbol, final_decision, coin_data['price'], deriv_result)
                audit_decision_event(
                    scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                    stage="DERIVATIVES_FILTER",
                    status=deriv_result.get("decision", "UNKNOWN"), trade_id=trade_id,
                    details={"result": deriv_result},
                )
                
                # إذا تم اقتراح المنع وكان وضع التفعيل نشطاً
                if Config.DERIVATIVES_FILTER_MODE == "enforce" and deriv_result['decision'] == "BLOCK_SUGGESTED":
                    logger.warning(f"[{symbol}] ❌ تم منع الصفقة بواسطة فلتر مشتقات Binance: {deriv_result['reason']}")
                    state_manager.increment_rejection_counter("Derivatives (Binance)")
                    coin_data['decision'] = '🔴 REJECT'
                    coin_data['reason'] = f"حظر مشتقات Binance: {deriv_result['reason']}"
                    coin_data['rejection_stage'] = 'DERIVATIVES_FILTER'
                    coin_data['derivatives_result'] = deriv_result
                    scan_results.append(coin_data)
                    if trade_id:
                        try:
                            from utils.forensics_logger import update_signal_outcome
                            update_signal_outcome(trade_id, f"REJECTED_DERIVATIVES: {deriv_result['reason']}", 0.0)
                        except Exception:
                            pass
                    continue
                    

                
            # جلب تفاصيل التغير السعري في 24 ساعة لتفادي الشراء في القمة لعملات الميم/الجديدة
            ticker = None
            try:
                ticker = client.exchange.fetch_ticker(symbol)
            except Exception as e:
                logger.warning(f"تعذر جلب بيانات Ticker للزوج {symbol}: {e}")
                
            change_24h = ticker.get('percentage', 0) if ticker else 0
            logger.info(f"[{symbol}] نسبة تغير السعر في 24 ساعة: {change_24h:+.2f}%")
            
            # حماية الشراء في القمم (Anti-Pump Filter) للعملات عالية الخطورة
            if coin_type in ['meme', 'new'] and change_24h > 15.0 and final_decision == 'buy':
                logger.warning(f"[{symbol}] ❌ تم رفض الشراء: العملة ميم/جديدة وهي في حالة صعود حاد (Pump > 15%). السعر الحالي قد يكون قمة.")
                state_manager.increment_rejection_counter("Anti-Pump")
                coin_data['decision'] = '🔴 REJECT'
                coin_data['reason'] = f"تجنب شراء قمة (صعود 24h: {change_24h:.1f}%)"
                coin_data['rejection_stage'] = 'ANTI_PUMP_FILTER'
                coin_data['change_24h_pct'] = change_24h
                scan_results.append(coin_data)
                if trade_id:
                    try:
                        from utils.forensics_logger import update_signal_outcome
                        update_signal_outcome(trade_id, f"REJECTED_PUMP: change_24h={change_24h:.1f}%", 0.0)
                    except Exception:
                        pass
                continue

            global ws_manager
            price_snapshot = ws_manager.get_price_with_source(
                symbol, fallback_fn=client.get_current_price
            )
            current_price = float(price_snapshot.get("price") or 0.0)
            audit_decision_event(
                scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                stage="PRICE_SOURCE",
                status=price_snapshot.get("source", "UNKNOWN"),
                trade_id=trade_id,
                details=price_snapshot,
            )
            if current_price:
                # Optional, user-controlled Demo mode: the current runtime's
                # HYBRID BUY/SELL decision is itself the user-visible proposal.
                # It bypasses only the later strategy selectivity gates
                # (Quality/RR minimum/5m confirmation). Hard execution safety
                # remains centralized in execute_and_protect_trade.
                demo_signal_mode = (
                    state_manager.get_demo_signal_execution_mode()
                    if hasattr(state_manager, "get_demo_signal_execution_mode")
                    else "strict_proposal"
                )
                if (
                    Config.TELEGRAM_PROPOSAL_DEMO_MODE
                    and demo_signal_mode == "hybrid_decision"
                ):
                    short_confidence = None
                    if (
                        final_decision == "sell"
                        and getattr(
                            Config, "SHORT_HIGH_CONFIDENCE_V1_ENABLED", True
                        )
                    ):
                        _short_confidence_counter(
                            state_manager, "HYBRID_SELL_TOTAL"
                        )
                    atr_val = (
                        float(bars_df.iloc[-2].get('atr', 0))
                        if len(bars_df) > 1 else 0.0
                    )
                    if atr_val <= 0.0:
                        logger.warning(
                            f"[{symbol}] HYBRID proposal rejected safely: ATR=0."
                        )
                        coin_data.update({
                            'decision': '🔴 REJECT',
                            'reason': 'رفض أمان Proposal: ATR=0',
                            'rejection_stage': 'DATA_QUALITY',
                        })
                        scan_results.append(coin_data)
                        continue

                    if (
                        final_decision == "sell"
                        and getattr(
                            Config, "SHORT_HIGH_CONFIDENCE_V1_ENABLED", True
                        )
                    ):
                        short_trigger_bars = client.fetch_ohlcv(
                            symbol,
                            timeframe=getattr(
                                Config, "ENTRY_CONFIRMATION_TIMEFRAME", "5m"
                            ),
                            limit=getattr(Config, "ENTRY_CONFIRMATION_LIMIT", 80),
                        )
                        short_confidence = evaluate_short_high_confidence(
                            decision_price=current_price,
                            atr=atr_val,
                            trend_1h=trend_1h,
                            context_4h=coin_data.get("context_4h"),
                            context_1h=coin_data.get("context_1h"),
                            market_levels=coin_data.get("market_levels"),
                            trigger_bars=short_trigger_bars,
                            invalidation_level=ta_details.get(
                                "invalidation_level", current_price
                            ),
                        )
                        _audit_short_confidence(
                            state_manager=state_manager,
                            symbol=symbol,
                            evaluation=short_confidence,
                            scan_id=scan_id,
                            decision_id=decision_id,
                            trade_id=trade_id,
                            thesis_id=coin_data.get("_thesis_id"),
                        )
                        if short_confidence.get("status") == "REJECTED":
                            reason = str(short_confidence.get("reason"))
                            coin_data.update({
                                "decision": "🔴 REJECT",
                                "reason": _short_policy_rejection_label(reason),
                                "rejection_stage": SHORT_HIGH_CONFIDENCE_V1,
                                "short_high_confidence": short_confidence,
                            })
                            scan_results.append(coin_data)
                            continue

                    from strategy.filter_profiles import get_filter_profile
                    proposal_profile = _hybrid_decision_demo_profile(
                        get_filter_profile(state_manager)
                    )
                    proposed_entry = float(current_price)
                    invalidation_level = ta_details.get(
                        'invalidation_level', proposed_entry
                    )
                    proposal_sl, proposal_tp = risk_manager.calculate_sl_tp(
                        final_decision,
                        proposed_entry,
                        support=coin_data.get('support'),
                        resistance=coin_data.get('resistance'),
                        atr=atr_val,
                        invalidation_level=invalidation_level,
                        market_levels=coin_data.get('market_levels', {}),
                        filter_profile=proposal_profile,
                        setup_anchor=((coin_data.get('_ta_context') or {}).get('setup_anchor')),
                    )
                    if proposal_sl is None or proposal_tp is None:
                        reason = (
                            risk_manager.last_block_reason
                            or "NO_EXECUTABLE_STRUCTURAL_STOP_OR_TARGET"
                        )
                        logger.warning(
                            f"[{symbol}] HYBRID proposal rejected safely: {reason}"
                        )
                        coin_data.update({
                            'decision': '🔴 REJECT',
                            'reason': reason,
                            'rejection_stage': 'PROPOSAL_GEOMETRY',
                        })
                        scan_results.append(coin_data)
                        continue

                    amount = risk_manager.calculate_position_size(
                        usdt_total,
                        proposed_entry,
                        custom_risk_percent=trade_risk_pct,
                        atr=atr_val,
                        sl=proposal_sl,
                        leverage=trade_leverage,
                        entry_margin_percent=Config.ENTRY_MARGIN_PERCENT,
                        available_balance=usdt_free,
                    )
                    margin_required = (
                        float(amount) * proposed_entry / float(trade_leverage)
                    )
                    if amount <= 0 or margin_required > usdt_free * 0.95:
                        logger.warning(
                            f"[{symbol}] HYBRID proposal rejected safely: "
                            "invalid size or insufficient free margin."
                        )
                        coin_data.update({
                            'decision': '🔴 REJECT',
                            'reason': 'حجم غير صالح أو هامش غير كافٍ',
                            'rejection_stage': 'POSITION_SIZING',
                        })
                        scan_results.append(coin_data)
                        continue

                    if (
                        short_confidence
                        and short_confidence.get("status")
                        == "WAIT_SHORT_CONFIRMATION"
                    ):
                        existing_wait = state_manager.get_virtual_orders().get(symbol)
                        if existing_wait:
                            logger.info(
                                f"[{symbol}] SHORT_HIGH_CONFIDENCE_V1: "
                                "انتظار قائم بالفعل؛ لن يُنشأ Thesis مكرر."
                            )
                            continue
                        created_at = datetime.now(timezone.utc)
                        max_age_minutes = int(
                            getattr(Config, "SNIPER_MAX_WAIT_MINUTES", 15)
                        )
                        virtual_order_id = f"shcv1_{uuid.uuid4().hex[:16]}"
                        short_wait = {
                            "virtual_order_id": virtual_order_id,
                            "execution_policy": SHORT_HIGH_CONFIDENCE_V1,
                            "proposal_execution_mode": "HYBRID_DECISION_DEMO",
                            "trade_id": trade_id,
                            "scan_id": scan_id,
                            "decision_id": decision_id,
                            "side": "sell",
                            "entry_price": proposed_entry,
                            "optimal_entry": proposed_entry,
                            "sl": float(proposal_sl),
                            "tp": float(proposal_tp),
                            "invalidation_level": float(invalidation_level),
                            "amount": float(amount),
                            "atr": float(atr_val),
                            "support": float(coin_data.get("support", 0) or 0),
                            "resistance": float(coin_data.get("resistance", 0) or 0),
                            "trade_leverage": int(trade_leverage),
                            "trade_risk_pct": float(trade_risk_pct),
                            "created_at": created_at.isoformat(),
                            "created_at_ms": int(created_at.timestamp() * 1000),
                            "expires_at": (
                                created_at + timedelta(minutes=max_age_minutes)
                            ).isoformat(),
                            "max_age_minutes": max_age_minutes,
                            "filter_profile_snapshot": proposal_profile,
                            "market_levels": coin_data.get("market_levels", {}),
                            "setup_thesis_id": coin_data.get("_thesis_id"),
                            "setup_context": coin_data.get("_ta_context", {}),
                            "context_1h": coin_data.get("context_1h", {}),
                            "context_4h": coin_data.get("context_4h", {}),
                            "trend_1h": trend_1h,
                            "target_plan": dict(
                                getattr(risk_manager, "last_plan", {}) or {}
                            ),
                            "last_short_confidence": short_confidence,
                            "last_confirmation_bar": (
                                short_confidence.get("trigger") or {}
                            ).get("bar_id"),
                            "sentiment_label": (
                                sentiment.get("label", "neutral")
                                if sentiment else "neutral"
                            ),
                            "sentiment_score": float(
                                sentiment.get("score")
                                or sentiment.get("galaxy_score")
                                or 0.0
                            ) if sentiment else 0.0,
                            "ta_trend": ta_trend,
                        }
                        if not state_manager.save_virtual_order(symbol, short_wait):
                            logger.critical(
                                f"[{symbol}] تعذر حفظ WAIT_SHORT_CONFIRMATION؛ "
                                "لن يتم التنفيذ."
                            )
                            continue
                        audit_decision_event(
                            scan_id=scan_id,
                            decision_id=decision_id,
                            signal_id=decision_id,
                            setup_id=coin_data.get("_thesis_id"),
                            thesis_id=coin_data.get("_thesis_id"),
                            virtual_order_id=virtual_order_id,
                            trade_id=trade_id,
                            symbol=symbol,
                            stage="SHORT_WAIT_CONFIRMATION",
                            status="PENDING",
                            details=short_confidence,
                            durable=True,
                        )
                        pending_result = dict(coin_data)
                        pending_result.update({
                            "decision": "⏳ WAIT SHORT",
                            "reason": "بانتظار تأكيد SHORT خفيف على شمعة 5m مغلقة",
                            "short_high_confidence": short_confidence,
                        })
                        scan_results.append(pending_result)
                        continue

                    coin_data.update({
                        'optimal_entry': proposed_entry,
                        'price': proposed_entry,
                        'target_plan': dict(
                            getattr(risk_manager, 'last_plan', {}) or {}
                        ),
                        'proposal_execution_mode': 'HYBRID_DECISION_DEMO',
                        'short_execution_policy': (
                            SHORT_HIGH_CONFIDENCE_V1
                            if final_decision == 'sell' and short_confidence
                            else None
                        ),
                        'short_high_confidence': short_confidence,
                        'entry_confirmation': {
                            'passed': bool(
                                short_confidence
                                and short_confidence.get('status') == 'PASS'
                            ) if final_decision == 'sell' else False,
                            'reason': (
                                short_confidence.get('reason')
                                if short_confidence
                                else 'NOT_REQUIRED_FOR_USER_VISIBLE_HYBRID_PROPOSAL'
                            ),
                            'source': (
                                SHORT_HIGH_CONFIDENCE_V1
                                if short_confidence else 'HYBRID_DECISION'
                            ),
                            'bar_id': (
                                (short_confidence.get('trigger') or {}).get('bar_id')
                                if short_confidence else latest.get('timestamp', latest.get('time'))
                            ),
                            'evidence': short_confidence or {},
                        },
                    })
                    audit_decision_event(
                        scan_id=scan_id,
                        decision_id=decision_id,
                        signal_id=decision_id,
                        setup_id=coin_data.get('_thesis_id'),
                        thesis_id=coin_data.get('_thesis_id'),
                        symbol=symbol,
                        stage='USER_VISIBLE_HYBRID_PROPOSAL',
                        status='EXECUTION_REQUESTED',
                        trade_id=trade_id,
                        details={
                            'side': final_decision.upper(),
                            'entry': proposed_entry,
                            'sl': float(proposal_sl),
                            'tp': float(proposal_tp),
                            'execution_environment': 'DEMO',
                            'quality_rr_confirmation_recheck': False,
                        },
                        durable=True,
                    )
                    trade_executed = execute_and_protect_trade(
                        symbol, final_decision, amount, proposed_entry,
                        trade_leverage, trade_risk_pct, atr_val, coin_data,
                        sentiment, bars_df, ticker, ta_trend, client,
                        risk_manager, trailing_manager, state_manager,
                        notifier, active_symbols, scan_results,
                        invalidation_level, ta_engine=ta_engine,
                        trade_id=trade_id,
                    )
                    if trade_executed and len(active_symbols) >= max_trades:
                        logger.info(
                            "تم الوصول للحد الأقصى للصفقات المتزامنة. "
                            "إيقاف المسح الحالي."
                        )
                        break
                    continue

                # ✅ فلتر جودة الدخول (منع دخول القمم، النطاق العرضي، والتحركات الحادة)
                quality_audit = {}
                quality_ok, quality_reason = ta_engine.validate_entry_quality(
                    bars_df=bars_df,
                    side=final_decision,
                    current_price=current_price,
                    filter_profile=current_filter_profile,
                    market_regime=market_regime,
                    audit_details=quality_audit,
                )
                coin_data['quality_audit'] = quality_audit
                if not quality_ok:
                    audit_decision_event(
                        scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                        stage="QUALITY_FILTER", status="REJECTED", trade_id=trade_id,
                        details={
                            "reason": quality_reason,
                            "gates": quality_audit,
                            "market_regime": market_regime,
                        },
                    )
                    logger.warning(f"[{symbol}] ❌ رفض جودة الدخول: {quality_reason}")
                    state_manager.increment_rejection_counter("EntryQuality")
                    coin_data['decision'] = '🔴 REJECT'
                    coin_data['reason'] = quality_reason
                    coin_data['price'] = current_price
                    coin_data['rejection_stage'] = 'QUALITY_FILTER'
                    scan_results.append(coin_data)
                    # ── Decision Observer: تسجيل رفض إشارة قوية ───────────
                    try:
                        import builtins as _bi
                        _observer = getattr(_bi, "_global_observer", None)
                        _critic   = getattr(_bi, "_global_ai_critic", None)
                        if _observer is not None:
                            _snap_15m = None
                            try:
                                if bars_df is not None and len(bars_df) > 1:
                                    _r = bars_df.iloc[-2]
                                    _snap_15m = {
                                        "close":   float(_r.get("close",   0) or 0),
                                        "rsi":     round(float(_r.get("rsi",     0) or 0), 2),
                                        "atr":     round(float(_r.get("atr",     0) or 0), 6),
                                        "adx":     round(float(_r.get("adx",     0) or 0), 2),
                                        "ema_200": round(float(_r.get("ema_200", 0) or 0), 6),
                                    }
                            except Exception:
                                pass
                            # شموع 1H تُجلب داخل get_htf_trend وتُخزّن نتيجتها فقط؛
                            # لا نكرر الطلب من أجل Observer.
                            _snap_1h = {"trend": trend_1h}
                            _observer.record_rejected_signal(
                                event={
                                    "event_type":          "SIGNAL_REJECTED_STRONG",
                                    "symbol":              symbol,
                                    "side":                final_decision,
                                    "bot_decision":        "REJECT",
                                    "bot_reasoning":       quality_reason,
                                    "price_at_decision":   float(current_price),
                                    "entry_quality_score": float((ta_details or {}).get("score", 0) or 0),
                                    "mtf_snapshot":        {"15m": _snap_15m, "1h": _snap_1h},
                                },
                                client=client,
                                ta_engine=ta_engine,
                                ai_engine_critic=_critic,
                            )
                    except Exception as _obs_exc:
                        logger.debug(f"[DecisionObserver] فشل تسجيل REJECT: {_obs_exc}")
                    if trade_id:
                        try:
                            from utils.forensics_logger import update_signal_outcome
                            update_signal_outcome(trade_id, f"REJECTED_QUALITY: {quality_reason}", 0.0)
                        except Exception:
                            pass
                    continue

                audit_decision_event(
                    scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                    stage="QUALITY_FILTER", status="PASS", trade_id=trade_id,
                    details={"gates": quality_audit},
                )
                optimal_entry = ta_details.get('optimal_entry', current_price)
                coin_data["optimal_entry"] = float(optimal_entry)
                invalidation_level = ta_details.get('invalidation_level', current_price)
                
                atr_val = float(bars_df.iloc[-2].get('atr', 0)) if len(bars_df) > 1 else 0.0

                # ── حارس بيانات: إذا كانت ATR=0 فالبيانات ناقصة — لا نفتح الصفقة ──
                if atr_val == 0.0:
                    logger.warning(f"[{symbol}] ATR=0 — بيانات شموع ناقصة أو Testnet. رفض الدخول.")
                    state_manager.increment_rejection_counter("ATR=0")
                    coin_data['decision'] = '🔴 REJECT'
                    coin_data['reason'] = "رفض: ATR=0 (بيانات غير كافية)"
                    coin_data['rejection_stage'] = 'DATA_QUALITY'
                    scan_results.append(coin_data)
                    if trade_id:
                        try:
                            from utils.forensics_logger import update_signal_outcome
                            update_signal_outcome(trade_id, "REJECTED_ATR_ZERO", 0.0)
                        except Exception:
                            pass
                    continue

                # ── جلب البروفايل مرة واحدة — يُستخدم في حساب SL/TP وحساب الحجم ──
                from strategy.filter_profiles import get_filter_profile
                filter_profile = get_filter_profile(state_manager)
                profile_key = filter_profile.get("key", "strict")

                # حساب SL/TP مرة واحدة فقط (يُعاد استخدامه في الأمر المعلق لاحقاً)
                temp_sl, temp_tp = risk_manager.calculate_sl_tp(
                    final_decision, optimal_entry,
                    atr=atr_val,
                    invalidation_level=invalidation_level,
                    market_levels=coin_data.get('market_levels'),
                    filter_profile=filter_profile,
                    setup_anchor=((coin_data.get('_ta_context') or {}).get('setup_anchor')),
                )
                coin_data['target_plan'] = dict(
                    getattr(risk_manager, "last_plan", {}) or {}
                )

                # ── حارس: إذا اكتشف RiskManager مقاومة قوية تعطي RR سيء — نرفض فوراً ──
                if risk_manager.last_block_reason:
                    audit_decision_event(
                        scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                        stage="RR_FILTER", status="REJECTED", trade_id=trade_id,
                        details={
                            "reason": risk_manager.last_block_reason,
                            "target_plan": dict(getattr(risk_manager, "last_plan", {}) or {}),
                        },
                    )
                    logger.warning(f"[{symbol}] ❌ رفض الدخول (RR Block): {risk_manager.last_block_reason}")
                    state_manager.increment_rejection_counter("RR_Block")
                    coin_data['decision'] = '🔴 REJECT'
                    coin_data['reason'] = risk_manager.last_block_reason
                    coin_data['rejection_stage'] = 'RR_FILTER'
                    scan_results.append(coin_data)
                    if trade_id:
                        try:
                            from utils.forensics_logger import update_signal_outcome
                            update_signal_outcome(trade_id, f"REJECTED_RR_BLOCK: {risk_manager.last_block_reason}", 0.0)
                        except Exception:
                            pass
                    continue

                audit_decision_event(
                    scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                    stage="RR_FILTER", status="PASS", trade_id=trade_id,
                    details={
                        "sl": temp_sl,
                        "tp": temp_tp,
                        "target_plan": dict(getattr(risk_manager, "last_plan", {}) or {}),
                    },
                )
                amount = risk_manager.calculate_position_size(
                    usdt_total,
                    optimal_entry,
                    custom_risk_percent=trade_risk_pct,
                    atr=atr_val,
                    sl=temp_sl,
                    leverage=trade_leverage,
                    entry_margin_percent=Config.ENTRY_MARGIN_PERCENT,
                    available_balance=usdt_free,
                )

                margin_required = (amount * optimal_entry) / trade_leverage
                if margin_required > usdt_free * 0.95:
                    logger.warning(f"[{symbol}] الهامش المطلوب أكبر من الرصيد الحر. تخطي الصفقة.")
                    state_manager.increment_rejection_counter("Margin")
                    if trade_id:
                        try:
                            from utils.forensics_logger import update_signal_outcome
                            update_signal_outcome(trade_id, "REJECTED_MARGIN: INSUFFICIENT_USDT", 0.0)
                        except Exception:
                            pass
                    continue

                target_plan = dict(getattr(risk_manager, "last_plan", {}) or {})
                planned_risk = abs(float(optimal_entry) - float(temp_sl))
                planned_rr = (
                    abs(float(temp_tp) - float(optimal_entry)) / planned_risk
                    if planned_risk > 0 else 0.0
                )
                coin_data["target_plan"] = target_plan

                # Scanner وSniper يستخدمان الدالة نفسها. القرب من optimal_entry
                # ليس تأكيداً، ولا يسمح بعد الآن بتجاوز شمعة 5m المغلقة.
                trigger_bars = client.fetch_ohlcv(
                    symbol,
                    timeframe=getattr(Config, "ENTRY_CONFIRMATION_TIMEFRAME", "5m"),
                    limit=getattr(Config, "ENTRY_CONFIRMATION_LIMIT", 80),
                )
                confirmation = evaluate_entry_confirmation(
                    side=final_decision,
                    trigger_bars=trigger_bars,
                    current_price=current_price,
                    optimal_entry=optimal_entry,
                    invalidation_level=invalidation_level,
                    atr_15m=atr_val,
                    ta_context=coin_data.get("_ta_context", {}),
                    filter_profile=filter_profile,
                    planned_rr=planned_rr,
                )
                initial_closed_bar = confirmation.get("closed_bar") or {}
                initial_confirmation_bar = confirmation.get("bar_id")
                if (
                    initial_closed_bar.get("exact_closed_bar") is True
                ):
                    initial_confirmation_bar = str(
                        (int(time.time() * 1000) // (5 * 60 * 1000) - 1)
                        * (5 * 60 * 1000)
                    )
                audit_decision_event(
                    scan_id=scan_id,
                    decision_id=decision_id,
                    symbol=symbol,
                    stage="ENTRY_CONFIRMATION",
                    status="PASS" if confirmation.get("passed") else "WAIT",
                    trade_id=trade_id,
                    details={
                        "confirmation": confirmation,
                        "trigger_source": "BINANCE_USDM_FUTURES_REST",
                        "setup_thesis_id": coin_data.get("_thesis_id"),
                        "target_plan": target_plan,
                        "confirmation_gate": {
                            "expected_closed_bar": str(
                                (int(time.time() * 1000) // (5 * 60 * 1000) - 1)
                                * (5 * 60 * 1000)
                            ),
                            "raw_bar_id": confirmation.get("bar_id"),
                            "consumed_bar_id": initial_confirmation_bar,
                            "source": "INITIAL_SCANNER_CONFIRMATION",
                        },
                    },
                )

                if confirmation.get("passed"):
                    if symbol in state_manager.get_virtual_orders():
                        logger.warning(
                            f"[{symbol}] تأكيد Scanner صالح لكن يوجد أمر مراقبة لنفس الزوج؛ "
                            "يُترك التنفيذ للمراقب لتجنب السباق."
                        )
                        continue
                    coin_data["entry_confirmation"] = confirmation
                    trade_executed = execute_and_protect_trade(
                        symbol, final_decision, amount, current_price, trade_leverage,
                        trade_risk_pct, atr_val, coin_data, sentiment, bars_df, ticker,
                        ta_trend, client, risk_manager, trailing_manager, state_manager,
                        notifier, active_symbols, scan_results, invalidation_level,
                        ta_engine=ta_engine, trade_id=trade_id,
                    )
                    if trade_executed and len(active_symbols) >= max_trades:
                        logger.info("تم الوصول للحد الأقصى للصفقات المتزامنة. إيقاف المسح الحالي.")
                        break
                    if not trade_executed:
                        audit_decision_event(
                            scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                            stage="EXECUTION", status="FAILED", trade_id=trade_id,
                        )
                    continue

                if symbol in state_manager.get_virtual_orders():
                    pending_result = dict(coin_data)
                    pending_result.update({
                        "decision": "⏳ V-LIMIT",
                        "reason": "أمر تأكيد 5m موجود مسبقاً",
                        "price": current_price,
                        "_trade_id": trade_id,
                    })
                    scan_results.append(pending_result)
                    continue

                if profile_key in ("institutional", "smc_strict", "strict"):
                    max_age_minutes = getattr(Config, "VIRTUAL_ORDER_MAX_AGE_MINUTES_STRICT", 30)
                elif profile_key in ("smc_balanced", "smc_fallback", "medium"):
                    max_age_minutes = getattr(Config, "VIRTUAL_ORDER_MAX_AGE_MINUTES_MEDIUM", 20)
                else:
                    max_age_minutes = getattr(Config, "VIRTUAL_ORDER_MAX_AGE_MINUTES_RELAXED", 15)

                created_at = datetime.now(timezone.utc)
                expires_at = created_at + timedelta(minutes=max_age_minutes)
                initial_touch_guard = None
                confirmation_evidence = dict(confirmation.get("evidence") or {})
                if (
                    confirmation_evidence.get("candle_touched_zone")
                    and confirmation_evidence.get("current_in_zone")
                ):
                    initial_executable_ticker = fetch_executable_contract_ticker(
                        client, symbol, ticker=ticker
                    )
                    initial_touch_guard = evaluate_executable_entry_price(
                        side=final_decision,
                        ticker=initial_executable_ticker,
                        reference_price=current_price,
                        optimal_entry=optimal_entry,
                        atr_val=atr_val,
                    )
                    initial_touch_guard["executable_price_source"] = (
                        initial_executable_ticker.get("_executable_price_source")
                    )
                    initial_touch_guard["closed_bar_id"] = confirmation.get("bar_id")
                tp_steps = []
                if getattr(Config, "TP_LADDER_ENABLED", True) and temp_sl:
                    is_bf = (
                        winning_strategy == "Macro Bottom Fishing & Trend Rider Strategy"
                        or bool(ta_details.get("bottom_fishing_active", False))
                    )
                    tp_steps = risk_manager.get_tp_steps(
                        final_decision,
                        float(optimal_entry),
                        float(temp_sl),
                        tp3_override=float(temp_tp),
                        is_bottom_fishing=is_bf,
                    )

                virtual_order_id = f"vo_{uuid.uuid4().hex[:16]}"
                virtual_order = {
                    "virtual_order_id": virtual_order_id,
                    "trade_id": trade_id,
                    "scan_id": scan_id,
                    "decision_id": decision_id,
                    "side": final_decision,
                    "entry_price": float(optimal_entry),
                    "optimal_entry": float(optimal_entry),
                    "sl": float(temp_sl),
                    "tp": float(temp_tp),
                    "invalidation_level": float(invalidation_level),
                    "ob_mid": float(ta_details.get("ob_mid", optimal_entry)),
                    "amount": float(amount),
                    "atr": float(atr_val),
                    "support": float(coin_data.get("support", 0)),
                    "resistance": float(coin_data.get("resistance", 0)),
                    "trade_leverage": int(trade_leverage),
                    "trade_risk_pct": float(trade_risk_pct),
                    "created_at": created_at.isoformat(),
                    "created_at_ms": int(created_at.timestamp() * 1000),
                    "expires_at": expires_at.isoformat(),
                    "max_age_minutes": max_age_minutes,
                    "filter_profile": profile_key,
                    "filter_profile_snapshot": filter_profile,
                    "cancel_if_tp_hit_first": True,
                    "cancel_if_invalidated": True,
                    "cancel_if_trend_flips": True,
                    "created_price": float(current_price),
                    "created_reason": coin_data.get("reason", ""),
                    "created_ta_signal": ta_trend,
                    "sentiment_label": sentiment.get("label", "neutral") if sentiment else "neutral",
                    "sentiment_score": float(
                        sentiment.get("score") or sentiment.get("galaxy_score") or 0.0
                    ) if sentiment else 0.0,
                    "ta_trend": ta_trend,
                    "market_levels": coin_data.get("market_levels", {}),
                    "tp_steps": tp_steps,
                    "tp_ladder_enabled": bool(tp_steps),
                    "setup_thesis_id": coin_data.get("_thesis_id"),
                    "setup_context": coin_data.get("_ta_context", {}),
                    "target_plan": target_plan,
                    "planned_rr": float(planned_rr),
                    "last_confirmation_bar": initial_confirmation_bar,
                    "last_confirmation_attempt_slot": str(
                        (int(time.time() * 1000) // (5 * 60 * 1000) - 1)
                        * (5 * 60 * 1000)
                    ),
                    "last_confirmation": confirmation,
                    "invalidation_state": "VALID",
                }
                if initial_touch_guard and initial_touch_guard.get("passed"):
                    confirmation_window = max(
                        7,
                        int(getattr(Config, "ENTRY_CONFIRMATION_WINDOW_MINUTES", 12)),
                    )
                    virtual_order.update({
                        "entry_zone_touched_at_ms": int(created_at.timestamp() * 1000),
                        "entry_zone_touched_price": float(
                            initial_touch_guard["executable_price"]
                        ),
                        "entry_zone_touch_kind": "INITIAL_CLOSED_5M_AND_LIVE_EXECUTABLE",
                        "entry_zone_touch_price_source": initial_touch_guard.get(
                            "executable_price_source"
                        ),
                        "confirmation_deadline_ms": int(
                            created_at.timestamp() * 1000
                            + confirmation_window * 60 * 1000
                        ),
                    })
                if not state_manager.save_virtual_order(symbol, virtual_order):
                    logger.critical(
                        f"[{symbol}] لم يُنشأ أمر التأكيد: تعذر حفظه كتابةً على القرص."
                    )
                    audit_decision_event(
                        scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                        stage="SNIPER", status="REJECTED_PERSISTENCE",
                        trade_id=trade_id, durable=True,
                    )
                    continue

                paired_shadow_registered = register_external_smc_thesis({
                    "symbol": symbol,
                    "side": final_decision,
                    "source_thesis_id": coin_data.get("_thesis_id"),
                    "invalidation_level": float(invalidation_level),
                    "market_regime": (coin_data.get("_ta_context", {}) or {}).get(
                        "market_regime", {}
                    ),
                    "context_15m": coin_data.get("_ta_context", {}),
                    "source_snapshot": {
                        "scan_id": scan_id,
                        "decision_id": decision_id,
                        "virtual_order_id": virtual_order_id,
                        "original_entry": float(optimal_entry),
                        "original_sl": float(temp_sl),
                        "original_tp": float(temp_tp),
                        "original_planned_rr": float(planned_rr),
                    },
                })
                audit_decision_event(
                    scan_id=scan_id,
                    decision_id=decision_id,
                    signal_id=decision_id,
                    setup_id=coin_data.get("_thesis_id"),
                    thesis_id=coin_data.get("_thesis_id"),
                    virtual_order_id=virtual_order_id,
                    trade_id=trade_id,
                    symbol=symbol,
                    stage="MOMENTUM_CONTINUATION",
                    status=(
                        "EXTERNAL_SMC_QUEUED"
                        if paired_shadow_registered
                        else "EXTERNAL_SMC_REGISTRATION_REJECTED"
                    ),
                    details={
                        "execution_enabled": False,
                        "source": "SMC_RR_PASS_PAIRED_SHADOW",
                    },
                )

                if initial_touch_guard:
                    audit_decision_event(
                        scan_id=scan_id,
                        decision_id=decision_id,
                        setup_id=coin_data.get("_thesis_id"),
                        thesis_id=coin_data.get("_thesis_id"),
                        virtual_order_id=virtual_order_id,
                        trade_id=trade_id,
                        symbol=symbol,
                        stage="ENTRY_PRICE_BASIS",
                        status=(
                            "ALIGNED" if initial_touch_guard.get("passed")
                            else "INITIAL_TOUCH_NOT_EXECUTABLE"
                        ),
                        details=initial_touch_guard,
                    )
                if initial_touch_guard and initial_touch_guard.get("passed"):
                    audit_decision_event(
                        scan_id=scan_id,
                        decision_id=decision_id,
                        signal_id=decision_id,
                        setup_id=coin_data.get("_thesis_id"),
                        thesis_id=coin_data.get("_thesis_id"),
                        virtual_order_id=virtual_order_id,
                        trade_id=trade_id,
                        symbol=symbol,
                        stage="ENTRY_ZONE_TOUCH",
                        status="TOUCHED",
                        details={
                            "price": initial_touch_guard.get("executable_price"),
                            "mark_price": current_price,
                            "optimal_entry": float(optimal_entry),
                            "tolerance": initial_touch_guard.get("entry_tolerance"),
                            "confirmation_window_minutes": confirmation_window,
                            "touch_kind": virtual_order["entry_zone_touch_kind"],
                            "closed_bar_id": confirmation.get("bar_id"),
                        },
                        durable=True,
                    )

                logger.info(
                    f"🎯 [EntryWatch] {symbol}: الإعداد صالح لكن تأكيد 5m غير مكتمل "
                    f"({confirmation.get('reason')}). صالح حتى {virtual_order['expires_at']}."
                )
                audit_decision_event(
                    scan_id=scan_id, decision_id=decision_id, symbol=symbol,
                    signal_id=decision_id,
                    setup_id=coin_data.get("_thesis_id"),
                    thesis_id=coin_data.get("_thesis_id"),
                    virtual_order_id=virtual_order_id,
                    stage="SNIPER", status="PENDING", trade_id=trade_id,
                    details={
                        "optimal_entry": float(optimal_entry),
                        "expires_at": virtual_order["expires_at"],
                        "setup_thesis_id": coin_data.get("_thesis_id"),
                        "initial_confirmation": confirmation,
                    },
                    durable=True,
                )
                try:
                    from utils.forensics_logger import update_signal_outcome
                    update_signal_outcome(trade_id, "PENDING_5M_CONFIRMATION", 0.0)
                except Exception:
                    pass
                pending_result = dict(coin_data)
                pending_result.update({
                    "decision": "⏳ V-LIMIT",
                    "reason": f"انتظار تأكيد 5m: {confirmation.get('reason')}",
                    "price": current_price,
                    "_trade_id": trade_id,
                    "entry_confirmation": confirmation,
                    "target_plan": target_plan,
                })
                scan_results.append(pending_result)
                continue
                        
            else:
                logger.warning(f"تعذر الحصول على السعر الحالي للزوج {symbol}")
                coin_data.update({
                    'decision': '🔴 REJECT',
                    'reason': 'فشل الحصول على السعر',
                    'price': 0,
                    'rejection_stage': 'DATA_QUALITY',
                })
                scan_results.append(coin_data)
                if trade_id:
                    try:
                        from utils.forensics_logger import update_signal_outcome
                        update_signal_outcome(trade_id, "FAILED_GET_PRICE", 0.0)
                    except Exception:
                        pass
                    
        else:
            coin_data['decision'] = '🟡 HOLD'
            if sentiment and sentiment.get('source') == 'lunarcrush':
                coin_data['reason'] = f"فني:{ta_trend} | 🌙 Galaxy={sentiment.get('galaxy_score','?')} | Sentiment={sentiment.get('raw_sentiment_pct','?')}%"
            elif sentiment:
                coin_data['reason'] = f"فني:{ta_trend} | أخبار:{sentiment.get('label','?')}"
            else:
                coin_data['reason'] = f"فني:{ta_trend} | لا بيانات اجتماعية"
            scan_results.append(coin_data)
                
    logger.info("=========================================")
    logger.info("تم إنهاء جولة مسح السوق.")

    # طبقة حالة/شرح موحدة لا تملك أي صلاحية تنفيذ. النسبة تقيس مقدار
    # التقدم داخل pipeline فقط، ولا تُستخدم كاحتمال ربح أو كبوابة دخول.
    # ترتيب أولي رخيص، ثم إكمال سياق 4H لعدد محدود من أفضل المرشحين فقط؛
    # هذا يمنع إضافة عشرات طلبات 4H إلى كل جولة.
    preliminary_results, _ = enrich_scan_results(
        scan_results,
        score_weights=getattr(Config, "CANDIDATE_SCORE_WEIGHTS", None),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    context_budget = 8
    for candidate_row in sorted(
        preliminary_results,
        key=lambda item: int(
            (item.get("candidate_assessment") or {}).get("candidate_rank") or 9999
        ),
    ):
        assessment = candidate_row.get("candidate_assessment") or {}
        if context_budget <= 0:
            break
        if not assessment.get("is_candidate") or candidate_row.get("context_4h"):
            continue
        candidate_symbol = str(candidate_row.get("symbol") or "")
        if not candidate_symbol:
            continue
        context = ta_engine.get_market_context_4h(candidate_symbol, client)
        if isinstance(context, dict):
            candidate_row["context_4h"] = context
            if isinstance(candidate_row.get("_ta_context"), dict):
                candidate_row["_ta_context"]["context_4h"] = context
        context_budget -= 1

    scan_results, candidate_lifecycle = enrich_scan_results(
        preliminary_results,
        previous_lifecycle=state_manager.get("candidate_lifecycle", {}),
        timestamp=datetime.now(timezone.utc).isoformat(),
        score_weights=getattr(Config, "CANDIDATE_SCORE_WEIGHTS", None),
    )
    state_manager.set("candidate_lifecycle", candidate_lifecycle)

    # لقطة طرفية موحدة لكل نتيجة ظهرت في تقرير الجولة. الأحداث التفصيلية أعلاه
    # تبقى المصدر لمعرفة أي فلتر اتخذ القرار.
    for result in scan_results:
        coin = result.get("coin")
        context = audit_context_by_coin.get(coin)
        if not context:
            continue
        result_decision_id, result_symbol = context
        raw_status = str(result.get("decision", "UNKNOWN"))
        event_stage, final_status = classify_scan_result_event(raw_status)
        assessment = result.get("candidate_assessment") or {}
        audit_decision_event(
            scan_id=scan_id,
            decision_id=result_decision_id,
            symbol=result_symbol,
            stage=event_stage,
            status=final_status,
            trade_id=result.get("_trade_id"),
            details={
                "display_status": raw_status,
                "reason": result.get("reason"),
                "candidate_assessment": assessment,
            },
        )
        if assessment.get("is_candidate"):
            audit_decision_event(
                scan_id=scan_id,
                decision_id=result_decision_id,
                symbol=result_symbol,
                stage="CANDIDATE_STATE",
                status=assessment.get("status", "UNKNOWN"),
                trade_id=result.get("_trade_id"),
                details=assessment,
            )

    # --- حفظ نتائج المسح في state للـ Dashboard ---
    scan_snapshot = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_scanned": len(scan_results),
        "usdt_total": round(usdt_total, 2),
        "usdt_free": round(usdt_free, 2),
        "trade_executed": trade_executed,
        "results": [
            {
                "coin":     r.get("coin", ""),
                "decision": r.get("decision", ""),
                "reason":   r.get("reason", ""),
                "price":    float(r.get("price", 0) or 0),
                "rsi":      float(r.get("rsi", 0) or 0),
                "side":     r.get("side", ""),
                "technical_signal": r.get("technical_signal", ""),
                "htf_trend": r.get("htf_trend", ""),
                "market_regime": r.get("market_regime", ""),
                "rejection_stage": r.get("rejection_stage", ""),
                "target_plan": r.get("target_plan", {}),
                "quality_audit": r.get("quality_audit", {}),
                "entry_confirmation": r.get("entry_confirmation", {}),
                "derivatives_result": r.get("derivatives_result", {}),
                "candidate_assessment": r.get("candidate_assessment", {}),
            }
            for r in scan_results
        ],
        "candidate_count": candidate_lifecycle.get("candidate_count", 0),
        "candidate_ranking_method": candidate_lifecycle.get("ranking_method"),
        "candidate_metrics": candidate_lifecycle.get("metrics", {}),
    }
    state_manager.set("latest_scan", scan_snapshot)

    # --- إرسال رسالة قصيرة مع زر Dashboard ---
    executed_count = sum(1 for r in scan_results if "BUY" in r.get("decision","") or "SELL" in r.get("decision",""))
    _finish_scan(
        "COMPLETED",
        target_symbols=len(target_symbols),
        terminal_results=len(scan_results),
        missing_terminal_results=max(0, len(target_symbols) - len(scan_results)),
        trade_executed=bool(trade_executed),
    )
    # Demo execution must not make the existing Telegram interface silent.
    # The summary remains informational; only USER_VISIBLE_TRADE_PROPOSAL can
    # reach the Demo order route.
    notifier.send_scan_summary(
        len(scan_results),
        executed_count,
        trade_executed,
        scan_results=scan_results,
    )
    return usdt_total

def _parse_dt_safe(value):
    """Parse persisted timestamps and always return an aware UTC datetime."""
    try:
        if value in (None, ""):
            return None
        if isinstance(value, (float, int)):
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            raw = str(value).strip()
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        if parsed.tzinfo is None:
            # Legacy Railway state was written without an offset while the
            # container itself used UTC. Preserve that meaning explicitly.
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None

def get_current_price_from_ticker(client, symbol):
    try:
        ticker = client.exchange.fetch_ticker(symbol)
        return float(
            ticker.get("last")
            or ticker.get("close")
            or ticker.get("bid")
            or ticker.get("ask")
            or 0.0
        )
    except Exception as e:
        logger.warning(f"[VirtualOrderGuard] تعذر جلب السعر الحالي لـ {symbol}: {e}")
        return 0.0


def _closed_bar_end_utc(row, timeframe_minutes=15):
    """Return a normalized close time for REST or WS candle rows."""
    try:
        value = row.get("timestamp", row.get("time", row.name))
        if isinstance(value, (int, float, np.integer, np.floating)):
            unit = "ms" if abs(float(value)) > 10_000_000_000 else "s"
            opened = pd.to_datetime(value, unit=unit, utc=True)
        else:
            opened = pd.Timestamp(value)
            if opened.tzinfo is None:
                opened = opened.tz_localize("UTC")
            else:
                opened = opened.tz_convert("UTC")
        return opened.to_pydatetime() + timedelta(minutes=timeframe_minutes)
    except Exception:
        return None

def check_virtual_order_cancellation(
    client,
    symbol,
    v_order,
    current_price=None,
    check_structure=False,
):
    """
    يفحص هل الأمر المعلق ما زال صالحًا أم يجب إلغاؤه.
    يرجع:
    - None إذا الأمر صالح.
    - نص سبب الإلغاء إذا انتهت صلاحيته.
    """
    now = datetime.now(timezone.utc)

    side = str(v_order.get("side", "")).lower()
    if side in ["long"]:
        side = "buy"
    elif side in ["short"]:
        side = "sell"

    entry = float(v_order.get("entry_price") or v_order.get("optimal_entry") or 0.0)
    sl = float(v_order.get("sl") or v_order.get("invalidation_level") or 0.0)
    tp = float(v_order.get("tp") or 0.0)
    invalidation = float(v_order.get("invalidation_level") or sl or 0.0)

    # 1. انتهاء الوقت
    confirmation_deadline_ms = int(v_order.get("confirmation_deadline_ms") or 0)
    structural_deadline_ms = int(
        v_order.get("structural_resolution_deadline_ms") or 0
    )
    active_deadline_ms = (
        structural_deadline_ms
        if str(v_order.get("invalidation_state") or "").upper() == "SWEEP_PENDING"
        else confirmation_deadline_ms
    )
    if active_deadline_ms:
        if int(now.timestamp() * 1000) > active_deadline_ms:
            return (
                "انتهت نافذة حسم sweep على 15m."
                if structural_deadline_ms == active_deadline_ms
                else "انتهت نافذة تأكيد 5m بعد لمس منطقة الدخول."
            )
    else:
        expires_at = _parse_dt_safe(v_order.get("expires_at"))
        if expires_at:
            if now > expires_at:
                return (
                    "انتهت صلاحية اكتشاف الإعداد قبل لمس منطقة الدخول. "
                    f"expires_at={v_order.get('expires_at')}"
                )

    # السعر اللحظي من WebSocket كافٍ للحارس السريع. كان الكود السابق يجلب
    # 1m و250 شمعة 15m لكل أمر كل ثانيتين، فيؤخر مراقبة المراكز الحقيقية.
    current_price = float(
        current_price or get_current_price_from_ticker(client, symbol) or 0.0
    )
    recent_high = current_price
    recent_low = current_price
    last_close = current_price

    # 3. إذا ضرب الهدف المتوقع قبل أن يفعل الدخول، فالفرصة انتهت
    # استخراج أول هدف من tp_steps إذا كان tp=0
    if tp <= 0:
        _tp_steps = v_order.get("tp_steps") or []
        if _tp_steps:
            _first = _tp_steps[0]
            tp = float(_first.get("price", 0) if isinstance(_first, dict) else _first)

    if getattr(Config, "VIRTUAL_ORDER_CANCEL_IF_TP_HIT_FIRST", True) and tp > 0:
        if side == "buy" and recent_high >= tp:
            return f"تم إلغاء الأمر لأن السعر ضرب هدف الشراء قبل العودة للدخول. high={recent_high}, tp={tp}"
        if side == "sell" and recent_low <= tp:
            return f"تم إلغاء الأمر لأن السعر ضرب هدف البيع قبل العودة للدخول. low={recent_low}, tp={tp}"

    # 4. لمس invalidation لحظياً هو sweep محتمل، لا موت تلقائي لفرضية 15m.
    # الإلغاء الصلب لا يحدث إلا بعد إغلاق 15m لاحق خلف المستوى.
    if getattr(Config, "VIRTUAL_ORDER_CANCEL_IF_INVALIDATED", True) and invalidation > 0:
        prior_state = str(v_order.get("invalidation_state") or "VALID").upper()
        classification = classify_virtual_invalidation(
            side=side,
            current_price=current_price,
            invalidation_level=invalidation,
            prior_state=prior_state,
        )
        if classification == "SWEEP_PENDING" and prior_state != "SWEEP_PENDING":
            v_order["invalidation_state"] = "SWEEP_PENDING"
            breach_now = datetime.now(timezone.utc)
            breach_ms = int(breach_now.timestamp() * 1000)
            interval_ms = 15 * 60 * 1000
            expected_close_ms = (breach_ms // interval_ms + 1) * interval_ms
            retry_delays = tuple(
                getattr(
                    Config,
                    "VIRTUAL_ORDER_STRUCTURE_RETRY_DELAYS_SECONDS",
                    (3, 10, 25, 50, 90),
                )
            ) or (3, 10, 25, 50, 90)
            v_order["soft_invalidation_at"] = breach_now.isoformat()
            v_order["soft_invalidation_price"] = current_price
            v_order["soft_invalidation_extreme"] = current_price
            breach_distance = abs(current_price - invalidation)
            v_order["soft_invalidation_breach_pct"] = (
                breach_distance / invalidation if invalidation > 0 else None
            )
            atr_value = float(v_order.get("atr") or 0.0)
            v_order["soft_invalidation_breach_atr"] = (
                breach_distance / atr_value if atr_value > 0 else None
            )
            v_order["structure_expected_close_ms"] = expected_close_ms
            v_order["structure_retry_count"] = 0
            v_order["next_structure_check_ms"] = (
                expected_close_ms + int(retry_delays[0]) * 1000
            )
            # نافذة حسم مرتبطة بموعد الإغلاق المتوقع، لا polling مفتوحاً.
            v_order["structural_resolution_deadline_ms"] = int(
                expected_close_ms + (int(retry_delays[-1]) + 30) * 1000
            )
        elif prior_state == "SWEEP_PENDING":
            old_extreme = float(
                v_order.get("soft_invalidation_extreme") or current_price
            )
            v_order["soft_invalidation_extreme"] = (
                min(old_extreme, current_price)
                if side == "buy"
                else max(old_extreme, current_price)
            )

    # 5. إذا ابتعد السعر كثيرًا عن الدخول ولم يعد قريبًا، فالفرصة غالبًا فاتت
    max_dist = float(getattr(Config, "VIRTUAL_ORDER_MAX_DISTANCE_FROM_ENTRY_PCT", 0.04))
    if entry > 0 and last_close > 0:
        distance = abs(last_close - entry) / entry
        # نُلغي فقط إذا ابتعد في الاتجاه الخاطئ (باتجاه الـ SL لا الهدف)
        moved_away_wrong_direction = (
            (side == "buy"  and last_close < entry * (1 - max_dist)) or
            (side == "sell" and last_close > entry * (1 + max_dist))
        )
        if moved_away_wrong_direction:
            return f"تم إلغاء الأمر لأن السعر ابتعد في الاتجاه الخاطئ. distance={distance:.2%}"

    # 6. حسم sweep وفحص تغير الترند على إغلاق 15m فقط. لا نجلب REST فور
    # حدوث الاختراق داخل الشمعة؛ لا يمكن أن يوجد حينها إغلاق post-breach.
    if str(v_order.get("invalidation_state") or "").upper() == "SWEEP_PENDING":
        due_ms = int(v_order.get("next_structure_check_ms") or 0)
        if due_ms and int(now.timestamp() * 1000) < due_ms:
            check_structure = False
    if check_structure and (
        str(v_order.get("invalidation_state") or "").upper() == "SWEEP_PENDING"
        or getattr(Config, "VIRTUAL_ORDER_CANCEL_IF_TREND_FLIPS", True)
    ):
        try:
            v_order["structure_check_attempt_serial"] = int(
                v_order.get("structure_check_attempt_serial") or 0
            ) + 1
            bars_15m = client.fetch_ohlcv(
                symbol,
                timeframe="15m",
                limit=Config.DECISION_CANDLE_HISTORY
            )

            if bars_15m is not None and not bars_15m.empty and len(bars_15m) >= 210:
                closed_bars = bars_15m.iloc[:-1]
                last_closed = closed_bars.iloc[-1]
                ema_50 = closed_bars["close"].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = closed_bars["close"].ewm(span=200, adjust=False).mean().iloc[-1]
                closed_at = _closed_bar_end_utc(last_closed, 15)
                v_order["trend_check_evidence"] = {
                    "source": "BINANCE_USDM_FUTURES_REST",
                    "timeframe": "15m",
                    "requested_candles": Config.DECISION_CANDLE_HISTORY,
                    "analysis_candle_count": len(bars_15m),
                    "closed_candle_count": len(closed_bars),
                    "closed_bar_end": closed_at.isoformat() if closed_at else None,
                    "close": float(last_closed["close"]),
                    "ema_50": float(ema_50),
                    "ema_200": float(ema_200),
                }

                if str(v_order.get("invalidation_state") or "").upper() == "SWEEP_PENDING":
                    soft_at = _parse_dt_safe(v_order.get("soft_invalidation_at"))
                    if soft_at and soft_at.tzinfo is None:
                        soft_at = soft_at.replace(tzinfo=timezone.utc)
                    closed_at = _closed_bar_end_utc(last_closed, 15)
                    closed_after_breach = bool(
                        soft_at and closed_at and closed_at > soft_at
                    )
                    structural_open_time = last_closed.get(
                        "timestamp", last_closed.get("time")
                    )
                    if structural_open_time is not None:
                        isoformat = getattr(structural_open_time, "isoformat", None)
                        structural_open_time = (
                            isoformat() if callable(isoformat)
                            else str(structural_open_time)
                        )
                    v_order["structural_close_evidence"] = {
                        "source": "BINANCE_USDM_FUTURES_REST",
                        "timeframe": "15m",
                        "open_time": structural_open_time,
                        "close_time": closed_at.isoformat() if closed_at else None,
                        "closed_after_breach": closed_after_breach,
                        "open": last_closed.get("open"), "high": last_closed.get("high"),
                        "low": last_closed.get("low"), "close": last_closed.get("close"),
                        "volume": last_closed.get("volume"),
                    }
                    classification = classify_virtual_invalidation(
                        side=side,
                        current_price=current_price,
                        invalidation_level=invalidation,
                        prior_state="SWEEP_PENDING",
                        closed_price=float(last_closed.get("close") or 0),
                        closed_after_breach=closed_after_breach,
                    )
                    if classification == "HARD_INVALIDATED":
                        v_order["invalidation_state"] = classification
                        return (
                            "HARD_STRUCTURAL_INVALIDATION: إغلاق 15m خلف المستوى. "
                            f"close={float(last_closed.get('close') or 0)}, "
                            f"invalidation={invalidation}"
                        )
                    if classification == "RECLAIM_CONFIRMED":
                        v_order["invalidation_state"] = classification
                        v_order["reclaim_confirmed_at"] = closed_at.isoformat()
                        v_order["reclaim_close"] = float(last_closed.get("close") or 0)
                        v_order.pop("structural_resolution_deadline_ms", None)
                        v_order["confirmation_deadline_ms"] = int(
                            now.timestamp() * 1000
                        ) + max(
                            7,
                            int(getattr(Config, "ENTRY_CONFIRMATION_WINDOW_MINUTES", 12)),
                        ) * 60 * 1000

                if (
                    getattr(Config, "VIRTUAL_ORDER_CANCEL_IF_TREND_FLIPS", True)
                    and side == "buy" and ema_50 < ema_200
                ):
                    return f"تم إلغاء أمر الشراء لأن ترند 15m أصبح هابطًا. EMA50 < EMA200"

                if (
                    getattr(Config, "VIRTUAL_ORDER_CANCEL_IF_TREND_FLIPS", True)
                    and side == "sell" and ema_50 > ema_200
                ):
                    return f"تم إلغاء أمر البيع لأن ترند 15m أصبح صاعدًا. EMA50 > EMA200"

        except Exception as e:
            logger.warning(f"[VirtualOrderGuard] فشل فحص الترند للأمر المعلق {symbol}: {e}")

    return None

def recover_pending_order_intents(client, state_manager, notifier, trailing_manager, risk_manager):
    """استرداد نوايا قديمة دون مزاحمة التنفيذ الجاري داخل العملية نفسها."""
    acquired = _execution_lock.acquire(blocking=False)
    if not acquired:
        logger.debug("[OrderIntent] التنفيذ ما زال يملك البوابة؛ تأجيل الاسترداد.")
        return
    try:
        _recover_pending_order_intents_locked(
            client, state_manager, notifier, trailing_manager, risk_manager
        )
    finally:
        _execution_lock.release()


def _recover_pending_order_intents_locked(
    client, state_manager, notifier, trailing_manager, risk_manager
):
    """الجزء الداخلي؛ لا يُستدعى إلا أثناء امتلاك _execution_lock."""
    intents = state_manager.get_order_intents()
    if not intents:
        return

    now = time.time()
    recovery_grace = float(
        getattr(Config, "ORDER_INTENT_RECOVERY_GRACE_SECONDS", 30)
    )
    for symbol, intent in intents.items():
        try:
            recovery_audit = {
                "symbol": symbol,
                "trade_id": intent.get("trade_id"),
                "position_id": intent.get("trade_id"),
                "setup_id": intent.get("setup_thesis_id"),
                "thesis_id": intent.get("setup_thesis_id"),
            }
            audit_decision_event(
                **recovery_audit, stage="ORDER_INTENT_RECOVERY", status="STARTED",
                details={"intent_status": intent.get("status"),
                         "created_at": intent.get("created_at")},
            )
            if now < float(intent.get("retry_after", 0) or 0):
                continue

            intent_status = str(intent.get("status", "SUBMITTING"))
            updated_at = float(
                intent.get("updated_at") or intent.get("created_at") or now
            )
            same_live_owner = (
                intent.get("owner_instance_id") == _PROCESS_INSTANCE_ID
            )
            if (
                same_live_owner
                and intent_status in {
                    "SUBMITTING", "ENTRY_CONFIRMED", "PROTECTING", "PROTECTED"
                }
                and now - updated_at < recovery_grace
            ):
                continue

            if not state_manager.transition_order_intent(
                symbol,
                intent_status,
                "RECOVERING",
                updates={"recovery_owner_instance_id": _PROCESS_INSTANCE_ID},
            ):
                continue
            intent = state_manager.get_order_intent(symbol)

            position_side = str(intent.get("position_side", "") or "").upper()
            status = client.get_position_status(
                symbol,
                position_side=position_side if getattr(client, '_is_hedge_mode', False) else None,
            )
            if status.get("status") == "UNKNOWN":
                state_manager.transition_order_intent(
                    symbol,
                    "RECOVERING",
                    "RECOVERY_WAIT_POSITION",
                    updates={"retry_after": now + 30},
                )
                audit_decision_event(
                    **recovery_audit, stage="ORDER_INTENT_RECOVERY",
                    status="WAIT_POSITION_UNKNOWN", durable=True,
                )
                continue

            if status.get("status") == "CLOSED":
                age = now - float(intent.get("created_at", now) or now)
                resolver = getattr(client, "resolve_order_by_client_id", None)
                resolution = (
                    resolver(symbol, intent.get("trade_id"))
                    if callable(resolver)
                    else {"status": "UNKNOWN", "reason": "RESOLVER_UNAVAILABLE"}
                )
                if not isinstance(resolution, dict):
                    resolution = {
                        "status": "UNKNOWN", "reason": "INVALID_RESOLVER_RESULT"
                    }
                resolution_status = str(
                    resolution.get("status") or "UNKNOWN"
                ).upper()
                resolved_order = resolution.get("order")
                parsed_order = (
                    parse_entry_order_execution(resolved_order)
                    if resolution_status == "FOUND"
                    else None
                )

                if parsed_order and parsed_order["executed_qty"] > 0:
                    logger.critical(
                        f"[OrderIntent] وُجد fill سابق لـ {symbol} لكن المركز "
                        "أصبح صفراً قبل الاسترداد؛ أرشفة الدليل بدلاً من حذفه."
                    )
                    state_manager.archive_order_intent(
                        symbol,
                        "ENTRY_FILLED_POSITION_CLOSED",
                        proofs={
                            "order_resolution": resolution_status,
                            "order": parsed_order,
                            "position_status": "CLOSED",
                        },
                    )
                    audit_decision_event(
                        **recovery_audit, stage="ORDER_INTENT_RECOVERY",
                        status="FILLED_ORDER_POSITION_ALREADY_CLOSED",
                        details={"age_seconds": age, "order": parsed_order},
                        durable=True,
                    )
                    continue

                definitive_no_fill = bool(
                    parsed_order
                    and parsed_order["state"] == "TERMINAL_NO_FILL"
                )
                definitive_not_found = resolution_status == "NOT_FOUND" and age >= 120
                if definitive_no_fill or definitive_not_found:
                    terminal_status = (
                        "ENTRY_TERMINAL_NO_FILL"
                        if definitive_no_fill
                        else "ENTRY_NOT_FOUND_NO_POSITION"
                    )
                    state_manager.archive_order_intent(
                        symbol,
                        terminal_status,
                        proofs={
                            "order_resolution": resolution_status,
                            "order": parsed_order or {},
                            "position_status": "CLOSED",
                            "age_seconds": age,
                        },
                    )
                    audit_decision_event(
                        **recovery_audit, stage="ORDER_INTENT_RECOVERY",
                        status=terminal_status,
                        details={"age_seconds": age, "order": parsed_order or {}},
                        durable=True,
                    )
                else:
                    # UNKNOWN، أو ACK/NEW، أو NOT_FOUND حديث: لا يثبت أن الأمر
                    # لم يُنفذ. نحتفظ بالنية ونستخدم backoff بدلاً من إعادة إرسال.
                    state_manager.transition_order_intent(
                        symbol,
                        "RECOVERING",
                        "RECOVERY_WAIT_ORDER_RESOLUTION",
                        updates={
                            "retry_after": now + 30,
                            "last_order_resolution": resolution_status,
                            "last_order_resolution_reason": resolution.get("reason"),
                        },
                    )
                    audit_decision_event(
                        **recovery_audit, stage="ORDER_INTENT_RECOVERY",
                        status="WAIT_ORDER_RESOLUTION",
                        details={
                            "age_seconds": age,
                            "order_resolution": resolution_status,
                            "reason": resolution.get("reason"),
                        },
                        durable=True,
                    )
                continue

            amount = float(status.get("amount") or 0)
            rows = status.get("positions") or []
            actual_entry = float(
                (rows[0].get("entry_price") if rows else 0)
                or intent.get("expected_entry")
                or 0
            )
            if amount <= 0 or actual_entry <= 0:
                state_manager.transition_order_intent(
                    symbol,
                    "RECOVERING",
                    "RECOVERY_WAIT_POSITION_DATA",
                    updates={"retry_after": now + 30},
                )
                continue

            side = str(intent.get("side", "BUY")).lower()
            expected_entry = float(intent.get("expected_entry") or actual_entry)
            planned_sl = float(intent.get("sl") or 0)
            planned_tp = float(intent.get("tp") or 0)
            sl = planned_sl
            tp = planned_tp
            risk_distance = (
                actual_entry - sl if side == "buy" else sl - actual_entry
            )
            reward_distance = (
                tp - actual_entry if side == "buy" else actual_entry - tp
            )
            actual_rr = reward_distance / risk_distance if risk_distance > 0 else 0.0
            min_rr = float(intent.get("min_rr", 1.5) or 1.5)
            if (
                planned_sl <= 0
                or planned_tp <= 0
                or risk_distance <= 0
                or reward_distance <= 0
                or actual_rr < min_rr
            ):
                logger.critical(
                    f"[OrderIntent] استُعيد مركز {symbol} لكن خطته المخزنة غير "
                    f"صالحة بعد التعبئة (RR={actual_rr:.2f} < {min_rr:.2f})؛ "
                    "محاولة إغلاق طارئ دون اختلاق 4R."
                )
                close_ok = client.close_position(symbol, side, amount)
                if close_ok:
                    state_manager.remove_order_intent(symbol)
                    notifier.send_message(
                        f"⚠️ أُغلق مركز <code>{symbol}</code> المسترد لأن خطة "
                        "SL/TP لم تعد صالحة بعد timeout."
                    )
                else:
                    intent["status"] = "UNPROTECTED_INVALID_PLAN_CLOSE_UNCONFIRMED"
                    intent["retry_after"] = now + 15
                    intent["updated_at"] = now
                    state_manager.save_order_intent(symbol, intent)
                    state_manager.add_active_symbol(symbol)
                    state_manager.save_entry_metadata(symbol, {
                        **intent,
                        "entry_price": actual_entry,
                        "amount": amount,
                        "original_amount": amount,
                        "protection_status": "UNPROTECTED",
                    })
                    notifier.send_message(
                        f"🚨 مركز <code>{symbol}</code> المسترد بلا خطة صالحة "
                        "وإغلاقه غير مؤكد؛ التدخل اليدوي مطلوب."
                    )
                continue

            protection = client.ensure_position_protection(
                symbol,
                side,
                amount,
                sl,
                tp,
                client_order_prefix=intent.get("trade_id"),
            )
            if protection.get("status") == "UNKNOWN":
                logger.error(
                    f"[OrderIntent] حالة حماية {symbol} غير معروفة؛ لن نكرر أوامر ولن نغلق المركز."
                )
                state_manager.transition_order_intent(
                    symbol,
                    "RECOVERING",
                    "RECOVERY_WAIT_PROTECTION_QUERY",
                    updates={"retry_after": now + 30},
                )
                continue

            if not protection.get("sl_ok") or not protection.get("tp_ok"):
                missing_legs = [
                    leg
                    for leg, ok in (
                        ("SL", protection.get("sl_ok")),
                        ("TP", protection.get("tp_ok")),
                    )
                    if not ok
                ]
                logger.critical(
                    f"[OrderIntent] استُعيد مركز {symbol} لكن حمايته غير كاملة "
                    f"({','.join(missing_legs)})؛ محاولة إغلاق طارئ."
                )
                state_manager.transition_order_intent(
                    symbol,
                    "RECOVERING",
                    "RECOVERY_EMERGENCY_CLOSE",
                    updates={"pending_close_reason": "PROTECTION_FAILED_EMERGENCY_CLOSE"},
                )
                close_ok = client.close_position(symbol, side, amount)
                if close_ok:
                    proposal = intent.get("user_visible_trade_proposal") or {}
                    if proposal:
                        _proposal_ledger_update(
                            state_manager,
                            proposal,
                            "RECOVERY_PROTECTION_FAILED_EMERGENCY_CLOSED",
                            entry_post_count=1,
                            missing_legs=missing_legs,
                            recovered_after_restart=True,
                        )
                    state_manager.remove_order_intent(symbol)
                    notifier.send_message(
                        f"⚠️ استُعيدت صفقة <code>{symbol}</code> بعد timeout، "
                        "لكن فشلت حمايتها فتم إغلاقها وتأكيد صفر المركز."
                    )
                    continue

                intent = state_manager.get_order_intent(symbol)
                intent["retry_after"] = now + 15
                intent["status"] = "UNPROTECTED_CLOSE_UNCONFIRMED"
                intent["updated_at"] = now
                state_manager.save_order_intent(symbol, intent)
                state_manager.add_active_symbol(symbol)
                state_manager.save_entry_metadata(symbol, {
                    **intent,
                    "entry_price": actual_entry,
                    "amount": amount,
                    "original_amount": amount,
                    "protection_status": "UNPROTECTED",
                })
                proposal = intent.get("user_visible_trade_proposal") or {}
                if proposal:
                    _proposal_ledger_update(
                        state_manager,
                        proposal,
                        "RECOVERY_PROTECTION_FAILED_CLOSE_UNCONFIRMED",
                        entry_post_count=1,
                        missing_legs=missing_legs,
                        reconciliation_required=True,
                    )
                notifier.send_message(
                    f"🚨 <b>مركز مسترد غير محمي</b>\nالزوج: <code>{symbol}</code>\n"
                    "فشل SL وفشل تأكيد الإغلاق؛ التدخل اليدوي مطلوب."
                )
                continue

            tp_steps = risk_manager.get_tp_steps(side, actual_entry, sl, tp3_override=tp)
            entry_meta = {
                **intent,
                "side": side.upper(),
                "entry_price": actual_entry,
                "amount": amount,
                "original_amount": amount,
                "sl": sl,
                "initial_sl": float(intent.get("initial_sl") or sl),
                "current_sl": sl,
                "tp": tp,
                "tp_steps": tp_steps,
                "sl_order_id": protection.get("sl_order_id"),
                "sl_client_id": protection.get("sl_client_id"),
                "sl_actual_order_id": protection.get("sl_actual_order_id"),
                "tp_order_id": protection.get("tp_order_id"),
                "tp_client_id": protection.get("tp_client_id"),
                "tp_actual_order_id": protection.get("tp_actual_order_id"),
                "protection_status": "PROTECTED",
                "recovered_after_timeout": True,
                "mfe_r": float(intent.get("mfe_r") or 0.0),
                "mae_r": float(intent.get("mae_r") or 0.0),
                "planned_risk_usdt": float(
                    intent.get("planned_risk_usdt")
                    or abs(actual_entry - float(intent.get("initial_sl") or sl)) * amount
                ),
            }
            metadata_saved = state_manager.save_entry_metadata(symbol, entry_meta)
            if not metadata_saved:
                state_manager.transition_order_intent(
                    symbol,
                    "RECOVERING",
                    "PROTECTED_METADATA_PENDING",
                    updates={
                        "retry_after": now + 15,
                        **{
                            key: protection.get(key)
                            for key in (
                                "sl_order_id", "sl_client_id", "sl_actual_order_id",
                                "tp_order_id", "tp_client_id", "tp_actual_order_id",
                            )
                        },
                    },
                )
                logger.critical(
                    f"[OrderIntent] حُميت {symbol} لكن تعذر حفظ metadata؛ ستُعاد المحاولة."
                )
                continue
            state_manager.add_active_symbol(symbol)
            trailing_manager.register_trade(
                symbol=symbol,
                side=side,
                entry_price=actual_entry,
                initial_sl=sl,
                amount=amount,
            )
            proposal = entry_meta.get("user_visible_trade_proposal") or {}
            if proposal:
                _proposal_ledger_update(
                    state_manager,
                    proposal,
                    "ENTRY_FILLED_AND_PROTECTED",
                    entry_post_count=1,
                    actual_entry=float(actual_entry),
                    filled_amount=float(amount),
                    sl=float(sl),
                    tp=float(tp),
                    recovered_after_restart=True,
                )
            state_manager.remove_order_intent(symbol)
            audit_decision_event(
                **recovery_audit, stage="ORDER_INTENT_RECOVERY",
                status="POSITION_PROTECTED_AND_RESTORED",
                details={"actual_entry": actual_entry, "amount": amount,
                         "protection": {key: protection.get(key) for key in (
                             "sl_ok", "tp_ok", "sl_order_id", "tp_order_id"
                         )}}, durable=True,
            )
            notifier.send_message(
                f"✅ <b>استرداد صفقة بعد timeout</b>\n"
                f"الزوج: <code>{symbol}</code>\n"
                f"الكمية: <code>{amount}</code>\n"
                "تم ربط الحالة وإثبات SL وTP على Binance بنجاح."
            )
        except Exception as e:
            logger.error(f"[OrderIntent] فشل استرداد {symbol}: {e}")
            audit_decision_event(
                symbol=symbol, trade_id=(intent or {}).get("trade_id"),
                position_id=(intent or {}).get("trade_id"),
                stage="ORDER_INTENT_RECOVERY", status="FAILED",
                details={"error_type": type(e).__name__, "error": str(e)}, durable=True,
            )


def finalize_virtual_order(
    state_manager,
    notifier,
    symbol,
    v_order,
    outcome,
    notification=None,
    blacklist=False,
):
    """إنهاء مركزي ومتسق لأي أمر Sniper افتراضي بحالة طرفية واحدة."""
    trade_id = v_order.get("trade_id")
    if trade_id:
        try:
            from utils.forensics_logger import update_signal_outcome
            update_signal_outcome(trade_id, outcome, 0.0)
        except Exception as exc:
            # فشل التحليلات لا يجب أن يبقي أمراً منتهياً قابلاً للتنفيذ لاحقاً.
            logger.error(f"فشل تحديث نتيجة الأمر المعلق {symbol} في Forensics: {exc}")

    audit_decision_event(
        scan_id=v_order.get("scan_id"),
        decision_id=v_order.get("decision_id"),
        signal_id=v_order.get("decision_id"),
        setup_id=v_order.get("setup_thesis_id"),
        thesis_id=v_order.get("setup_thesis_id"),
        virtual_order_id=v_order.get("virtual_order_id"),
        symbol=symbol,
        stage="SNIPER",
        status=outcome,
        trade_id=trade_id,
        details={
            "created_at": v_order.get("created_at"),
            "entry_zone_touched_at_ms": v_order.get("entry_zone_touched_at_ms"),
            "confirmation_deadline_ms": v_order.get("confirmation_deadline_ms"),
            "structural_resolution_deadline_ms": v_order.get("structural_resolution_deadline_ms"),
            "elapsed_seconds": max(
                0.0,
                time.time() - float(
                    v_order.get("created_at_ms") or time.time() * 1000.0
                ) / 1000.0,
            ),
            "invalidation_state": v_order.get("invalidation_state"),
            "soft_invalidation_at": v_order.get("soft_invalidation_at"),
            "soft_invalidation_price": v_order.get("soft_invalidation_price"),
            "soft_invalidation_extreme": v_order.get("soft_invalidation_extreme"),
            "soft_invalidation_breach_pct": v_order.get("soft_invalidation_breach_pct"),
            "soft_invalidation_breach_atr": v_order.get("soft_invalidation_breach_atr"),
            "structural_close_evidence": v_order.get("structural_close_evidence"),
            "trend_check_evidence": v_order.get("trend_check_evidence"),
            "reclaim_confirmed_at": v_order.get("reclaim_confirmed_at"),
            "last_confirmation": v_order.get("last_confirmation"),
        },
        durable=True,
    )

    if blacklist:
        state_manager.set(f"vorder_tp_cancelled_{symbol}", int(time.time()))
        logger.info(f"[VirtualOrder] {symbol} مُضاف لـ Blacklist مؤقت — جولتان")

    if not state_manager.remove_virtual_order(symbol):
        logger.critical(
            f"[{symbol}] تعذر تثبيت حذف الأمر الافتراضي على القرص؛ "
            "سيبقى محلياً ولن يُعتبر إنهاؤه مكتملاً."
        )
        audit_decision_event(
            scan_id=v_order.get("scan_id"),
            decision_id=v_order.get("decision_id"),
            symbol=symbol,
            stage="SNIPER",
            status="TERMINATION_PERSIST_FAILED",
            trade_id=trade_id,
            durable=True,
        )
        return False

    thesis_id = v_order.get("setup_thesis_id")
    if thesis_id:
        upper_outcome = str(outcome or "").upper()
        if "TIMEOUT" in upper_outcome:
            cooldown_minutes = getattr(Config, "SETUP_COOLDOWN_TIMEOUT_MINUTES", 60)
        else:
            cooldown_minutes = getattr(Config, "SETUP_COOLDOWN_INVALIDATED_MINUTES", 120)
        cooldown_saved = state_manager.set_setup_cooldown(
            symbol=symbol,
            thesis_id=thesis_id,
            duration_minutes=cooldown_minutes,
            reason=upper_outcome,
            details={
                "trade_id": trade_id,
                "decision_id": v_order.get("decision_id"),
            },
        )
        audit_decision_event(
            scan_id=v_order.get("scan_id"),
            decision_id=v_order.get("decision_id"),
            symbol=symbol,
            stage="SETUP_COOLDOWN",
            status="CREATED" if cooldown_saved else "PERSIST_FAILED",
            trade_id=trade_id,
            details={
                "setup_thesis_id": thesis_id,
                "duration_minutes": cooldown_minutes,
                "reason": upper_outcome,
            },
            durable=True,
        )
    if notification:
        try:
            notifier.send_message(notification)
        except Exception as exc:
            logger.warning(f"[{symbol}] تعذر إرسال إشعار إنهاء الأمر الافتراضي: {exc}")
    return True


def _monitor_short_high_confidence_order(
    *, symbol, v_order, current_price, client, state_manager, notifier,
    trailing_manager, risk_manager, ta_engine, active_symbols,
):
    """Advance one persisted SHORT_HIGH_CONFIDENCE_V1 wait causally."""
    if ta_engine is None:
        return True
    interval_ms = 5 * 60 * 1000
    expected_closed_bar = str(
        (int(time.time() * 1000) // interval_ms - 1) * interval_ms
    )
    if str(v_order.get("last_confirmation_bar")) == expected_closed_bar:
        return True

    try:
        trigger_bars = client.fetch_ohlcv(
            symbol,
            timeframe=getattr(Config, "ENTRY_CONFIRMATION_TIMEFRAME", "5m"),
            limit=getattr(Config, "ENTRY_CONFIRMATION_LIMIT", 80),
        )
        bars_df = client.fetch_ohlcv(
            symbol, timeframe="15m", limit=Config.DECISION_CANDLE_HISTORY
        )
        if bars_df is None or len(bars_df) < 210:
            return True
        profile = (
            v_order.get("filter_profile_snapshot")
            if isinstance(v_order.get("filter_profile_snapshot"), dict)
            else _hybrid_decision_demo_profile(get_filter_profile(state_manager))
        )
        bars_df = ta_engine.add_indicators(bars_df, filter_profile=profile)
        latest = bars_df.iloc[-2]
        atr_val = float(latest.get("atr") or 0.0)
        if atr_val <= 0:
            return True
        market_levels = ta_engine.extract_market_levels(
            bars_df, max_age=int(profile.get("max_ob_age", 100))
        )
        trend_1h = ta_engine.get_htf_trend(symbol, client)
        context_1h = ta_engine.get_setup_context_1h(
            symbol, client, "sell"
        )
        context_4h = ta_engine.get_market_context_4h(symbol, client)
        evaluation = evaluate_short_high_confidence(
            decision_price=current_price,
            atr=atr_val,
            trend_1h=trend_1h,
            context_4h=context_4h,
            context_1h=context_1h,
            market_levels=market_levels,
            trigger_bars=trigger_bars,
            invalidation_level=v_order.get("invalidation_level"),
        )
        trigger = evaluation.get("trigger") or {}
        consumed_bar = str(trigger.get("bar_id") or expected_closed_bar)
        v_order["last_confirmation_bar"] = consumed_bar
        v_order["last_short_confidence"] = evaluation
        v_order["trend_1h"] = trend_1h
        v_order["context_1h"] = context_1h
        v_order["context_4h"] = context_4h
        v_order["market_levels"] = market_levels
        count_terminal_decision = bool(
            evaluation.get("status") != "WAIT_SHORT_CONFIRMATION"
            and not v_order.get("short_terminal_counted")
        )
        if count_terminal_decision:
            v_order["short_terminal_counted"] = True
        if not state_manager.save_virtual_order(symbol, v_order):
            logger.critical(
                f"[{symbol}] تعذر حفظ SHORT confirmation؛ لن يتم التنفيذ."
            )
            return True
        _audit_short_confidence(
            state_manager=state_manager,
            symbol=symbol,
            evaluation=evaluation,
            scan_id=v_order.get("scan_id"),
            decision_id=v_order.get("decision_id"),
            trade_id=v_order.get("trade_id"),
            thesis_id=v_order.get("setup_thesis_id"),
            virtual_order_id=v_order.get("virtual_order_id"),
            count=count_terminal_decision,
        )
        if evaluation.get("status") == "REJECTED":
            finalize_virtual_order(
                state_manager,
                notifier,
                symbol,
                v_order,
                str(evaluation.get("reason")),
            )
            return True
        if evaluation.get("status") != "PASS":
            logger.info(
                f"[{symbol}] WAIT_SHORT_CONFIRMATION: "
                f"{evaluation.get('reason')}"
            )
            return True

        max_trades = state_manager.get_max_open_trades()
        active_now = state_manager.get_active_symbols()
        if len(active_now) >= max_trades:
            logger.warning(
                f"[{symbol}] SHORT trigger صالح لكن الحد {max_trades} مكتمل."
            )
            return True

        invalidation_level = float(
            v_order.get("invalidation_level") or current_price
        )
        temp_sl, temp_tp = risk_manager.calculate_sl_tp(
            "sell",
            current_price,
            support=v_order.get("support"),
            resistance=v_order.get("resistance"),
            atr=atr_val,
            invalidation_level=invalidation_level,
            market_levels=market_levels,
            filter_profile=profile,
            setup_anchor=((v_order.get('setup_context') or {}).get('setup_anchor')),
        )
        if temp_sl is None or temp_tp is None:
            finalize_virtual_order(
                state_manager,
                notifier,
                symbol,
                v_order,
                "SHORT_PLAN_INVALID_AT_TRIGGER",
            )
            return True
        target_plan = dict(getattr(risk_manager, "last_plan", {}) or {})
        balance = client.get_balance()
        entry_equity, usdt_free = entry_sizing_balances(balance)
        amount = risk_manager.calculate_position_size(
            entry_equity,
            current_price,
            custom_risk_percent=v_order["trade_risk_pct"],
            atr=atr_val,
            sl=temp_sl,
            leverage=v_order["trade_leverage"],
            entry_margin_percent=Config.ENTRY_MARGIN_PERCENT,
            available_balance=usdt_free,
        )
        amount = min(float(v_order.get("amount") or amount), float(amount))
        if amount <= 0:
            finalize_virtual_order(
                state_manager, notifier, symbol, v_order,
                "SHORT_INSUFFICIENT_BALANCE_AT_TRIGGER",
            )
            return True
        confirmation = {
            "passed": True,
            "reason": evaluation.get("reason"),
            "source": SHORT_HIGH_CONFIDENCE_V1,
            "bar_id": consumed_bar,
            "evidence": evaluation,
        }
        fresh_context = dict(v_order.get("setup_context") or {})
        fresh_context.update({
            "htf_trend": trend_1h,
            "context_1h": context_1h,
            "context_4h": context_4h,
        })
        trade_executed = execute_and_protect_trade(
            symbol=symbol,
            final_decision="sell",
            amount=amount,
            current_price=current_price,
            trade_leverage=v_order["trade_leverage"],
            trade_risk_pct=v_order["trade_risk_pct"],
            atr_val=atr_val,
            coin_data={
                "_scan_id": v_order.get("scan_id"),
                "_decision_id": v_order.get("decision_id"),
                "_trade_id": v_order.get("trade_id"),
                "_thesis_id": v_order.get("setup_thesis_id"),
                "_ta_context": fresh_context,
                "support": v_order.get("support", 0),
                "resistance": v_order.get("resistance", 0),
                "price": current_price,
                "optimal_entry": current_price,
                "reason": "HYBRID SELL + SHORT_HIGH_CONFIDENCE_V1",
                "market_levels": market_levels,
                "target_plan": target_plan,
                "entry_confirmation": confirmation,
                "proposal_execution_mode": "HYBRID_DECISION_DEMO",
                "short_execution_policy": SHORT_HIGH_CONFIDENCE_V1,
                "short_high_confidence": evaluation,
            },
            sentiment={
                "label": v_order.get("sentiment_label", "neutral"),
                "score": v_order.get("sentiment_score", 0.0),
            },
            bars_df=bars_df,
            ticker=client.exchange.fetch_ticker(symbol),
            ta_trend=v_order.get("ta_trend", "sell"),
            client=client,
            risk_manager=risk_manager,
            trailing_manager=trailing_manager,
            state_manager=state_manager,
            notifier=notifier,
            active_symbols=active_symbols,
            scan_results=[],
            invalidation_level=invalidation_level,
            ta_engine=ta_engine,
            trade_id=v_order.get("trade_id"),
        )
        if (
            trade_executed
            or symbol in state_manager.get_active_symbols()
            or bool(state_manager.get_order_intent(symbol))
        ):
            state_manager.remove_virtual_order(symbol)
        return True
    except Exception as exc:
        logger.error(
            f"[{symbol}] فشل SHORT_HIGH_CONFIDENCE_V1 fail-closed: {exc}"
        )
        return True


def monitor_virtual_orders(client, state_manager, notifier, trailing_manager, risk_manager, ta_engine=None):
    """
    مراقبة الأوامر الافتراضية المعلقة (Virtual Pending Orders).
    تستخدم مفاهيم التداول المؤسسي: انتظار إغلاق الشمعة (Candle Close)
    وتأكيد حجم التداول (Volume Confirmation) قبل التنفيذ.
    """
    virtual_orders = state_manager.get_virtual_orders()
    if not virtual_orders:
        return
        
    active_symbols = state_manager.get_active_symbols()
    
    for symbol, v_order in list(virtual_orders.items()):
        if symbol in active_symbols:
            audit_decision_event(
                scan_id=v_order.get("scan_id"), decision_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"), thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"), position_id=v_order.get("trade_id"),
                trade_id=v_order.get("trade_id"), symbol=symbol,
                stage="SNIPER", status="SUPERSEDED_BY_ACTIVE_POSITION", durable=True,
            )
            state_manager.remove_virtual_order(symbol)
            continue

        # ── timeout: إذا انتظر الأمر أكثر من المسموح يُلغى ────────────
        import time as _t
        created_ms  = v_order.get("created_at_ms")
        if created_ms is None:
            # احسب من created_at النصي إذا وُجد
            created_at = _parse_dt_safe(v_order.get("created_at"))
            created_ms = (
                int(created_at.timestamp() * 1000)
                if created_at is not None
                else int(_t.time() * 1000)
            )

        max_wait_min = v_order.get(
            "max_age_minutes",
            getattr(Config, "SNIPER_MAX_WAIT_MINUTES", 15)
        )
        now_ms = int(_t.time() * 1000)
        elapsed_min = (now_ms - created_ms) / 60000
        confirmation_deadline_ms = int(
            v_order.get("confirmation_deadline_ms") or 0
        )
        structural_deadline_ms = int(
            v_order.get("structural_resolution_deadline_ms") or 0
        )
        active_deadline_ms = (
            structural_deadline_ms
            if str(v_order.get("invalidation_state") or "").upper() == "SWEEP_PENDING"
            else confirmation_deadline_ms
        )
        setup_timed_out = not active_deadline_ms and elapsed_min > max_wait_min
        confirmation_timed_out = (
            active_deadline_ms > 0 and now_ms > active_deadline_ms
        )

        if setup_timed_out or confirmation_timed_out:
            timeout_kind = (
                (
                    "STRUCTURAL_SWEEP_WINDOW"
                    if structural_deadline_ms and confirmation_timed_out
                    else "CONFIRMATION_WINDOW"
                )
                if confirmation_timed_out
                else "SETUP_DISCOVERY"
            )
            logger.info(
                f"[Sniper] ⏰ انتهت مهلة الأمر المعلق لـ {symbol} "
                f"({timeout_kind}, elapsed={elapsed_min:.1f}m) — إلغاء"
            )
            if v_order.get("execution_policy") == SHORT_HIGH_CONFIDENCE_V1:
                _short_confidence_counter(
                    state_manager, "SHORT_REJECT_5M"
                )
            finalize_virtual_order(
                state_manager,
                notifier,
                symbol,
                v_order,
                "EXPIRED_VIRTUAL_TIMEOUT",
                notification=(
                    f"⏰ <b>انتهت مهلة الأمر القناص</b>\n"
                    f"الزوج: <code>{symbol}</code>\n"
                    f"السبب: {timeout_kind} — لم يكتمل تأكيد 5m المغلق"
                ),
            )
            continue

        global ws_manager
        mark_price_fallback = getattr(client, "get_mark_price", None)
        price_snapshot = ws_manager.get_price_with_source(
            symbol, fallback_fn=mark_price_fallback
        )
        current_price = float(price_snapshot.get("price") or 0.0)
        price_source = price_snapshot.get("source")
        if price_source != v_order.get("last_price_source"):
            audit_decision_event(
                scan_id=v_order.get("scan_id"), decision_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"), thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"), trade_id=v_order.get("trade_id"),
                symbol=symbol, stage="PRICE_SOURCE", status=price_source or "UNKNOWN",
                details=price_snapshot,
            )
            v_order["last_price_source"] = price_source
            state_manager.save_virtual_order(symbol, v_order)
        if not current_price:
            continue

        if v_order.get("execution_policy") == SHORT_HIGH_CONFIDENCE_V1:
            _monitor_short_high_confidence_order(
                symbol=symbol,
                v_order=v_order,
                current_price=current_price,
                client=client,
                state_manager=state_manager,
                notifier=notifier,
                trailing_manager=trailing_manager,
                risk_manager=risk_manager,
                ta_engine=ta_engine,
                active_symbols=active_symbols,
            )
            continue

        optimal_entry = float(v_order.get("optimal_entry") or 0.0)
        side = str(v_order.get("side") or "").lower()
        tolerance = entry_zone_tolerance(optimal_entry, v_order.get("atr", 0))
        near_entry_zone = bool(
            optimal_entry - tolerance
            <= current_price
            <= optimal_entry + tolerance
        )
        basis_check_skipped = bool(
            near_entry_zone
            and not v_order.get("entry_zone_touched_at_ms")
            and now_ms < int(v_order.get("entry_basis_retry_until_ms") or 0)
        )
        if near_entry_zone and not v_order.get("entry_zone_touched_at_ms") and not basis_check_skipped:
            touch_ticker = fetch_executable_contract_ticker(client, symbol)
            contract_last_price = float(
                (touch_ticker or {}).get("last")
                or (touch_ticker or {}).get("close")
                or 0.0
            )
            contract_touch_price = float(
                (touch_ticker or {}).get("ask" if side == "buy" else "bid")
                or 0.0
            )
            if touch_ticker.get("_executable_price_source") == "UNAVAILABLE":
                logger.warning(
                    f"[EntryWatch] {symbol}: تعذر التحقق من لمس سعر العقد: "
                    f"{touch_ticker.get('_order_book_error') or touch_ticker.get('_ticker_error')}"
                )
            touch_guard = evaluate_executable_entry_price(
                side=side,
                ticker=touch_ticker,
                reference_price=current_price,
                optimal_entry=optimal_entry,
                atr_val=v_order.get("atr", 0),
            )
            contract_near_entry = bool(touch_guard.get("passed"))
            executable_source = touch_ticker.get("_executable_price_source")
            basis_status = (
                "ALIGNED"
                if contract_near_entry
                else (
                    "MARK_ONLY_TOUCH"
                    if executable_source == "UNAVAILABLE"
                    else "EXECUTABLE_GUARD_REJECTED"
                )
            )
            audit_decision_event(
                scan_id=v_order.get("scan_id"),
                decision_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"),
                thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"),
                trade_id=v_order.get("trade_id"),
                symbol=symbol,
                stage="ENTRY_PRICE_BASIS",
                status=basis_status,
                details={
                    "mark_price": current_price,
                    "contract_last": contract_last_price,
                    "executable_price": contract_touch_price,
                    "executable_side": "ASK" if side == "buy" else "BID",
                    "optimal_entry": optimal_entry,
                    "tolerance": tolerance,
                    "bid": (touch_ticker or {}).get("bid") if touch_ticker else None,
                    "ask": (touch_ticker or {}).get("ask") if touch_ticker else None,
                    "executable_price_source": (
                        (touch_ticker or {}).get("_executable_price_source")
                    ),
                    "executable_guard": touch_guard,
                },
            )
            if not contract_near_entry:
                retry_seconds = max(
                    2.0,
                    float(getattr(Config, "ENTRY_BASIS_RETRY_SECONDS", 15)),
                )
                v_order["entry_basis_retry_until_ms"] = (
                    now_ms + int(retry_seconds * 1000)
                )
                logger.info(
                    f"[EntryWatch] {symbol}: لمس Mark فقط دون سعر العقد؛ "
                    f"لن يبدأ تأكيد الدخول؛ إعادة فحص العقد بعد {retry_seconds:.0f}s."
                )
                if not state_manager.save_virtual_order(symbol, v_order):
                    logger.critical(
                        f"[{symbol}] تعذر حفظ مهلة إعادة فحص سعر العقد؛ "
                        "سيعاد الفحص دون backoff في الدورة التالية."
                    )
                continue
            v_order.pop("entry_basis_retry_until_ms", None)
            confirmation_window = max(
                7,
                int(getattr(Config, "ENTRY_CONFIRMATION_WINDOW_MINUTES", 12)),
            )
            v_order["entry_zone_touched_at_ms"] = now_ms
            v_order["entry_zone_touched_price"] = float(contract_touch_price)
            v_order["entry_zone_touch_kind"] = "LIVE_BOUNDED"
            v_order["entry_zone_touch_price_source"] = touch_ticker.get(
                "_executable_price_source"
            )
            v_order["confirmation_deadline_ms"] = (
                now_ms + confirmation_window * 60 * 1000
            )
            audit_decision_event(
                scan_id=v_order.get("scan_id"),
                decision_id=v_order.get("decision_id"),
                signal_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"),
                thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"),
                symbol=symbol,
                stage="ENTRY_ZONE_TOUCH",
                status="TOUCHED",
                trade_id=v_order.get("trade_id"),
                details={
                    "price": float(contract_touch_price),
                    "mark_price": float(current_price),
                    "optimal_entry": optimal_entry,
                    "tolerance": tolerance,
                    "confirmation_window_minutes": confirmation_window,
                },
                durable=True,
            )
            if not state_manager.save_virtual_order(symbol, v_order):
                logger.critical(
                    f"[{symbol}] تعذر حفظ بداية نافذة التأكيد؛ لن يُنفذ الأمر."
                )
                continue
        guard_interval_ms = 15 * 60 * 1000
        guard_bar = str(
            (int(time.time() * 1000) // guard_interval_ms - 1) * guard_interval_ms
        )
        sweep_pending_before = (
            str(v_order.get("invalidation_state") or "VALID").upper()
            == "SWEEP_PENDING"
        )
        next_structure_check_ms = int(
            v_order.get("next_structure_check_ms") or 0
        )
        check_structure = (
            now_ms >= next_structure_check_ms
            if sweep_pending_before
            else (
                near_entry_zone
                and str(v_order.get("last_structure_guard_bar")) != guard_bar
            )
        )
        invalidation_state_before = str(
            v_order.get("invalidation_state") or "VALID"
        ).upper()
        structure_check_marker_before = int(
            v_order.get("structure_check_attempt_serial") or 0
        )
        cancel_reason = check_virtual_order_cancellation(
            client,
            symbol,
            v_order,
            current_price=current_price,
            check_structure=check_structure,
        )
        invalidation_state_after = str(
            v_order.get("invalidation_state") or "VALID"
        ).upper()
        structure_check_performed = int(
            v_order.get("structure_check_attempt_serial") or 0
        ) != structure_check_marker_before
        invalidation_state_changed = (
            invalidation_state_after != invalidation_state_before
        )
        if invalidation_state_changed:
            audit_decision_event(
                scan_id=v_order.get("scan_id"),
                decision_id=v_order.get("decision_id"),
                signal_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"),
                thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"),
                symbol=symbol,
                stage="INVALIDATION_STATE",
                status=invalidation_state_after,
                trade_id=v_order.get("trade_id"),
                details={
                    "previous_state": invalidation_state_before,
                    "current_price": current_price,
                    "invalidation_level": v_order.get("invalidation_level"),
                    "soft_invalidation_at": v_order.get("soft_invalidation_at"),
                    "reclaim_confirmed_at": v_order.get("reclaim_confirmed_at"),
                    "setup_thesis_id": v_order.get("setup_thesis_id"),
                    "soft_invalidation_price": v_order.get("soft_invalidation_price"),
                    "soft_invalidation_extreme": v_order.get("soft_invalidation_extreme"),
                    "breach_pct": v_order.get("soft_invalidation_breach_pct"),
                    "breach_atr": v_order.get("soft_invalidation_breach_atr"),
                    "structural_close_evidence": v_order.get("structural_close_evidence"),
                },
                durable=True,
            )
        if (check_structure or invalidation_state_changed) and not cancel_reason:
            structural_evidence = (
                v_order.get("structural_close_evidence")
                if isinstance(v_order.get("structural_close_evidence"), dict)
                else {}
            )
            waiting_for_post_breach_close = (
                invalidation_state_after == "SWEEP_PENDING"
                and not structural_evidence.get("closed_after_breach")
            )
            if waiting_for_post_breach_close:
                retry_delays = tuple(
                    getattr(
                        Config,
                        "VIRTUAL_ORDER_STRUCTURE_RETRY_DELAYS_SECONDS",
                        (3, 10, 25, 50, 90),
                    )
                ) or (3, 10, 25, 50, 90)
                max_attempts = min(
                    len(retry_delays),
                    max(
                        1,
                        int(
                            getattr(
                                Config,
                                "VIRTUAL_ORDER_STRUCTURE_MAX_ATTEMPTS",
                                len(retry_delays),
                            )
                        ),
                    ),
                )
                attempts = int(v_order.get("structure_retry_count") or 0)
                if structure_check_performed:
                    attempts += 1
                    v_order["structure_retry_count"] = attempts
                if attempts >= max_attempts:
                    cancel_reason = (
                        "STRUCTURAL_EVIDENCE_UNAVAILABLE: انتهت محاولات جلب "
                        "أول إغلاق 15m بعد sweep دون دليل حديث."
                    )
                else:
                    expected_close_ms = int(
                        v_order.get("structure_expected_close_ms") or now_ms
                    )
                    due_from_schedule = (
                        expected_close_ms
                        + int(retry_delays[min(attempts, len(retry_delays) - 1)])
                        * 1000
                    )
                    # إذا استيقظ monitor متأخراً، لا ينفذ حلقات catch-up؛ يؤجل
                    # محاولة واحدة على الأقل كي لا يحدث burst متزامن.
                    v_order["next_structure_check_ms"] = max(
                        due_from_schedule,
                        now_ms + 1000,
                    )
            else:
                v_order["last_structure_guard_bar"] = guard_bar
                v_order.pop("next_structure_check_ms", None)
            if not state_manager.save_virtual_order(symbol, v_order):
                logger.critical(
                    f"[{symbol}] تعذر حفظ توقيت فحص هيكل الأمر المعلق؛ لن يُنفذ."
                )
                continue

        if cancel_reason:
            logger.warning(f"🧹 [VirtualOrder CANCELLED] {symbol}: {cancel_reason}")
            safe_reason = (
                notifier._safe_html(cancel_reason)
                if hasattr(notifier, "_safe_html") else str(cancel_reason)
            )
            finalize_virtual_order(
                state_manager,
                notifier,
                symbol,
                v_order,
                f"CANCELLED_VIRTUAL: {cancel_reason}",
                blacklist=("tp" in cancel_reason.lower() or "ضرب هدف" in cancel_reason),
                notification=(
                    f"🧹 <b>إلغاء أمر قناص معلق</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>الزوج:</b> <code>{symbol}</code>\n"
                    f"• <b>السبب:</b> <code>{safe_reason}</code>"
                ),
            )

            continue

        # لا يوجد دخول أثناء كون sweep غير محسوم. ننتظر أول إغلاق 15m بعده.
        if invalidation_state_after == "SWEEP_PENDING":
            continue

        # ── فحص: هل ضرب السعر الهدف قبل أخذ الطلب؟ (Target Invalidation) ──
        if v_order.get('cancel_if_tp_hit_first', True):
            tp_levels = v_order.get('tp_steps') or []
            # نأخذ أول هدف — إما من tp_steps أو من tp مباشرة
            if tp_levels and isinstance(tp_levels[0], dict):
                first_tp = float(tp_levels[0].get('price', 0) or 0)
            elif tp_levels:
                first_tp = float(tp_levels[0])
            else:
                first_tp = float(v_order.get('tp', 0) or 0)

            if first_tp > 0:
                target_hit_before_entry = False
                if side == 'buy' and current_price >= first_tp:
                    target_hit_before_entry = True
                elif side == 'sell' and current_price <= first_tp:
                    target_hit_before_entry = True

                if target_hit_before_entry:
                    logger.info(
                        f"[Sniper] {symbol} — السعر وصل للهدف "
                        f"({current_price:.6f} vs TP={first_tp:.6f}) "
                        f"قبل أخذ الطلب — إلغاء الأمر المعلق"
                    )
                    finalize_virtual_order(
                        state_manager,
                        notifier,
                        symbol,
                        v_order,
                        "CANCELLED_VIRTUAL: TARGET_HIT_BEFORE_ENTRY",
                        blacklist=True,
                        notification=(
                            f"🚫 <b>إلغاء أمر قناص — الهدف سبق الدخول</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"• <b>الزوج:</b> <code>{symbol}</code>\n"
                            f"• <b>السعر الحالي:</b> <code>{current_price:.6f}</code>\n"
                            f"• <b>الهدف الأول:</b> <code>{first_tp:.6f}</code>\n\n"
                            f"السعر تجاوز الهدف قبل الارتداد للدخول — الفرصة انتهت ✅"
                        ),
                    )
                    continue

        # أثناء backoff نترك حراس الإبطال/الهدف يعملون أعلاه، لكن لا نبدأ
        # تأكيد 5m من لمس Mark لم يُثبت بعد على سعر العقد.
        if basis_check_skipped:
            continue

        # قبل اللمس لا نجلب شموع 5m إلا عند دخول السعر المنطقة. بعد تسجيل
        # اللمس نتابع كل شمعة مغلقة جديدة حتى نهاية نافذة التأكيد، حتى لو
        # خرج السعر من المنطقة نتيجة الارتداد الذي ننتظر تأكيده.
        zone_touch_recorded = bool(v_order.get("entry_zone_touched_at_ms"))
        if not near_entry_zone and not zone_touch_recorded:
            continue

        try:
            # لا نكرر REST داخل نفس الشمعة المغلقة حتى لو دارت المراقبة كل ثانيتين.
            interval_ms = 5 * 60 * 1000
            expected_closed_bar = str(
                (int(time.time() * 1000) // interval_ms - 1) * interval_ms
            )
            # مصدر الحقيقة هو bar_id الذي استُهلك فعلاً، لا slot حاولنا
            # قراءته. قد تتقدم علامة المحاولة على REST/الحالة المحفوظة؛ ربط
            # البوابة بها يُسقط شمعة 5m جديدة كاملة (كما حدث لـ XVG).
            previous_confirmation_bar = v_order.get("last_confirmation_bar")
            previous_confirmation_attempt_slot = v_order.get(
                "last_confirmation_attempt_slot"
            )
            if str(v_order.get("last_confirmation_bar")) == expected_closed_bar:
                continue
            if str(v_order.get("confirmation_abandoned_slot")) == expected_closed_bar:
                continue
            if (
                str(v_order.get("confirmation_data_retry_slot")) == expected_closed_bar
                and now_ms < int(v_order.get("next_confirmation_data_retry_ms") or 0)
            ):
                continue

            trigger_bars = client.fetch_ohlcv(
                symbol,
                timeframe=getattr(Config, "ENTRY_CONFIRMATION_TIMEFRAME", "5m"),
                limit=getattr(Config, "ENTRY_CONFIRMATION_LIMIT", 80),
            )
            filter_profile = (
                v_order.get("filter_profile_snapshot")
                if isinstance(v_order.get("filter_profile_snapshot"), dict)
                else get_filter_profile(state_manager)
            )
            setup_context = dict(v_order.get("setup_context", {}) or {})
            setup_context["invalidation_state"] = invalidation_state_after
            confirmation = evaluate_entry_confirmation(
                side=side,
                trigger_bars=trigger_bars,
                current_price=current_price,
                optimal_entry=optimal_entry,
                invalidation_level=v_order.get("invalidation_level", current_price),
                atr_15m=v_order.get("atr", 0),
                ta_context=setup_context,
                filter_profile=filter_profile,
                planned_rr=v_order.get("planned_rr", 0),
                zone_touch_recorded=zone_touch_recorded,
            )
            closed_bar = confirmation.get("closed_bar") or {}
            if closed_bar.get("exact_closed_bar", True) is False:
                if str(v_order.get("confirmation_data_retry_slot")) != expected_closed_bar:
                    v_order["confirmation_data_retry_slot"] = expected_closed_bar
                    v_order["confirmation_data_retry_count"] = 0
                retry_count = int(v_order.get("confirmation_data_retry_count") or 0) + 1
                retry_delays = tuple(
                    getattr(
                        Config,
                        "ENTRY_CONFIRMATION_DATA_RETRY_DELAYS_SECONDS",
                        (3, 10, 25, 50),
                    )
                ) or (3, 10, 25, 50)
                max_attempts = max(
                    1,
                    int(getattr(Config, "ENTRY_CONFIRMATION_DATA_MAX_ATTEMPTS", 4)),
                )
                v_order["confirmation_data_retry_count"] = retry_count
                v_order["last_confirmation"] = confirmation
                if retry_count >= max_attempts:
                    v_order["confirmation_abandoned_slot"] = expected_closed_bar
                    v_order["last_confirmation_attempt_slot"] = expected_closed_bar
                    v_order["next_confirmation_data_retry_ms"] = 0
                    logger.error(
                        f"[EntryWatch] {symbol}: تعذر الحصول على شمعة 5m "
                        f"المغلقة المتوقعة بعد {retry_count} محاولات؛ انتظار الشمعة التالية."
                    )
                else:
                    delay = retry_delays[min(retry_count - 1, len(retry_delays) - 1)]
                    v_order["next_confirmation_data_retry_ms"] = now_ms + int(delay * 1000)
                    logger.warning(
                        f"[EntryWatch] {symbol}: REST لم يعرض بعد شمعة 5m "
                        f"المغلقة المتوقعة؛ إعادة {retry_count}/{max_attempts} بعد {delay}s."
                    )
                state_manager.save_virtual_order(symbol, v_order)
                continue
            v_order.pop("confirmation_data_retry_slot", None)
            v_order.pop("confirmation_data_retry_count", None)
            v_order.pop("next_confirmation_data_retry_ms", None)
            v_order.pop("confirmation_abandoned_slot", None)
            raw_confirmation_bar_id = confirmation.get("bar_id")
            confirmation_bar_id = raw_confirmation_bar_id
            # A valid closed candle must be marked by the slot that was
            # actually consumed. If an adapter omits or misformats its
            # timestamp, the exact-closed flag makes the expected slot the
            # only safe causal identifier. Stale/non-exact data is handled
            # above and never reaches this assignment.
            if closed_bar.get("exact_closed_bar") is True:
                confirmation_bar_id = expected_closed_bar
            v_order["last_confirmation_bar"] = confirmation_bar_id
            v_order["last_confirmation_attempt_slot"] = expected_closed_bar
            v_order["last_confirmation"] = confirmation
            if not state_manager.save_virtual_order(symbol, v_order):
                logger.critical(
                    f"[{symbol}] تعذر حفظ نتيجة تأكيد 5m؛ لن يتم التنفيذ."
                )
                continue

            audit_decision_event(
                scan_id=v_order.get("scan_id"),
                decision_id=v_order.get("decision_id"),
                signal_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"),
                thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"),
                symbol=symbol,
                stage="ENTRY_CONFIRMATION",
                status="PASS" if confirmation.get("passed") else "WAIT",
                trade_id=v_order.get("trade_id"),
                details={
                    "confirmation": confirmation,
                    "trigger_source": "BINANCE_USDM_FUTURES_REST",
                    "setup_thesis_id": v_order.get("setup_thesis_id"),
                    "target_plan": v_order.get("target_plan", {}),
                    "confirmation_gate": {
                        "expected_closed_bar": expected_closed_bar,
                        "previous_confirmation_bar": previous_confirmation_bar,
                        "previous_confirmation_attempt_slot": (
                            previous_confirmation_attempt_slot
                        ),
                        "raw_bar_id": raw_confirmation_bar_id,
                        "consumed_bar_id": confirmation_bar_id,
                        "source": "LAST_CONFIRMATION_BAR",
                    },
                },
                durable=True,
            )
            if not confirmation.get("passed"):
                logger.info(
                    f"⏳ [EntryWatch] {symbol}: {confirmation.get('reason')} "
                    f"| score={confirmation.get('score', 0)}"
                )
                continue

            max_trades = state_manager.get_max_open_trades()
            active_now = state_manager.get_active_symbols()
            if len(active_now) >= max_trades:
                logger.warning(
                    f"[EntryWatch] {symbol}: تأكيد صالح لكن الحد {max_trades} مكتمل؛ "
                    "لن نتجاوز بوابة التنفيذ المركزية."
                )
                continue

            # نحتاج نافذة 15m كاملة لتحديث الجودة وبيانات سجل الصفقة، لا لتغيير
            # دلالة شمعة التأكيد التي حُسمت أعلاه على 5m.
            bars_df = client.fetch_ohlcv(symbol, timeframe="15m", limit=Config.DECISION_CANDLE_HISTORY)
            if bars_df is None or len(bars_df) < 210:
                logger.warning(f"[EntryWatch] {symbol}: نافذة 15m غير كافية للتنفيذ.")
                continue
            bars_df = ta_engine.add_indicators(
                bars_df, filter_profile=filter_profile
            )
            latest_15m = bars_df.iloc[-2]
            fresh_atr = float(latest_15m.get("atr") or 0.0)
            if fresh_atr <= 0:
                logger.warning(
                    f"[EntryWatch] {symbol}: ATR الحديث غير صالح؛ لن تستخدم الخطة ATR قديم."
                )
                continue
            fresh_market_levels = ta_engine.extract_market_levels(
                bars_df,
                max_age=int(filter_profile.get("max_ob_age", 100)),
            )
            fresh_context = dict(v_order.get("setup_context", {}) or {})
            fresh_context.update({
                "ema_50": float(latest_15m.get("ema_50") or 0),
                "ema_200": float(latest_15m.get("ema_200") or 0),
                "adx": float(latest_15m.get("adx") or 0),
                "htf_trend": ta_engine.get_htf_trend(symbol, client),
                "market_regime": detect_market_regime(client),
                "invalidation_state": invalidation_state_after,
            })
            revalidated = evaluate_entry_confirmation(
                side=side,
                trigger_bars=trigger_bars,
                current_price=current_price,
                optimal_entry=optimal_entry,
                invalidation_level=v_order.get("invalidation_level", current_price),
                atr_15m=fresh_atr,
                ta_context=fresh_context,
                filter_profile=filter_profile,
                planned_rr=v_order.get("planned_rr", 0),
                zone_touch_recorded=zone_touch_recorded,
            )
            if not revalidated.get("passed"):
                logger.warning(
                    f"[EntryWatch] {symbol}: فشل إعادة التحقق بسياق 15m/1H "
                    f"الحديث ({revalidated.get('reason')})."
                )
                v_order["last_confirmation"] = revalidated
                state_manager.save_virtual_order(symbol, v_order)
                audit_decision_event(
                    scan_id=v_order.get("scan_id"),
                    decision_id=v_order.get("decision_id"),
                    signal_id=v_order.get("decision_id"),
                    setup_id=v_order.get("setup_thesis_id"),
                    thesis_id=v_order.get("setup_thesis_id"),
                    virtual_order_id=v_order.get("virtual_order_id"),
                    symbol=symbol,
                    stage="ENTRY_CONFIRMATION",
                    status="WAIT_REVALIDATION",
                    trade_id=v_order.get("trade_id"),
                    details={"confirmation": revalidated, "fresh_context": fresh_context},
                    durable=True,
                )
                continue
            confirmation = revalidated

            temp_sl, temp_tp = risk_manager.calculate_sl_tp(
                side,
                current_price,
                atr=fresh_atr,
                invalidation_level=v_order.get("invalidation_level", current_price),
                market_levels=fresh_market_levels,
                filter_profile=filter_profile,
                setup_anchor=(fresh_context.get('setup_anchor')),
            )
            raw_fresh_target_plan = getattr(risk_manager, "last_plan", {}) or {}
            fresh_target_plan = (
                dict(raw_fresh_target_plan)
                if isinstance(raw_fresh_target_plan, dict)
                else {}
            )
            plan_revalidation_details = {
                "trigger_price": current_price,
                "original_entry": v_order.get("optimal_entry"),
                "original_sl": v_order.get("sl"),
                "original_tp": v_order.get("tp"),
                "original_planned_rr": v_order.get("planned_rr"),
                "original_target_plan": v_order.get("target_plan") or {},
                "fresh_sl": temp_sl,
                "fresh_tp": temp_tp,
                "fresh_target_plan": fresh_target_plan,
                "fresh_block_reason": getattr(
                    risk_manager, "last_block_reason", None
                ),
            }
            audit_decision_event(
                scan_id=v_order.get("scan_id"),
                decision_id=v_order.get("decision_id"),
                signal_id=v_order.get("decision_id"),
                setup_id=v_order.get("setup_thesis_id"),
                thesis_id=v_order.get("setup_thesis_id"),
                virtual_order_id=v_order.get("virtual_order_id"),
                trade_id=v_order.get("trade_id"),
                symbol=symbol,
                stage="ENTRY_PLAN_REVALIDATION",
                status="REFRESHED" if temp_sl is not None and temp_tp is not None else "REJECTED",
                details=plan_revalidation_details,
                durable=True,
            )
            if temp_sl is None or temp_tp is None:
                finalize_virtual_order(
                    state_manager,
                    notifier,
                    symbol,
                    v_order,
                    f"CANCELLED_VIRTUAL: RR_INVALID_AT_TRIGGER: {risk_manager.last_block_reason}",
                )
                continue

            balance = client.get_balance()
            entry_equity, usdt_free = entry_sizing_balances(balance)
            recalculated_amount = risk_manager.calculate_position_size(
                entry_equity,
                current_price,
                custom_risk_percent=v_order["trade_risk_pct"],
                atr=fresh_atr,
                sl=temp_sl,
                leverage=v_order["trade_leverage"],
                entry_margin_percent=Config.ENTRY_MARGIN_PERCENT,
                available_balance=usdt_free,
            )
            original_amount = float(
                v_order.get("amount", recalculated_amount) or recalculated_amount
            )
            new_amount = min(original_amount, recalculated_amount)
            if new_amount <= 0:
                finalize_virtual_order(
                    state_manager,
                    notifier,
                    symbol,
                    v_order,
                    "CANCELLED_VIRTUAL: INSUFFICIENT_BALANCE",
                )
                continue

            notifier.send_message(
                f"✅ <b>إشارة دخول 5m مؤكدة</b>\n"
                f"الزوج: <code>{symbol}</code>\n"
                f"الدرجة التشخيصية: <code>{confirmation.get('score', 0)}</code>\n"
                "جاري التنفيذ عبر بوابة الأمان المركزية."
            )
            trade_executed = execute_and_protect_trade(
                symbol=symbol,
                final_decision=side,
                amount=new_amount,
                current_price=current_price,
                trade_leverage=v_order["trade_leverage"],
                trade_risk_pct=v_order["trade_risk_pct"],
                atr_val=fresh_atr,
                coin_data={
                    "_scan_id": v_order.get("scan_id"),
                    "_decision_id": v_order.get("decision_id"),
                    "_trade_id": v_order.get("trade_id"),
                    "_thesis_id": v_order.get("setup_thesis_id"),
                    "_ta_context": fresh_context,
                    "support": v_order["support"],
                    "resistance": v_order["resistance"],
                    "price": current_price,
                    "reason": "SMC 15m + closed 5m trigger",
                    "tp_steps": v_order.get("tp_steps", []),
                    "market_levels": fresh_market_levels,
                    "target_plan": fresh_target_plan,
                    "entry_confirmation": confirmation,
                    "optimal_entry": optimal_entry,
                },
                sentiment={
                    "label": v_order["sentiment_label"],
                    "score": v_order["sentiment_score"],
                },
                bars_df=bars_df,
                ticker=client.exchange.fetch_ticker(symbol),
                ta_trend=v_order["ta_trend"],
                client=client,
                risk_manager=risk_manager,
                trailing_manager=trailing_manager,
                state_manager=state_manager,
                notifier=notifier,
                active_symbols=active_symbols,
                scan_results=[],
                invalidation_level=v_order.get("invalidation_level"),
                ta_engine=ta_engine,
                trade_id=v_order.get("trade_id"),
            )
            if (
                trade_executed
                or symbol in state_manager.get_active_symbols()
                or bool(state_manager.get_order_intent(symbol))
            ):
                state_manager.remove_virtual_order(symbol)
            else:
                logger.warning(
                    f"[EntryWatch] فشل تنفيذ {symbol} دون ظهور مركز أو نية؛ "
                    "سيبقى الإعداد للمحاولة على شمعة 5m جديدة فقط."
                )
        except Exception as e:
            logger.error(f"❌ فشل تحويل الأمر المعلق إلى صفقة حقيقية للزوج {symbol}: {e}")

def _emit_confirmed_protection_notification(
    *, state_manager, notifier, symbol, meta, stage, revision,
    previous_sl, requested_sl, confirmed_sl, message,
):
    """Notify exactly once for a venue-confirmed protection revision."""
    dedup = {"emit": True, "reason": "DEDUP_DISABLED"}
    if getattr(Config, "PROTECTION_NOTIFICATION_DEDUP_ENABLED", True):
        dedup = state_manager.record_confirmed_protection_notification(
            symbol,
            trade_id=str(meta.get("trade_id") or meta.get("position_id") or symbol),
            protection_revision=str(revision),
            confirmed_sl=float(confirmed_sl),
            stage=str(stage),
        )
    audit_decision_event(
        scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
        setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
        trade_id=meta.get("trade_id"), symbol=symbol,
        stage="PROTECTION_NOTIFICATION",
        status="EMITTED" if dedup.get("emit") else "DEDUP_SKIPPED",
        details={
            "previous_confirmed_sl": float(previous_sl or 0.0),
            "requested_new_sl": float(requested_sl or 0.0),
            "confirmed_new_sl": float(confirmed_sl or 0.0),
            "protection_stage": str(stage),
            "notification_emitted": bool(dedup.get("emit")),
            "dedup_reason": dedup.get("reason"),
        },
        durable=True,
    )
    if dedup.get("emit"):
        notifier.send_message(message)
    return dedup


def _request_stop_replacement(client, symbol, side, amount, requested_stop):
    """Normalize legacy and durable SL replacement APIs to an explicit state."""
    stateful = getattr(client, "replace_sl_order_state", None)
    stateful_declared = (
        callable(getattr(type(client), "replace_sl_order_state", None))
        or "replace_sl_order_state" in getattr(client, "__dict__", {})
    )
    if callable(stateful) and stateful_declared:
        return stateful(symbol, side, amount, requested_stop)
    legacy = client.replace_sl_order(symbol, side, amount, requested_stop)
    return {
        "state": "CONFIRMED" if legacy else "FAILED",
        "confirmed_stop": legacy or None,
        "requested_stop": requested_stop,
        "reason": "LEGACY_BOOLEAN_RESULT",
    }


def _record_pending_stop_replacement(
    *, state_manager, meta, symbol, stage, requested_stop, old_stop,
    replacement, audit_status,
):
    retry_after = max(
        float(replacement.get("retry_after") or 0.0),
        time.time() + 2.0,
    )
    meta["sl_replace_retry_after"] = retry_after
    meta["protection_status"] = "UPGRADE_RECONCILING"
    state_manager.save_entry_metadata(symbol, meta)
    audit_decision_event(
        scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
        setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
        trade_id=meta.get("trade_id"), symbol=symbol,
        stage=stage, status=audit_status,
        details={"old_sl": old_stop, "requested_sl": requested_stop,
                 "reason": replacement.get("reason"), "retry_after": retry_after},
        durable=True,
    )


def manage_tp_ladder(client, state_manager, trailing_manager, notifier):
    """
    إدارة مراحل حماية الربح دون إغلاق جزئي. المستوى النهائي تملكه أوامر
    Binance الواقية ولا يُعاد تنفيذه محلياً.
    """
    state          = state_manager.get_state()
    entry_metadata = state.get("entry_metadata", {}) or {}

    for symbol, meta in list(entry_metadata.items()):
        try:
            tp_steps = meta.get("tp_steps", [])

            side = str(meta.get("side", "BUY")).upper()
            if side in ["LONG"]:
                side = "BUY"
            elif side in ["SHORT"]:
                side = "SELL"

            original_amount = float(meta.get("original_amount") or meta.get("amount") or 0.0)
            if original_amount <= 0:
                continue

            current_price = client.get_current_price(symbol)
            if not current_price:
                continue

            current_amount = client.get_position_amount(
                symbol,
                position_side=(
                    "LONG" if side == "BUY" else "SHORT"
                ) if getattr(client, "_is_hedge_mode", False) else None,
            )
            if not current_amount or current_amount <= 0:
                continue

            if update_trade_excursions(meta, current_price, symbol=symbol):
                if not state_manager.save_entry_metadata(symbol, meta):
                    logger.error(
                        f"[{symbol}] تعذر حفظ MFE/MAE؛ ستُعاد المحاولة في الدورة التالية."
                    )

            # V2 deliberately removes the 85%/92% price-hugging and TP1 75%
            # locks.  The original full position and final venue target remain;
            # TrailingStopManager alone advances SL behind confirmed structure.
            if getattr(Config, "TRADE_MANAGEMENT_V2_ENABLED", False):
                continue

            if not tp_steps:
                continue

            entry_price = float(meta.get("entry_price", 0.0))
            sl_update_blocked = time.time() < float(
                meta.get("sl_replace_retry_after", 0) or 0
            )

            for idx, step in enumerate(tp_steps):
                if step.get("status") == "done":
                    continue

                step_price  = float(step["price"])
                step_vol    = float(step["volume"])
                is_last     = (idx == len(tp_steps) - 1)
                ttp_enabled = step.get("trailing", {}).get("enabled", False)
                ttp_dev     = float(step.get("trailing", {}).get("percent",
                              getattr(Config, "TTP_DEVIATION_PCT", 0.005)))

                # TP النهائي موضوع مسبقاً على Binance ليغلق كامل المركز. لا
                # نحرك SL لحظة وصوله ولا نرسل أمر إغلاق منافساً لأمر المنصة.
                if step.get("label") == "FINAL_TARGET":
                    break

                price_reached = (
                    (side == "BUY"  and current_price >= step_price) or
                    (side == "SELL" and current_price <= step_price)
                )

                # ── TTP Logic (للخطوة الأخيرة فقط) ──────────────────
                if is_last and ttp_enabled and price_reached:
                    ttp_state = state_manager.get_ttp_state(symbol)

                    if not ttp_state:
                        # أول مرة يصل السعر للهدف الأخير → نُفعّل TTP
                        state_manager.set_ttp_state(symbol, {
                            "activated":   True,
                            "peak_price":  current_price,
                        })
                        logger.info(f"[TTP] 🚀 تُفعّل TTP لـ {symbol} عند {current_price}")
                        audit_decision_event(
                            scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
                            setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
                            trade_id=meta.get("trade_id"), symbol=symbol,
                            stage="POSITION_MANAGEMENT", status="TTP_ACTIVATED",
                            details={"current_price": current_price, "step": idx + 1,
                                     "target": step_price, "deviation": ttp_dev},
                        )
                        break

                    # تحديث أعلى سعر
                    peak = float(ttp_state.get("peak_price", current_price))
                    if side == "BUY":
                        peak = max(peak, current_price)
                    else:
                        peak = min(peak, current_price)

                    state_manager.set_ttp_state(symbol, {"activated": True, "peak_price": peak})

                    # هل تراجع السعر بنسبة الـ deviation؟
                    if side == "BUY":
                        ttp_trigger = peak * (1 - ttp_dev)
                        ttp_fired   = current_price <= ttp_trigger
                    else:
                        ttp_trigger = peak * (1 + ttp_dev)
                        ttp_fired   = current_price >= ttp_trigger

                    if ttp_fired:
                        logger.info(f"[TTP] ✅ إطلاق TTP لـ {symbol} | القمة={peak:.5f} | الحالي={current_price:.5f}")
                        persist_bot_close_intent(
                            state_manager, symbol, "BOT_TRAILING_TAKE_PROFIT"
                        )
                        success = client.close_position(symbol, side.lower(), current_amount)
                        audit_decision_event(
                            scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
                            setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
                            trade_id=meta.get("trade_id"), symbol=symbol,
                            stage="POSITION_MANAGEMENT",
                            status="TTP_CLOSE_CONFIRMED" if success else "TTP_CLOSE_FAILED",
                            details={"peak": peak, "current_price": current_price,
                                     "trigger": ttp_trigger, "amount": current_amount}, durable=True,
                        )
                        if success:
                            step["status"] = "done"
                            state_manager.mark_tp_step_done(symbol, idx, current_price)
                            state_manager.set_ttp_state(symbol, {})
                            notifier.send_message(
                                f"🏆 <b>TTP تُنفِّذ TP{idx+1} (Trailing)</b>\n"
                                f"الزوج: <code>{symbol}</code>\n"
                                f"القمة المُسجَّلة: <code>{peak:.5f}</code>\n"
                                f"سعر الإغلاق: <code>{current_price:.5f}</code>\n"
                                f"التراجع: <code>{ttp_dev*100:.1f}%</code>"
                            )
                    break   # ننتظر TTP، لا نُكمل الخطوات

                # ── خطوة عادية (غير أخيرة أو TTP معطّل) ───────────
                if not price_reached:
                    break   # الخطوات مرتبة، إذا لم يصل لهذه لن يصل للتالية

                # ─── Step-Based Trailing (بدلاً من الإغلاق الجزئي) ───
                # لا نغلق أي جزء، فقط نحرك الـ SL لحماية الربح
                tp1_price = float(tp_steps[0]["price"])

                if idx == 0:
                    # بعد TP1: احجز 75% من مسافة TP1
                    tp1_dist   = abs(tp1_price - entry_price)
                    protect_75 = tp1_dist * getattr(Config, "STEP_TRAIL_TP1_PROTECTION_PCT", 0.75)
                    if side == "BUY":
                        new_sl = round(entry_price + protect_75, 6)
                    else:
                        new_sl = round(entry_price - protect_75, 6)
                    sl_label = "75% من TP1"

                elif idx == 1:
                    # بعد TP2: SL فوق TP1 بـ buffer صغير
                    sl_buf = getattr(Config, "STEP_TRAIL_TP2_BUFFER_PCT", 0.001)
                    if side == "BUY":
                        new_sl = round(tp1_price * (1 + sl_buf), 6)
                    else:
                        new_sl = round(tp1_price * (1 - sl_buf), 6)
                    sl_label = "فوق TP1"

                else:
                    # بعد TP3: احجز 50% من مسافة TP2 → TP3
                    tp2_price = float(tp_steps[1]["price"])
                    tp3_price = float(tp_steps[2]["price"])
                    mid_dist  = abs(tp3_price - tp2_price) * getattr(Config, "STEP_TRAIL_TP3_PROTECTION_PCT", 0.50)
                    if side == "BUY":
                        new_sl = round(tp2_price + mid_dist, 6)
                    else:
                        new_sl = round(tp2_price - mid_dist, 6)
                    sl_label = "50% بين TP2 وTP3"

                # تحقق أن الـ SL الجديد أفضل من الحالي فعلاً
                old_sl = float(meta.get("current_sl") or meta.get("sl") or 0)
                sl_improved = (
                    (side == "BUY"  and new_sl > old_sl) or
                    (side == "SELL" and new_sl < old_sl)
                )

                if sl_improved and current_amount > 0 and not sl_update_blocked:
                    replacement = _request_stop_replacement(
                        client, symbol, side, current_amount, new_sl
                    )
                    replacement_state = str(replacement.get("state") or "FAILED")
                    ok = replacement_state in {"CONFIRMED", "NOOP_PROTECTED"}
                    if ok:
                        meta.pop("sl_replace_retry_after", None)
                        new_sl = float(replacement.get("confirmed_stop"))
                        step["status"] = "done"
                        state_manager.mark_tp_step_done(symbol, idx, new_sl)
                        trailing_manager.mark_step_trail_active(symbol, new_sl, current_amount)
                        meta["current_sl"] = new_sl
                        meta["current_sl_confirmed_at"] = datetime.now(timezone.utc).isoformat()
                        meta["protection_stage"] = f"PROTECTION_STAGE_{idx + 1}"
                        state_manager.save_entry_metadata(symbol, meta)
                        logger.info(
                            f"[StepTrail] 🛡️ {symbol} TP{idx+1} → SL محمي عند {new_sl:.6f} ({sl_label})"
                        )
                        audit_decision_event(
                            scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
                            setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
                            trade_id=meta.get("trade_id"), symbol=symbol,
                            stage="POSITION_MANAGEMENT", status="TP_MILESTONE_SL_REPLACED",
                            details={"milestone": idx + 1, "target": step_price,
                                     "current_price": current_price, "old_sl": old_sl,
                                     "new_sl": new_sl, "full_amount": current_amount}, durable=True,
                        )
                        _emit_confirmed_protection_notification(
                            state_manager=state_manager, notifier=notifier,
                            symbol=symbol, meta=meta,
                            stage=f"PROTECTION_STAGE_{idx + 1}",
                            revision=f"TP_MILESTONE_{idx + 1}",
                            previous_sl=old_sl, requested_sl=new_sl,
                            confirmed_sl=new_sl,
                            message=(
                            f"🛡️ <b>مرحلة الحماية {idx+1}</b>\n"
                            f"الزوج: <code>{symbol}</code>\n"
                            f"السعر وصل: <code>{current_price:.5f}</code> (المعلم: <code>{step_price:.5f}</code>)\n"
                            f"تم رفع SL إلى {sl_label}: <code>{new_sl:.6f}</code>\n"
                            f"🔒 الكمية الكاملة محتفظ بها — لا إغلاق جزئي."
                            ),
                        )
                    elif replacement_state == "PENDING_VERIFICATION":
                        sl_update_blocked = True
                        _record_pending_stop_replacement(
                            state_manager=state_manager, meta=meta, symbol=symbol,
                            stage="POSITION_MANAGEMENT", requested_stop=new_sl,
                            old_stop=old_sl, replacement=replacement,
                            audit_status="TP_MILESTONE_SL_REPLACE_PENDING",
                        )
                    else:
                        sl_update_blocked = True
                        meta["sl_replace_retry_after"] = time.time() + float(
                            getattr(Config, "SL_REPLACE_BACKOFF_SECONDS", 30)
                        )
                        state_manager.save_entry_metadata(symbol, meta)
                        logger.critical(f"[StepTrail] فشل نقل SL بعد TP{idx+1} لـ {symbol}؛ الوقف القديم بقي قائماً")
                        audit_decision_event(
                            scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
                            setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
                            trade_id=meta.get("trade_id"), symbol=symbol,
                            stage="POSITION_MANAGEMENT", status="TP_MILESTONE_SL_REPLACE_FAILED",
                            details={"milestone": idx + 1, "target": step_price,
                                     "current_price": current_price, "old_sl": old_sl,
                                     "requested_sl": new_sl}, durable=True,
                        )
                else:
                    if not sl_improved:
                        # SL محمي مسبقاً، اعتبر الخطوة منجزة
                        step["status"] = "done"

                break   # نفذ خطوة واحدة فقط في كل دورة

            # ── حماية استباقية مزدوجة المراحل ──────────────────────────────
            # المرحلة 1 (85%): حجز 40% من مسافة TP1 — شبكة أمان مبكرة
            # المرحلة 2 (92%): رفع الحجز لـ 70%         — حماية أقوى قرب الهدف
            first_pending = next((s for s in tp_steps if s.get("status") != "done"), None)
            if first_pending and first_pending.get("position") == 1 and current_price:
                tp1_price  = float(first_pending["price"])
                current_sl = float(meta.get("current_sl") or meta.get("sl") or 0)
                tp1_dist   = abs(tp1_price - entry_price)

                # تحديد المرحلة الحالية بناءً على موقع السعر
                near_l2 = (
                    (side == "BUY"  and current_price >= entry_price + tp1_dist * 0.92) or
                    (side == "SELL" and current_price <= entry_price - tp1_dist * 0.92)
                )
                near_l1 = (
                    (side == "BUY"  and current_price >= entry_price + tp1_dist * 0.85) or
                    (side == "SELL" and current_price <= entry_price - tp1_dist * 0.85)
                )

                if near_l2:
                    protect_pct = 0.70   # المرحلة 2: حجز 70%
                    protect_label = "92%"
                elif near_l1:
                    protect_pct = 0.40   # المرحلة 1: حجز 40%
                    protect_label = "85%"
                else:
                    protect_pct = None
                    protect_label = ""

                if protect_pct is not None:
                    be_sl = (
                        round(entry_price + tp1_dist * protect_pct, 6) if side == "BUY"
                        else round(entry_price - tp1_dist * protect_pct, 6)
                    )
                    # sl_ok يمنع التراجع تلقائياً (لو M2 مطبّق لن تُخفّضه M1)
                    sl_ok = (side == "BUY" and current_sl >= be_sl) or (side == "SELL" and current_sl <= be_sl)

                    if not sl_ok and not sl_update_blocked:
                        old_sl = current_sl
                        replacement = _request_stop_replacement(
                            client, symbol, side, current_amount, be_sl
                        )
                        replacement_state = str(replacement.get("state") or "FAILED")
                        ok = replacement_state in {"CONFIRMED", "NOOP_PROTECTED"}
                        if ok:
                            meta.pop("sl_replace_retry_after", None)
                            be_sl = float(replacement.get("confirmed_stop"))
                            meta["current_sl"] = be_sl
                            meta["current_sl_confirmed_at"] = datetime.now(timezone.utc).isoformat()
                            meta["protection_stage"] = f"PREEMPTIVE_{2 if near_l2 else 1}"
                            state_manager.save_entry_metadata(symbol, meta)
                            trailing_manager.mark_step_trail_active(symbol, be_sl, current_amount)
                            logger.info(
                                f"[StepTrail] 🛡️ {symbol}: حماية M{2 if near_l2 else 1} "
                                f"({protect_label} نحو TP1) → SL={be_sl:.6f} "
                                f"({int(protect_pct*100)}% من المسافة)"
                            )
                            audit_decision_event(
                                scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
                                setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
                                trade_id=meta.get("trade_id"), symbol=symbol,
                                stage="POSITION_MANAGEMENT", status="PREEMPTIVE_SL_REPLACED",
                                details={"level": 2 if near_l2 else 1,
                                         "progress": protect_label, "current_price": current_price,
                                         "old_sl": old_sl, "new_sl": be_sl,
                                         "full_amount": current_amount}, durable=True,
                            )
                            _emit_confirmed_protection_notification(
                                state_manager=state_manager, notifier=notifier,
                                symbol=symbol, meta=meta,
                                stage=f"PREEMPTIVE_{2 if near_l2 else 1}",
                                revision=f"PREEMPTIVE_{2 if near_l2 else 1}",
                                previous_sl=old_sl, requested_sl=be_sl,
                                confirmed_sl=be_sl,
                                message=(
                                f"🛡️ <b>حماية استباقية — المرحلة {'2' if near_l2 else '1'}</b>\n"
                                f"الزوج: <code>{symbol}</code>\n"
                                f"السعر وصل <b>{protect_label}</b> نحو TP1 (<code>{tp1_price:.5f}</code>)\n"
                                f"تم رفع SL لحجز <b>{int(protect_pct*100)}%</b> من المسافة: "
                                f"<code>{be_sl:.6f}</code>\n"
                                f"🔒 الكمية الكاملة محتفظ بها — لا إغلاق جزئي."
                                ),
                            )
                        elif replacement_state == "PENDING_VERIFICATION":
                            sl_update_blocked = True
                            _record_pending_stop_replacement(
                                state_manager=state_manager, meta=meta, symbol=symbol,
                                stage="POSITION_MANAGEMENT", requested_stop=be_sl,
                                old_stop=old_sl, replacement=replacement,
                                audit_status="PREEMPTIVE_SL_REPLACE_PENDING",
                            )
                        else:
                            sl_update_blocked = True
                            meta["sl_replace_retry_after"] = time.time() + float(
                                getattr(Config, "SL_REPLACE_BACKOFF_SECONDS", 30)
                            )
                            state_manager.save_entry_metadata(symbol, meta)
                            logger.critical(f"[StepTrail] فشل الحماية الاستباقية لـ {symbol}؛ الوقف القديم بقي قائماً")
                            audit_decision_event(
                                scan_id=meta.get("scan_id"), decision_id=meta.get("decision_id"),
                                setup_id=meta.get("setup_thesis_id"), position_id=meta.get("trade_id"),
                                trade_id=meta.get("trade_id"), symbol=symbol,
                                stage="POSITION_MANAGEMENT", status="PREEMPTIVE_SL_REPLACE_FAILED",
                                details={"level": 2 if near_l2 else 1,
                                         "progress": protect_label, "current_price": current_price,
                                         "old_sl": old_sl, "requested_sl": be_sl}, durable=True,
                            )



        except Exception as e:
            logger.error(f"[TPLadder] خطأ في معالجة {symbol}: {e}")


def monitor_active_trades(client, state_manager, trailing_manager, alert_manager, notifier, last_check_time):
    """
    مراقبة الصفقات المفتوحة في الخلفية للتحقق مما إذا كانت قد أغلقت، تحديث التريلينغ، وتنبيهات الاقتراب.
    """
    if not getattr(Config, "ACCOUNT_MANAGEMENT_ENABLED", True):
        return last_check_time

    # Runs on the account-owning runtime even when there are no active
    # positions. It is read-only and never creates/cancels orders.
    monitor_post_exit_observations(client, state_manager)

    active_symbols = state_manager.get_active_symbols()
    if not active_symbols:
        return int(time.time() * 1000)
        
    try:
        # 1. جلب كميات الصفقات الحقيقية من المنصة مباشرة
        positions = client.exchange.fetch_positions() # لا نمرر الرموز لتفادي خطأ CCXT في الفلترة
        live_positions_by_symbol = {}
        for pos in positions:
            sym = pos.get('symbol', '').split(':')[0] # تحويل BTC/USDT:USDT إلى BTC/USDT ليتطابق مع النظام
            info = pos.get('info', {}) or {}
            amt = float(info.get('positionAmt', 0) or 0)
            if amt != 0:
                raw_position_side = str(info.get('positionSide') or 'BOTH').upper()
                effective_side = raw_position_side
                if effective_side == 'BOTH':
                    effective_side = 'LONG' if amt > 0 else 'SHORT'
                live_positions_by_symbol.setdefault(sym, []).append({
                    'amount': abs(amt),
                    'position_side': effective_side,
                })
            
        current_time = int(time.time() * 1000)
        
        for sym in list(active_symbols):
            current_price = None
            meta = state_manager.get_entry_metadata(sym)
            if meta.get('management_blocked'):
                logger.critical(
                    f"[{sym}] الإدارة الآلية متوقفة بسبب تعارض LONG/SHORT خارجي؛ "
                    "يلزم فصل المركزين أو إغلاق أحدهما يدوياً."
                )
                continue
            expected_side = str(meta.get('position_side') or '').upper()
            if not expected_side:
                raw_side = str(meta.get('side') or '').upper()
                expected_side = 'LONG' if raw_side in ('BUY', 'LONG') else (
                    'SHORT' if raw_side in ('SELL', 'SHORT') else ''
                )
            symbol_positions = live_positions_by_symbol.get(sym, [])
            matching_positions = [
                p for p in symbol_positions
                if not expected_side or p['position_side'] == expected_side
            ]
            if len(matching_positions) > 1:
                logger.critical(
                    f"[{sym}] وُجد أكثر من مركز مطابق ({expected_side})؛ "
                    "تم تخطي الإدارة الآلية لهذه الدورة لمنع إدارة كمية خاطئة."
                )
                continue
            live_amount = float(matching_positions[0]['amount']) if matching_positions else 0.0
            
            # 2. تحديث الستوب المتحرك والتنفيذ بالقوة (Active Execution)
            if live_amount > 0:
                current_price = client.get_current_price(sym)
                if current_price:
                    # أ) تحديث الستوب المتحرك
                    if trailing_manager.has_trade(sym):
                        previous_confirmed_sl = float(
                            trailing_manager.get_current_sl(sym)
                            or meta.get("current_sl") or meta.get("initial_sl")
                            or meta.get("sl") or 0.0
                        )
                        new_sl = trailing_manager.update(sym, current_price, client)
                        if new_sl:
                            logger.info(f"تم تفعيل الستوب المتحرك للزوج {sym} عند السعر {new_sl}")
                            updated_meta = state_manager.get_entry_metadata(sym) or meta
                            trade_info = trailing_manager.get_trade_info(sym) or {}
                            # A breach-triggered emergency close unregisters the trade;
                            # only a still-registered trade proves an SL replacement.
                            if trailing_manager.has_trade(sym):
                                stage = str(
                                    updated_meta.get("protection_stage")
                                    or trade_info.get("protection_stage")
                                    or "TRAILING_STOP"
                                )
                                confirmed_at = str(
                                    updated_meta.get("current_sl_confirmed_at")
                                    or trade_info.get("current_sl_confirmed_at") or ""
                                )
                                _emit_confirmed_protection_notification(
                                    state_manager=state_manager,
                                    notifier=notifier,
                                    symbol=sym,
                                    meta=updated_meta,
                                    stage=stage,
                                    revision=f"{stage}:{confirmed_at}:{float(new_sl):.12g}",
                                    previous_sl=previous_confirmed_sl,
                                    requested_sl=float(new_sl),
                                    confirmed_sl=float(new_sl),
                                    message=(
                                        f"🛡️ <b>تحديث حماية مؤكد</b>\n"
                                        f"الزوج: <code>{sym}</code>\n"
                                        f"المرحلة: <code>{stage}</code>\n"
                                        f"SL السابق: <code>{previous_confirmed_sl:.8g}</code>\n"
                                        f"SL المؤكد: <code>{float(new_sl):.8g}</code>"
                                    ),
                                )

                    # ب) التحقق من إشعارات الاقتراب
                    if meta:
                        trade_info = {
                            "trade_id": f"{sym}_active",
                            "side": meta.get("side", ""),
                            "entry_price": meta.get("entry_price", 0),
                            "initial_stop_loss": meta.get("sl", 0),
                            "stop_loss": trailing_manager.get_current_sl(sym) or meta.get("sl"),
                            "take_profit": meta.get("tp", 0)
                        }
                        atr = meta.get("atr", 0)
                        if atr > 0:
                            alert_manager.check_alerts(sym, trade_info, current_price, atr)

                    # ب) مسار احتياطي للإغلاق إذا غابت حماية Binance
                    if meta:
                        side = meta.get("side", "").upper()
                        current_sl = trailing_manager.get_current_sl(sym) or meta.get("current_sl") or meta.get("sl")
                        tp = meta.get("tp")
                        amt = live_amount

                        force_close_reason = None

                        sl_timeout_enabled = (
                            getattr(Config, "SL_TIMEOUT_ENABLED", False)
                            and meta.get("protection_status") != "PROTECTED"
                        )
                        sl_timeout_sec     = getattr(Config, "SL_TIMEOUT_SECONDS", 60)

                        sl_breached = (
                            (side in ["BUY",  "LONG"]  and current_sl and current_price <= current_sl) or
                            (side in ["SELL", "SHORT"] and current_sl and current_price >= current_sl)
                        )

                        tp_steps = meta.get("tp_steps", [])
                        tp_no_ladder = (
                            not tp_steps and
                            (
                                (side in ["BUY",  "LONG"]  and tp and current_price >= tp) or
                                (side in ["SELL", "SHORT"] and tp and current_price <= tp)
                            )
                        )

                        if sl_breached:
                            if sl_timeout_enabled:
                                # ── SL Timeout Logic (3Commas-style) ──────────
                                timeout_state = state_manager.get_sl_timeout_state(sym)

                                if not timeout_state:
                                    # أول مرة نلمس الـ SL → نبدأ المؤقت
                                    state_manager.set_sl_timeout_state(sym, {
                                        "triggered_at": time.time(),
                                        "sl_price":     current_sl,
                                    })
                                    logger.info(
                                        f"[SL Timeout] ⏳ {sym}: السعر لمس SL={current_sl:.5f}. "
                                        f"بدء مؤقت {sl_timeout_sec}s..."
                                    )
                                else:
                                    elapsed = time.time() - float(timeout_state.get("triggered_at", 0))

                                    if (side in ["BUY", "LONG"] and current_price > current_sl) or (side in ["SELL", "SHORT"] and current_price < current_sl):
                                        # عاد السعر فوق الـ SL → نلغي المؤقت
                                        state_manager.set_sl_timeout_state(sym, {})
                                        logger.info(f"[SL Timeout] ✅ {sym}: السعر عاد لمنطقة الأمان. تم إلغاء المؤقت.")
                                    elif elapsed >= sl_timeout_sec:
                                        # انتهت المهلة والسعر لا يزال تحت SL → نغلق
                                        force_close_reason = f"وقف الخسارة (SL Timeout {sl_timeout_sec}s)"
                                        state_manager.set_sl_timeout_state(sym, {})
                            else:
                                force_close_reason = "وقف الخسارة (SL)"
                        else:
                            # السعر آمن → نمسح أي timeout كان جارياً
                            if state_manager.get_sl_timeout_state(sym):
                                state_manager.set_sl_timeout_state(sym, {})

                        if tp_no_ladder:
                            force_close_reason = "الهدف (TP)"

                        if force_close_reason:
                            logger.critical(
                                f"🚀 [Active Execution] السعر اللحظي ({current_price}) لمس {force_close_reason} "
                                f"لصفقة {sym}! جاري الإغلاق الفوري بالقوة..."
                            )
                            close_code = (
                                "BOT_STOP_LOSS_FALLBACK"
                                if "وقف الخسارة" in force_close_reason
                                else "BOT_TAKE_PROFIT_FALLBACK"
                            )
                            persist_bot_close_intent(state_manager, sym, close_code)
                            success = client.close_position(sym, side.lower(), amt)
                            if success:
                                live_amount = 0.0
                                time.sleep(1.5)

            # 3. التحقق مما إذا كانت الصفقة قد أغلقت (سواء من باينانس أو بتدخلنا القسري أعلاه)
            if live_amount == 0:
                if not _close_persistence_retry_due(meta):
                    logger.debug(
                        f"[{sym}] تسجيل الإغلاق في فترة انتظار إعادة المحاولة؛ "
                        "لن يُعاد استدعاء Binance/SQLite في هذه الدورة."
                    )
                    continue
                logger.info(f"⚡ تم رصد إغلاق الصفقة للزوج {sym} لحظياً! جاري جلب تفاصيل الربح/الخسارة...")
                # تأخير بسيط لضمان تحديث سجل التداولات في باينانس
                time.sleep(2.0)
                alerts = client.get_recent_sl_tp_fills(
                    sym,
                    since_ms=None,
                    entry_meta=meta,
                ) # لا نعتمد على الوقت هنا لضمان عدم ضياع الإشعار
                
                if alerts:
                    # نأخذ آخر أمر إغلاق
                    alert = alerts[-1]
                else:
                    # Position=0 proves that the position disappeared, but it
                    # does not prove the close fill, reason, fees, funding, or
                    # final price.  The former synthetic fallback persisted an
                    # UNKNOWN/MANUAL close and sent it to Telegram; seconds
                    # later the real STOP fill arrived and produced a second,
                    # contradictory notification.  Keep ownership and retry
                    # the idempotent GET until Binance provides the fill.
                    pending_meta = state_manager.get_entry_metadata(sym) or meta or {}
                    now_ts = time.time()
                    pending_meta.setdefault("close_fill_pending_since", now_ts)
                    pending_meta["close_fill_last_checked_at"] = now_ts
                    pending_meta["close_fill_retry_count"] = int(
                        pending_meta.get("close_fill_retry_count", 0) or 0
                    ) + 1
                    pending_meta["close_persistence_retry_at"] = now_ts + min(
                        30.0,
                        2.0 ** min(pending_meta["close_fill_retry_count"], 4),
                    )
                    state_manager.save_entry_metadata(sym, pending_meta)
                    logger.warning(
                        f"[CloseReconciliation] {sym}: المركز أصبح صفراً لكن "
                        "Binance لم يعرض تعبئة الإغلاق بعد؛ ستبقى الحالة محفوظة "
                        "وسيُعاد GET لاحقاً دون إنشاء إغلاق أو إشعار تخميني."
                    )
                    continue

                exit_details = None
                try:
                    exit_details = handle_trade_close(
                        alert, state_manager, client=client, notifier=notifier
                    )
                    if exit_details == CLOSE_ALREADY_PROCESSING:
                        continue
                except Exception as e:
                    logger.error(f"خطأ أثناء تسجيل إغلاق الصفقة في مراقب الخلفية: {e}")
                    
                if exit_details:
                    notifier.send_pnl_alert(
                        alert["symbol"],
                        alert["type"],
                        exit_details.get("exit_price", alert.get("price", 0.0)),
                        exit_details.get("pnl", alert.get("pnl", 0.0)),
                        exit_details.get("pnl_pct", 0.0),
                        wallet_pnl_pct=exit_details.get("wallet_pnl_pct", 0.0),
                        pnl_ref_50=exit_details.get("pnl_ref_50", 0.0),
                        reference_balance=exit_details.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0)),
                    )
                logger.info(f"إشعار فوري: إغلاق {alert['symbol']} | PnL: {alert['pnl']}")
                    
                # لا ننظف الحالة إلا بعد نجاح التسجيل الدائم.
                if exit_details:
                    if sym in active_symbols:
                        active_symbols.discard(sym)
                        state_manager.remove_active_symbol(sym)
                    if trailing_manager.has_trade(sym):
                        trailing_manager.unregister_trade(sym)

        return current_time
    except Exception as e:
        logger.error(f"خطأ في مراقبة الصفقات النشطة لحظياً: {e}")
        return last_check_time

def compute_next_candle_wakeup(interval_minutes: int, buffer_seconds: int = 8) -> float:
    """
    يحسب عدد الثواني حتى الاستيقاظ الدقيق بعد إغلاق الشمعة.
    مستوحى من bot_runner.ts: يستيقظ بعد 8 ثوانٍ من إغلاق الشمعة.
    
    مثال: interval=15 → يستيقظ عند 00:08، 15:08، 30:08، 45:08
    """
    return seconds_to_next_candle(
        interval_seconds=interval_minutes * 60,
        buffer_seconds=buffer_seconds,
    )

import threading
import queue

_force_scan_event  = threading.Event()
_shutdown_event    = threading.Event()
_scan_lock         = threading.Lock()   # يمنع تشغيل مسحين في نفس الوقت
_shutdown_reason   = "NORMAL"


def _request_shutdown(reason):
    global _shutdown_reason
    if not _shutdown_event.is_set():
        _shutdown_reason = str(reason or "UNKNOWN")
    _shutdown_event.set()

def _scanner_thread(
    client, hybrid_strategy, risk_manager, trailing_manager,
    state_manager, notifier, news_engine, ai_engine, ta_engine,
    social_engine, universe_filter, derivatives_filter,
    active_strategies
):
    last_scan_time = int(time.time() * 1000)

    while not _shutdown_event.is_set():
        scan_interval = int(
            state_manager.get(
                "scan_interval_seconds",
                getattr(Config, "SCAN_INTERVAL_SECONDS", 900)
            )
        )
        scan_interval = max(
            getattr(Config, "MIN_SCAN_INTERVAL_SECONDS", 300),
            min(scan_interval, getattr(Config, "MAX_SCAN_INTERVAL_SECONDS", 3600))
        )
        interval_minutes = scan_interval // 60
        candle_wait      = compute_next_candle_wakeup(interval_minutes, buffer_seconds=8)

        logger.info(
            f"[Scanner] انتظار إغلاق الشمعة التالية ({candle_wait:.0f}s)..."
        )

        triggered_by_force = _force_scan_event.wait(timeout=candle_wait)
        if _shutdown_event.is_set():
            break
        if triggered_by_force:
            _force_scan_event.clear()
            state_manager.set("force_scan_now", False)
            logger.info("⚡ [Scanner] مسح فوري مطلوب من تيليجرام.")

        if not _scan_lock.acquire(blocking=False):
            logger.warning("[Scanner] مسح آخر قيد التشغيل — تم التخطي.")
            continue

        try:
            if not check_network_connection(client):
                logger.warning("[Scanner] الشبكة معطلة — تأجيل المسح.")
                audit_decision_event(
                    stage="SCAN_SCHEDULER",
                    status="SKIPPED_NETWORK_UNAVAILABLE",
                    details={"retry_delay_seconds": 10},
                )
                time.sleep(10)
                continue

            trades_before = len(state_manager.get_active_symbols())
            log_memory_milestone("SCAN_START", force=True)

            run_bot_iteration(
                client, hybrid_strategy, risk_manager, trailing_manager,
                state_manager, notifier, news_engine, ai_engine, ta_engine,
                social_engine, universe_filter, derivatives_filter,
                active_strategies, last_scan_time
            )

            last_scan_time = int(time.time() * 1000)
            state_manager.set(
                "last_scan_completed_at", datetime.now(timezone.utc).isoformat()
            )
            log_memory_milestone("AFTER_SCAN", force=True)
            state_manager.flush_if_dirty()

            if triggered_by_force:
                trades_after = len(state_manager.get_active_symbols())
                if trades_after == trades_before:
                    notifier.send_message(
                        "✅ <b>اكتمل المسح الفوري</b>\n"
                        "تم فحص العملات ولا توجد شروط دخول محققة حالياً.\n"
                        "البوت يعود للمراقبة. 🔍"
                    )
        except SystemExit:
            logger.critical("[Scanner] Kill Switch نشط — إيقاف البوت الكامل.")
            _request_shutdown("KILL_SWITCH")
            return
        except Exception as e:
            logger.error(f"[Scanner] خطأ: {e}")
            audit_decision_event(
                stage="SCAN_SCHEDULER",
                status="UNHANDLED_ERROR",
                details={"error_type": type(e).__name__, "message": str(e)[:500]},
                durable=True,
            )
        finally:
            _scan_lock.release()


def _monitor_thread(
    client, state_manager, notifier, trailing_manager,
    risk_manager, alert_manager, ta_engine, api_status_monitor
):
    monitor_last_time  = int(time.time() * 1000)
    last_health_check  = time.time()
    last_risk_check    = 0.0
    MONITOR_TICK       = getattr(Config, "MONITOR_TICK_SECONDS", 2)

    while not _shutdown_event.is_set():
        loop_start = time.time()
        try:
            if not check_network_connection(client):
                if state_manager.get("risk_equity_status") != "UNKNOWN":
                    state_manager.set("risk_equity_status", "UNKNOWN")
                time.sleep(10)
                continue

            if time.time() - last_risk_check >= 30:
                enforce_daily_loss_limit(client, state_manager, notifier)
                last_risk_check = time.time()

            # المراكز الحقيقية لها أولوية زمنية على البحث/الأوامر الافتراضية.
            # أي بطء REST في مراقبة المرشحات لا يجوز أن يؤخر رصد SL/TP اليدوي.
            recover_protection = getattr(
                client, "recover_pending_protection_replacements", None
            )
            if callable(recover_protection):
                protection_sagas = recover_protection()
                if protection_sagas.get("status") != "OK":
                    logger.critical(
                        "[ProtectionRecovery] استبدال SL ما زال قيد الحسم: "
                        f"{protection_sagas.get('pending')}"
                    )
                # A durable saga may become verified on this monitor tick,
                # after the original strategy caller already returned.  Keep
                # the in-memory trailing manager aligned with the
                # exchange-confirmed StateManager value before it evaluates a
                # new candidate.  Regression is rejected inside the manager.
                sync_confirmed = getattr(trailing_manager, "sync_confirmed_stop", None)
                if callable(sync_confirmed):
                    for protected_symbol in state_manager.get_active_symbols():
                        protected_meta = state_manager.get_entry_metadata(protected_symbol) or {}
                        confirmed_stop = protected_meta.get("current_sl")
                        if confirmed_stop:
                            sync_confirmed(
                                protected_symbol,
                                confirmed_stop,
                                protected_meta.get("current_sl_confirmed_at"),
                            )
            monitor_last_time = monitor_active_trades(
                client, state_manager, trailing_manager,
                alert_manager, notifier, monitor_last_time
            )

            manage_tp_ladder(
                client, state_manager, trailing_manager, notifier
            )
            recover_pending_order_intents(
                client, state_manager, notifier, trailing_manager, risk_manager
            )
            monitor_virtual_orders(
                client, state_manager, notifier,
                trailing_manager, risk_manager, ta_engine=ta_engine
            )

        except Exception as e:
            logger.error(f"[Monitor] خطأ: {e}")

        if getattr(Config, "API_HEALTH_CHECK_ENABLED", True):
            interval_sec = getattr(Config, "API_HEALTH_CHECK_INTERVAL_MINUTES", 60) * 60
            if time.time() - last_health_check >= interval_sec:
                last_health_check = time.time()
                threading.Thread(target=_run_api_health_check, args=(api_status_monitor,), daemon=True).start()

        state_manager.flush_if_dirty()

        elapsed = time.time() - loop_start
        sleep_for = max(0.1, MONITOR_TICK - elapsed)
        _shutdown_event.wait(timeout=sleep_for)

def _heartbeat_thread(client, state_manager, notifier):
    last_heartbeat_time = 0

    while not _shutdown_event.is_set():
        try:
            current_timestamp = time.time()
            if current_timestamp - last_heartbeat_time > 7200:
                mode_str     = "TESTNET" if getattr(Config, "USE_TESTNET", False) else "LIVE"
                active_count = len(state_manager.get_active_symbols())
                max_trades = state_manager.get_max_open_trades()
                notifier.send_message(
                    f"✅ <b>Bot is alive & running</b>\n"
                    f"Mode: <code>{mode_str}</code>\n"
                    f"Open trades: {active_count}/{max_trades}\n"
                    f"State file: <code>{os.getenv('STATE_FILE', 'bot_state.json')}</code>"
                )
                last_heartbeat_time = current_timestamp

            maybe_send_periodic_reports(notifier, state_manager, client)

        except Exception as e:
            logger.error(f"[Heartbeat] خطأ: {e}")

        _shutdown_event.wait(timeout=60)

def _run_api_health_check(api_status_monitor):
    try:
        logger.info("🧪 بدء فحص دوري لكل خدمات API...")
        payload = api_status_monitor.run_full_check()
        api_status_monitor.notify_if_needed(payload)
    except Exception as e:
        logger.error(f"فشل الفحص الدوري لخدمات API: {e}")

def validate_live_trading_confirmation():
    """يرفض وضع LIVE قبل إنشاء أي عميل أو اتصال خارجي."""
    if (
        not Config.USE_TESTNET
        and os.getenv("LIVE_TRADING_CONFIRMATION") != "I_ACCEPT_REAL_MONEY_RISK"
    ):
        raise RuntimeError(
            "Live trading blocked: LIVE_TRADING_CONFIRMATION is missing or incorrect."
        )
    return True


def validate_telegram_proposal_demo_mode():
    """Reject an unsafe bridge configuration before ExchangeClient exists."""
    if not Config.TELEGRAM_PROPOSAL_DEMO_MODE:
        return {"enabled": False}
    if not Config.USE_TESTNET:
        raise RuntimeError("TELEGRAM_PROPOSAL_DEMO_MODE_REQUIRES_USE_TESTNET_TRUE")
    if not Config.BINANCE_API_KEY or not Config.BINANCE_API_SECRET:
        raise RuntimeError("TELEGRAM_PROPOSAL_DEMO_CREDENTIALS_MISSING")
    if int(Config.LEVERAGE) != 5:
        raise RuntimeError("TELEGRAM_PROPOSAL_DEMO_REQUIRES_5X_LEVERAGE")
    return {
        "enabled": True,
        "execution_enabled": bool(Config.DEMO_EXECUTION_ENABLED),
        "effective_position_cap": (
            min(
                int(Config.TESTNET_MAX_OPEN_TRADES),
                int(Config.HARD_MAX_OPEN_TRADES),
            )
            if Config.DEMO_EXECUTION_ENABLED
            else 0
        ),
        "telegram_polling_enabled": bool(Config.TELEGRAM_POLLING_ENABLED),
        "telegram_outbound_enabled": bool(Config.TELEGRAM_OUTBOUND_ENABLED),
        "account_management_enabled": bool(Config.ACCOUNT_MANAGEMENT_ENABLED),
    }


def _release_writer_lease(writer_lease):
    """Release the optional mutation lease during active or passive shutdown."""
    if writer_lease is not None:
        writer_lease.release()


def main():
    # يجب أن يسبق هذا الحارس ExchangeClient وWebSocket وأي خيط خلفي. وجوده
    # لاحقاً فقط يمنع الأوامر، لكنه يسمح ببدء اتصالات LIVE قبل رفض البيئة.
    try:
        validate_live_trading_confirmation()
        demo_mode = validate_telegram_proposal_demo_mode()
    except Exception as exc:
        logger.critical(f"🛑 Startup Safety Check Failed: {exc}")
        raise

    runtime_session = begin_runtime_session()
    resource_monitor = start_resource_monitor()
    log_memory_milestone("PROCESS_START", force=True)

    logger.info("=========================================")
    logger.info("🤖 بدء التشغيل الآلي المستمر (Autonomous Loop) 🤖")
    logger.info("سيعمل البوت 24/7 ويقوم بالمسح كل 15 دقيقة.")
    logger.info("=========================================")
    
    last_scan_time = None
    
    client         = ExchangeClient()
    if demo_mode.get("enabled"):
        runtime_identity = telegram_proposal_runtime_identity()
        logger.info(
            "TELEGRAM_PROPOSAL_DEMO_STARTUP "
            f"mode={demo_mode} endpoint={client._environment_proof} "
            f"runtime_commit={runtime_identity['runtime_commit']} "
            f"runtime_build_hash={runtime_identity['runtime_build_hash']}"
        )
    log_memory_milestone("AFTER_BINANCE_INIT", force=True)
    
    global ws_manager, symbol_cache, signal_dedup, _candle_stream
    from utils.websocket_manager import FuturesWebSocketManager
    from utils.symbol_cache import SymbolInfoCache
    ws_manager    = FuturesWebSocketManager(use_testnet=Config.USE_TESTNET)
    _candle_stream = SharedCandleStream(
        interval="15m",
        max_candles=Config.DECISION_CANDLE_HISTORY,
        use_testnet=Config.USE_TESTNET,
    )
    symbol_cache  = SymbolInfoCache(client)
    ws_manager.start()
    _candle_stream.start()
    state_manager  = StateManager()
    client.bind_state_manager(state_manager)
    record_run_start(state_manager, runtime_session)
    log_memory_milestone("AFTER_STATE_RECOVERY", force=True)
    
    # ── حماية الحالة عند إيقاف الحاوية (Railway redeploy / OOM / SIGTERM) ──
    import atexit, signal as _signal
    atexit.register(lambda: state_manager.save_state(force=True))
    def _handle_term(sig, frame):
        logger.info(f"🛑 [Signal {sig}] جارٍ إيقاف استقبال أعمال جديدة وحفظ الحالة...")
        _request_shutdown(f"SIGNAL_{sig}")
        state_manager.save_state(force=True)
    _signal.signal(_signal.SIGTERM, _handle_term)
    _signal.signal(_signal.SIGINT, _handle_term)
    
    # تهيئة نظام منع التكرار
    signal_dedup = SignalDeduplicator(state_manager)
    logger.info("✅ [Dedup] تم تهيئة نظام منع التكرار بنجاح")
    
    notifier       = TelegramNotifier()
    # تسجيل notifier كمتغير عام للوصول إليه من handle_trade_close (التغذية الراجعة)
    import builtins
    builtins._global_notifier = notifier
    builtins._global_ws_manager = ws_manager
    builtins._global_candle_stream = _candle_stream
    
    api_status_monitor = APIStatusMonitor(
        client=client,
        state_manager=state_manager,
        notifier=notifier
    )
    last_api_health_check = 0
    
    # [فحوصات الأمان المبدئية - Startup Safety Checks]
    try:
        state_file_path = os.getenv(
            "STATE_FILE", os.path.join(Config.DATA_DIR, "bot_state.json")
        )
        dir_path = os.path.dirname(os.path.abspath(state_file_path)) or "."
        if not os.access(dir_path, os.W_OK):
            raise RuntimeError(f"Volume path not writable: {dir_path}")
    except Exception as e:
        logger.critical(f"🛑 Startup Safety Check Failed: {e}")
        notifier.send_message(f"🛑 <b>فشل في فحص بيئة التشغيل:</b>\n{e}\nتم إيقاف البوت لحماية النظام.")
        raise
        
    hybrid_strategy= HybridStrategy()

    # ── تهيئة قاعدة بيانات SQLite وترحيل CSV القديم ──────────────
    try:
        init_db()
        init_forensics_table()
        csv_path = getattr(Config, "TESTNET_TRADES_LOG", "/app/data/testnet_trades_log.csv")
        migrated = migrate_from_csv(csv_path)
        if migrated > 0:
            notifier.send_message(
                f"🗄️ <b>ترحيل البيانات اكتمل</b>\n"
                f"تم نقل <code>{migrated}</code> صفقة من CSV إلى SQLite بنجاح."
            )
    except Exception as e:
        logger.error(f"[DB] خطأ أثناء تهيئة/ترحيل قاعدة البيانات: {e}")

    risk_manager   = RiskManager()
    trailing_manager = TrailingStopManager(state_manager=state_manager)
    alert_manager = TradeApproachAlertManager(state_manager=state_manager, notifier=notifier)
    builtins._global_alert_manager = alert_manager   # يُتيح الوصول من handle_trade_close

    news_engine    = NewsEngine()
    # مزودات AI كلها Remote APIs. تحميل SDKs في كل تشغيل كان يستهلك عشرات MB
    # حتى عندما تكون ميزات AI الآلية معطلة؛ الوكيل يحملها عند أول طلب يدوي فقط.
    ai_engine      = LazyAIEngine()
    builtins._global_ai_engine = ai_engine   # يتيح الوصول من handle_trade_close للنقد بعد الإغلاق
    ta_engine      = TAEngine()
    
    # ── Strategy Manager: الاستراتيجيات التجريبية معطلة لحين Backtest مستقل ──
    active_strategies = [
        SMCRSIStrategy(),
    ]
    social_engine  = SocialEngine()
    universe_filter = UniverseFilter(client)
    derivatives_filter = DerivativesRiskFilter(state_manager=state_manager)
    log_memory_milestone("AFTER_COMPONENT_INIT", force=True)

    # إصلاح: تهيئة الرصيد المرجعي فور بدء التشغيل لمنع Kill Switch الزائف
    try:
        startup_equity, _ = get_wallet_equity_snapshot(client, state_manager)
        if startup_equity and startup_equity > 0:
            state_manager.update_daily_balance(startup_equity)
            logger.info(f"[Startup] ✅ تم تهيئة الرصيد المرجعي: {startup_equity:.2f} USDT")
    except Exception as _e:
        logger.warning(f"[Startup] فشل تهيئة الرصيد المرجعي: {_e}")
    
    # تشغيل خادم Mini App (Flask) على port 8080 في خيط daemon منفصل
    webapp_server = None
    try:
        webapp_port = int(os.getenv("PORT", "8080"))
        webapp_server = start_webapp(
            state_manager=state_manager, client=client, port=webapp_port
        )
    except Exception as e:
        logger.warning(f"[WebApp] تعذر تشغيل خادم Mini App: {e}")

    # قفل على الـVolume، لا على ذاكرة العملية. نبدأ HTTP أولاً كي يستطيع Railway
    # تفعيل النسخة الجديدة ثم إرسال SIGTERM للقديمة التي تحمل القفل.
    writer_lease = None
    if Config.ACCOUNT_MANAGEMENT_ENABLED or Config.TELEGRAM_POLLING_ENABLED:
        writer_lease = SingleWriterLease(owner_id=_PROCESS_INSTANCE_ID)
        lease_timeout = float(os.getenv("BOT_SINGLE_WRITER_WAIT_SECONDS", "120") or 120)
        if not writer_lease.acquire(timeout_seconds=lease_timeout):
            logger.critical(
                "🛑 تعذر امتلاك Single-Writer lease؛ توجد نسخة أخرى تدير الحساب."
            )
            raise RuntimeError("another bot instance owns the trading writer lease")
        atexit.register(writer_lease.release)
        logger.info("[SingleWriter] ✅ هذه العملية هي الكاتب الوحيد المصرح للتداول.")
    else:
        logger.info(
            "[SingleWriter] passive runtime; writer lease not requested and "
            "account mutation remains disabled."
        )

    # Telegram polling نفسه يجب أن يكون خلف القفل حتى لا تتصارع نسختان على
    # getUpdates أو تنفذا أوامر إدارة متزامنة.
    if Config.TELEGRAM_POLLING_ENABLED:
        notifier.start_polling(
            client, state_manager, ai_engine, ta_engine, news_engine, social_engine
        )
    else:
        logger.info(
            "[Telegram] polling disabled; outbound=%s account_management=%s.",
            Config.TELEGRAM_OUTBOUND_ENABLED,
            Config.ACCOUNT_MANAGEMENT_ENABLED,
        )
    resource_monitor.set_notifier(notifier)

    if not Config.ACCOUNT_MANAGEMENT_ENABLED:
        # A passive companion service may read public market data, but it must
        # not adopt account positions, rebuild trailing state, create
        # protection or emit alarming reconciliation messages for the primary
        # Telegram runtime's account.
        state_manager.set("recovery_complete", False)
        logger.info(
            "[StartupRecovery] skipped: account_management=false; "
            "no account state was adopted or modified."
        )
        audit_decision_event(
            stage="STARTUP_RECOVERY",
            status="SKIPPED_PASSIVE_RUNTIME",
            details={
                "account_management_enabled": False,
                "order_calls_possible": False,
            },
            durable=True,
        )
    else:
        logger.info("جاري مزامنة الصفقات المفتوحة مع منصة باينانس...")
        try:
            startup_reconcile = reconcile_positions(client, state_manager)
            if startup_reconcile.get('status') != 'OK':
                raise RuntimeError(startup_reconcile.get('error', 'unknown reconciliation error'))
            live_symbols = set(startup_reconcile.get('exchange_active', set()))
            all_active = state_manager.get_active_symbols()
            if state_manager.is_state_recovery_blocked():
                recovery_status = state_manager.get_state_recovery_status()
                raise RuntimeError(
                    "local state is corrupt and no valid backup exists; "
                    f"source={recovery_status.get('source')}"
                )
            replacement_recovery = {"status": "BLOCKED", "pending": []}
            for _ in range(10):
                replacement_recovery = client.recover_pending_protection_replacements()
                if replacement_recovery.get("status") == "OK":
                    break
                time.sleep(2)
            if replacement_recovery.get("status") != "OK":
                raise RuntimeError(
                    "pending SL replacement could not be verified at startup: "
                    f"{replacement_recovery.get('pending')}"
                )
            protection_recovery = verify_recovered_position_protection(
                client, state_manager, live_symbols
            )
            if protection_recovery.get("status") != "OK":
                external = protection_recovery.get("external_unmanaged") or []
                if external:
                    notifier.send_message(
                        "⚠️ <b>مركز خارجي غير مُدار — التداول الجديد متوقف</b>\n"
                        f"الأزواج: <code>{', '.join(external)}</code>\n"
                        "أغلقه يدوياً أو راجعه قبل استئناف البوت."
                    )
                raise RuntimeError(
                    "recovered position protection not verified: "
                    f"errors={protection_recovery.get('errors')} external={external}"
                )

            # ── مزامنة trailing_manager مع الصفقات الحية (TS-3 fix) ──────
            stale_trailing = set(trailing_manager._trades.keys()) - live_symbols
            for sym in stale_trailing:
                trailing_manager.unregister_trade(sym)
                logger.info(f"[Startup] تم إزالة صفقة ميتة من Trailing: {sym}")
            if stale_trailing:
                logger.info(f"[Startup] ✅ تم تنظيف {len(stale_trailing)} صفقة ميتة من trailing_manager.")

            trailing_recovery = restore_recovered_trailing_tracking(
                client, state_manager, trailing_manager, live_symbols
            )
            if trailing_recovery.get("status") != "OK":
                raise RuntimeError(
                    "recovered trailing state not verified: "
                    f"{trailing_recovery.get('errors')}"
                )

            logger.info(f"✅ اكتملت المزامنة. الحالة المتتبعة: {all_active if all_active else 'لا يوجد'}")
            state_manager.set("recovery_complete", True)
            state_manager.set("last_successful_reconciliation_at", datetime.now(timezone.utc).isoformat())
            audit_decision_event(
                stage="STARTUP_RECOVERY",
                status="RECOVERY_COMPLETE",
                details={
                    "exchange_active": sorted(live_symbols),
                    "protection_verified": protection_recovery.get("verified", []),
                    "external_unmanaged": protection_recovery.get("external_unmanaged", []),
                    "trailing_restored": trailing_recovery.get("restored", []),
                    "unexpected_restart": runtime_session.get("unexpected_restart", False),
                },
                durable=True,
            )
        except Exception as e:
            state_manager.set("recovery_complete", False)
            logger.critical(
                f"🛑 فشل مزامنة الصفقات مع باينانس؛ سيبقى فتح الصفقات معطلاً حتى "
                f"تنجح مزامنة لاحقة: {e}"
            )
            audit_decision_event(
                stage="STARTUP_RECOVERY",
                status="RECOVERY_BLOCKED",
                details={"error": str(e)},
                durable=True,
            )

    if runtime_session.get("unexpected_restart"):
        recovery_label = (
            "COMPLETE" if state_manager.get("recovery_complete", False) else "BLOCKED"
        )
        notifier.send_message(
            "⚠️ <b>اكتُشف Restart غير متوقع</b>\n"
            "السبب غير مثبت (قد يكون OOM أو process kill، وليس بالضرورة).\n"
            f"Run: <code>{runtime_session['current']['run_id'][:12]}</code>\n"
            f"Recovery: <code>{recovery_label}</code>"
        )
        
    # ── مراقبة Telegram لأوامر force_scan (تبقى في main thread) ──
    logger.info("🚀 [Main] بدء تشغيل خيوط المعمارية الجديدة...")

    # تعريف الخيوط
    t_scanner = threading.Thread(
        target=_scanner_thread,
        args=(
            client, hybrid_strategy, risk_manager, trailing_manager,
            state_manager, notifier, news_engine, ai_engine, ta_engine,
            social_engine, universe_filter, derivatives_filter, active_strategies
        ),
        name="ScannerThread",
        daemon=True
    )
    t_monitor = threading.Thread(
        target=_monitor_thread,
        args=(
            client, state_manager, notifier, trailing_manager,
            risk_manager, alert_manager, ta_engine, api_status_monitor
        ),
        name="MonitorThread",
        daemon=True
    )
    t_heartbeat = threading.Thread(
        target=_heartbeat_thread,
        args=(client, state_manager, notifier),
        name="HeartbeatThread",
        daemon=True
    )
    # مرصد بيانات عامة: يزوّد فحص Demo بترتيب النشاط، ولا ينشئ إشارات أو أوامر.
    hot_list_monitor = HotListShadowMonitor()
    t_hot_list = threading.Thread(
        target=hot_list_monitor.run_forever,
        args=(_shutdown_event,),
        name="HotListShadowThread",
        daemon=True,
    )
    # فرع بحث سببي مستقل: يحول اندفاعات القائمة الساخنة إلى خطة جديدة كاملة
    # (دخول/إبطال/هدف/RR) ويتابع نتيجتها افتراضياً. لا يملك أي استدعاء تنفيذ.
    momentum_continuation_monitor = MomentumContinuationShadowMonitor(
        client=client,
        ta_engine=ta_engine,
        state_manager=state_manager,
    )
    t_momentum_continuation = threading.Thread(
        target=momentum_continuation_monitor.run_forever,
        args=(_shutdown_event,),
        name="MomentumContinuationShadowThread",
        daemon=True,
    )
    # مسار تحقق prospective مستقل مبني على Replay الدقيقة المصحح. يستهلك
    # بيانات Binance العامة فقط ويسجل نتائج افتراضية؛ لا يملك منفذ تنفيذ.
    active_altcoin_shadow_monitor = ActiveAltcoinShadowMonitor(
        state_manager=state_manager,
    )
    t_active_altcoin_shadow = threading.Thread(
        target=active_altcoin_shadow_monitor.run_forever,
        args=(_shutdown_event,),
        name="ActiveAltcoinShadowThread",
        daemon=True,
    )

    # تشغيل الخيوط
    t_scanner.start()
    t_monitor.start()
    t_heartbeat.start()
    t_hot_list.start()
    t_momentum_continuation.start()
    t_active_altcoin_shadow.start()
    logger.info("✅ [Main] كل الخيوط تعمل. المراقبة في main thread...")

    # ── main thread: يراقب فقط ويستجيب للـ force_scan ─────────────
    run_duration_seconds = max(
        0.0, float(os.getenv("BOT_RUN_DURATION_SECONDS", "0") or 0)
    )
    run_deadline = (
        time.monotonic() + run_duration_seconds
        if run_duration_seconds > 0 else None
    )
    if run_deadline is not None:
        logger.info(
            f"[Main] تشغيل تجريبي محدود لمدة {run_duration_seconds / 3600:.2f} ساعة."
        )

    try:
        while not _shutdown_event.is_set():
            if run_deadline is not None and time.monotonic() >= run_deadline:
                logger.info("⏱️ [Main] اكتملت مدة التشغيل التجريبي المحددة.")
                _request_shutdown("RUN_DURATION_COMPLETE")
                break

            # تحقق من أمر force_scan القادم من تيليجرام
            if state_manager.get("force_scan_now", False):
                logger.info("⚡ [Main] استقبال أمر force_scan — إرسال إشارة للـ Scanner.")
                state_manager.set("force_scan_now", False)
                _force_scan_event.set()   # يوقظ _scanner_thread فوراً

            # تأكد أن الخيوط لا تزال حية
            # المرصد الساخن shadow-only واختياري؛ تعطلُه لا يجوز أن يوقف
            # التداول أو إدارة المراكز. الخيوط الثلاثة التالية فقط حرجة.
            for t in [t_scanner, t_monitor, t_heartbeat]:
                if not t.is_alive():
                    logger.critical(f"🚨 [Main] الخيط {t.name} توقف — إيقاف البوت لتجنب التشغيل الجزئي.")
                    notifier.send_message(f"🚨 <b>خيط {t.name} توقف — البوت يُوقف نفسه تلقائياً.</b>")
                    _request_shutdown(f"THREAD_STOPPED_{t.name}")

            time.sleep(1)   # main thread يتحقق كل ثانية فقط

    except KeyboardInterrupt:
        logger.info("🛑 [Main] إيقاف البوت بأمر المستخدم...")
        _request_shutdown("KEYBOARD_INTERRUPT")
    finally:
        _request_shutdown(_shutdown_reason)
        notifier.stop_polling()
        if webapp_server is not None:
            webapp_server.stop()
        try:
            ws_manager.stop()
        finally:
            _candle_stream.stop()
            state_manager.save_state(force=True)
        for thread in (
            t_scanner,
            t_monitor,
            t_heartbeat,
            t_hot_list,
            t_momentum_continuation,
            t_active_altcoin_shadow,
        ):
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=5)
        try:
            from utils.db_manager import close_conn as close_trades_db
            from utils.forensics_logger import close_conn as close_forensics_db
            close_trades_db()
            close_forensics_db()
        except Exception as exc:
            logger.warning(f"[Shutdown] تعذر إغلاق اتصال قاعدة بيانات: {exc}")
        _release_writer_lease(writer_lease)
        record_run_end(_shutdown_reason)
        mark_graceful_shutdown(_shutdown_reason)
        stop_resource_monitor()
        log_memory_milestone("PROCESS_SHUTDOWN", force=True)
        logger.info("✅ [Main] تم الإيقاف بأمان وحفظ الحالة.")
        close_decision_audit()

if __name__ == "__main__":
    main()
