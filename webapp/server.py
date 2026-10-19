"""
Telegram Mini App — Flask Dashboard Server
يعمل في خيط منفصل على port 8080 بجانب البوت الرئيسي.
"""
import os
import json
import threading
import time
import tempfile
import pandas as pd
from flask import Flask, jsonify, render_template, request, abort
import hashlib
import hmac
from datetime import datetime
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from core.config import Config
from core.logger import logger
from core.logger import get_log_dir

app = Flask(__name__, template_folder="templates", static_folder="static")

_state_manager = None
_client = None
_request_counts = defaultdict(list)
_rate_limit_lock = threading.Lock()

# ── Rate Limiter ──────────────────────────────────────────────────
def _rate_limit(max_requests=40, window_seconds=60):
    """
    يتحقق من معدل الطلبات لكل عنوان IP لمنع هجمات الإغراق.
    """
    ip = request.remote_addr
    now = datetime.now()
    # تنظيف الطلبات القديمة خارج النافذة الزمنية
    with _rate_limit_lock:
        _request_counts[ip] = [t for t in _request_counts[ip] if now - t < timedelta(seconds=window_seconds)]
        if len(_request_counts[ip]) >= max_requests:
            logger.warning(f"[RateLimit] ⚠️ تجاوز حد الطلبات لـ IP: {ip}")
            abort(429)
        _request_counts[ip].append(now)

# ── Telegram WebApp Validation ──────────────────────────────────
# يمنع فتح الـ Dashboard من خارج حساب التيليجرام الخاص
def _verify_telegram_init_data(init_data: str, bot_token: str, expected_user_id=None,
                               max_age_seconds: int = 3600) -> bool:
    """
    يتحقق من صحة initData المرسلة من Telegram WebApp.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        from urllib.parse import parse_qs
        parsed    = parse_qs(init_data, keep_blank_values=True)
        hash_val  = parsed.pop("hash", [None])[0]
        if not hash_val:
            return False

        # بناء سلسلة التحقق (data-check-string)
        data_check_string = "\n".join(
            sorted(f"{k}={v[0]}" for k, v in parsed.items())
        )

        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed   = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_val):
            return False

        auth_date = int(parsed.get("auth_date", ["0"])[0] or 0)
        age = time.time() - auth_date
        if auth_date <= 0 or age < -30 or age > max_age_seconds:
            return False

        if expected_user_id not in (None, ""):
            user_raw = parsed.get("user", [""])[0]
            user = json.loads(user_raw) if user_raw else {}
            if str(user.get("id", "")) != str(expected_user_id):
                return False

        return True
    except Exception:
        return False

# ── Authentication Decorator ─────────────────────────────────────
def _require_auth(f):
    """
    يقوم بفحص التخويل بناءً على توكن Telegram و initData مع حماية معدل الطلبات.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # التحقق من معدل الطلبات أولاً
        _rate_limit()
        
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            logger.error("[WebApp] رفض API: TELEGRAM_BOT_TOKEN غير مضبوط.")
            abort(503)
        init_data = request.args.get("initData") or request.headers.get("X-Telegram-Init-Data", "")
        expected_user_id = os.getenv("TELEGRAM_WEBAPP_USER_ID", "").strip()
        try:
            if not expected_user_id or int(expected_user_id) <= 0:
                raise ValueError("missing/invalid user id")
        except (TypeError, ValueError):
            logger.error("[WebApp] رفض API: TELEGRAM_WEBAPP_USER_ID غير مضبوط كمعرف مستخدم موجب.")
            abort(503)
        max_age = int(os.getenv("WEBAPP_AUTH_MAX_AGE_SECONDS", "3600"))
        if not init_data or not _verify_telegram_init_data(
            init_data, bot_token, expected_user_id=expected_user_id,
            max_age_seconds=max_age,
        ):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def init_webapp(state_manager=None, client=None):
    global _state_manager, _client
    _state_manager = state_manager
    _client = client


def _effective_runtime_config() -> dict:
    """Expose the effective risk configuration without secrets or guessed values."""
    effective_risk = float(Config.RISK_PER_TRADE_PERCENT)
    if _state_manager is not None:
        try:
            effective_risk = float(_state_manager.get_risk_pct())
        except Exception:
            pass
    return {
        "entry_margin_percent": float(Config.ENTRY_MARGIN_PERCENT),
        "base_stop_risk_percent": effective_risk,
        "hard_risk_cap_percent": float(Config.HARD_MAX_RISK_PER_TRADE_PERCENT),
        "leverage": int(getattr(Config, "LEVERAGE", 1)),
        "max_open_trades": int(getattr(Config, "MAX_OPEN_TRADES", 0)),
        "structural_stop_v2_enabled": bool(
            getattr(Config, "STRUCTURAL_STOP_V2_ENABLED", False)
        ),
        "trade_management_v2_enabled": bool(
            getattr(Config, "TRADE_MANAGEMENT_V2_ENABLED", False)
        ),
        "trade_management_v2_activation_r": float(
            getattr(Config, "TRADE_MANAGEMENT_V2_ACTIVATION_R", 1.5)
        ),
        "source": "effective_runtime_config",
    }

# ------------------------------------------------------------------
# Live Binance API helpers (بيانات حقيقية من باينانس مباشرة)
# ------------------------------------------------------------------

def _get_live_wallet() -> dict:
    """يجلب الرصيد الحقيقي من باينانس عبر الكلاينت الرئيسي"""
    global _client
    try:
        if not _client:
            raise Exception("Client not initialized in webapp")
        
        balance = _client.get_balance()
        if not balance:
            raise Exception("Empty balance returned")
            
        usdt    = balance.get("USDT", {})
        info    = balance.get("info", {})
        
        total_margin = float(info.get("totalMarginBalance") or info.get("totalWalletBalance") or usdt.get("total", 0) or 0)
        free         = float(info.get("availableBalance") or usdt.get("free", 0) or 0)
        used         = float(usdt.get("used",   0) or 0)
        unrealized_pnl = float(info.get("totalUnrealizedProfit", 0) or 0)

        return {
            "wallet_balance":   round(total_margin, 2),
            "available":        round(free,  2),
            "used_margin":      round(used,  2),
            "unrealized_pnl":   round(unrealized_pnl, 4),
            "source":           "binance_live",
        }
    except Exception as e:
        logger.warning(f"[WebApp] تعذر جلب الرصيد من باينانس: {e}")
        return {
            "wallet_balance": 0,
            "available":      0,
            "used_margin":    0,
            "unrealized_pnl": 0,
            "source":         "unavailable",
        }


def _get_live_positions() -> list:
    """يجلب الصفقات المفتوحة الحقيقية عبر الكلاينت الرئيسي"""
    global _client
    try:
        if not _client:
            raise Exception("Client not initialized")
            
        state_meta = {}
        if _state_manager:
            try:
                state_meta = _state_manager.get("entry_metadata", {})
            except Exception:
                pass
            
        all_positions = _client.exchange.fetch_positions()
        active = []
        for pos in all_positions:
            info = pos.get("info", {})
            amt = float(info.get("positionAmt", 0) or 0)
            
            if amt == 0:
                continue

            contracts  = abs(amt)
            entry      = float(pos.get("entryPrice")  or info.get("entryPrice",  0) or 0)
            mark       = float(pos.get("markPrice")   or info.get("markPrice",   0) or 0)
            upnl       = float(pos.get("unrealizedPnl") or info.get("unRealizedProfit", 0) or 0)
            lev        = float(pos.get("leverage")    or info.get("leverage",    1) or 1)
            side       = "LONG" if amt > 0 else "SHORT"
            symbol_raw = str(info.get("symbol", ""))       # e.g. "NEARUSDT"
            symbol_ccxt = str(pos.get("symbol", ""))       # e.g. "NEAR/USDT:USDT"
            # Use the clean NEAR/USDT form for state_meta lookup
            slash_sym  = symbol_ccxt.split(":")[0]         # "NEAR/USDT"

            notional   = entry * contracts
            margin     = notional / lev if lev > 0 else notional
            roe_pct    = (upnl / margin * 100) if margin > 0 else 0.0
            
            # جلب SL/TP من حالة البوت — initial_sl وcurrent_sl
            bot_meta    = state_meta.get(slash_sym) or state_meta.get(symbol_raw) or {}
            initial_sl  = float(bot_meta.get("initial_sl") or bot_meta.get("sl") or 0)
            current_sl  = float(bot_meta.get("current_sl", 0) or 0)
            tp          = float(bot_meta.get("tp",         0) or 0)
            confirmed_source = "state"
            if bool(getattr(Config, "LIVE_SL_UI_SYNC_ENABLED", True)):
                try:
                    protection = _client.get_position_protection_orders(
                        slash_sym or symbol_raw,
                        side if getattr(_client, "_is_hedge_mode", False) else None,
                    )
                    sl_orders = (protection or {}).get("sl_orders") or []
                    confirmed_prices = [
                        float(_client._order_trigger_price(order) or 0.0)
                        for order in sl_orders
                    ]
                    confirmed_prices = [price for price in confirmed_prices if price > 0]
                    if confirmed_prices:
                        # There must be one protective leg; if legacy duplicates exist,
                        # display the most protective confirmed price without mutating state.
                        current_sl = (
                            max(confirmed_prices) if side == "LONG" else min(confirmed_prices)
                        )
                        confirmed_source = "binance_algo_order"
                except Exception as sync_error:
                    logger.warning(
                        f"[WebApp] تعذر مزامنة SL المؤكد لـ {slash_sym or symbol_raw}: "
                        f"{sync_error}"
                    )
            # نعرض current_sl دائماً إذا كان موجوداً (سواء تحرك أم لا)
            sl_moved = current_sl > 0

            active.append({
                "symbol":         slash_sym or symbol_raw,
                "side":           side,
                "contracts":      contracts,
                "margin_used":    round(margin, 2),
                "entry_price":    round(entry, 4),
                "mark_price":     round(mark,  4),
                "unrealized_pnl": round(upnl,  4),
                "roe_pct":        round(roe_pct, 2),
                "leverage":       int(lev),
                "sl":             initial_sl if initial_sl > 0 else None,
                "initial_sl":     initial_sl if initial_sl > 0 else None,
                "current_sl":     current_sl if sl_moved else None,
                "current_sl_source": confirmed_source,
                "current_sl_confirmed_at": bot_meta.get("current_sl_confirmed_at"),
                "protection_stage": bot_meta.get("protection_stage") or "INITIAL_PROTECTION",
                "unrealized_r": round(
                    (
                        (mark - entry) / abs(entry - initial_sl)
                        if side == "LONG" else (entry - mark) / abs(entry - initial_sl)
                    ) if initial_sl > 0 and entry != initial_sl else 0.0,
                    3,
                ),
                "tp":             tp if tp > 0 else None,
                "source":         "binance_live",
            })
        return active

    except Exception as e:
        logger.warning(f"[WebApp] تعذر جلب الصفقات المفتوحة من باينانس: {e}")
        return _positions_from_state()


def _positions_from_state() -> list:
    """احتياطي: الصفقات المسجلة في state.json (بوت فقط)"""
    state = _load_state()
    result = []
    for symbol, meta in state.get("entry_metadata", {}).items():
        result.append({
            "symbol":         symbol,
            "side":           str(meta.get("side", "")).upper(),
            "contracts":      float(meta.get("amount", 0) or 0),
            "entry_price":    float(meta.get("entry_price", 0) or 0),
            "mark_price":     0.0,
            "unrealized_pnl": 0.0,
            "roe_pct":        0.0,
            "leverage":       int(meta.get("leverage", 1) or 1),
            "sl":             float(meta.get("initial_sl") or meta.get("sl") or 0) or None,
            "initial_sl":     float(meta.get("initial_sl") or meta.get("sl") or 0) or None,
            "current_sl":     float(meta.get("current_sl") or 0) or None,
            "current_sl_source": "state_json",
            "current_sl_confirmed_at": meta.get("current_sl_confirmed_at"),
            "protection_stage": meta.get("protection_stage") or "INITIAL_PROTECTION",
            "tp":             float(meta.get("tp") or 0) or None,
            "source":         "state_json",
        })
    return result

# ------------------------------------------------------------------
# Trade / State helpers
# ------------------------------------------------------------------

def _load_trades() -> list:
    # ── SQLite أولاً ─────────────────────────────────────────────
    try:
        from utils.db_manager import load_trades_df, DB_PATH
        if os.path.exists(DB_PATH):
            df = load_trades_df()
            if not df.empty:
                return _df_to_records(df)
    except Exception:
        pass

    # ── CSV احتياطي ───────────────────────────────────────────────
    csv_path = getattr(Config, "TESTNET_TRADES_LOG", "/app/data/testnet_trades_log.csv")
    if not os.path.exists(csv_path):
        return []
    try:
        df = pd.read_csv(csv_path)
        df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
        df["exit_time"]  = pd.to_datetime(df["exit_time"],  errors="coerce")
        df = df[df["exit_time"].notna()].copy()
        for col in ["pnl", "pnl_pct", "entry_price", "exit_price"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        df["roe_pct"] = 0.0
        return _df_to_records(df)
    except Exception as e:
        logger.error(f"[WebApp] Error loading trades from CSV: {e}")
        return []


def _df_to_records(df: pd.DataFrame) -> list:
    """يحوّل DataFrame إلى قائمة dicts جاهزة لـ JSON."""
    df = df.sort_values("exit_time", ascending=False)
    records = []
    for _, row in df.iterrows():
        records.append({
            "symbol":      str(row.get("symbol", "")),
            "side":        str(row.get("side", "")).upper(),
            "entry_price": float(row.get("entry_price", 0)),
            "exit_price":  float(row.get("exit_price", 0)),
            "pnl":         float(row.get("pnl", 0)),
            "pnl_pct":     float(row.get("pnl_pct", 0)),
            "roe_pct":     float(row.get("roe_pct", 0)),
            "exit_reason": str(row.get("exit_reason", "")),
            "entry_time":  row["entry_time"].strftime("%m-%d %H:%M") if pd.notna(row.get("entry_time")) else "",
            "exit_time":   row["exit_time"].strftime("%m-%d %H:%M")  if pd.notna(row.get("exit_time"))  else "",
        })
    return records

def _load_signal_forensics(limit: int = 50) -> list:
    """
    يجلب سجل القرارات (لماذا دُخل ولماذا رُفض) من جدول forensics في SQLite.
    يُستخدم في مراجعة أداء البوت.
    """
    try:
        import sqlite3
        from utils.forensics_logger import DB_PATH as FORENSICS_DB_PATH
        db_path = os.getenv("FORENSICS_DB_PATH", FORENSICS_DB_PATH)
        if not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur  = conn.cursor()
        cur.execute("""
            SELECT symbol, signal, reason, ta_score, rsi, adx,
                   htf_trend, sentiment_label, sentiment_score,
                   action_taken, timestamp
            FROM signal_forensics
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.warning(f"[Forensics] تعذّر جلب سجل القرارات: {e}")
        return []

def _load_state() -> dict:
    state_file = os.getenv("STATE_FILE", "/app/data/bot_state.json")
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _compute_stats(trades: list) -> dict:
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "profit_factor": 0.0, "best_trade": 0.0, "worst_trade": 0.0}
    pnls    = [t["pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p <= 0]
    gross_profit = sum(winners)
    gross_loss   = abs(sum(losers))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    return {
        "total":         len(trades),
        "wins":          len(winners),
        "losses":        len(losers),
        "win_rate":      round(len(winners) / len(trades) * 100, 1) if trades else 0.0,
        "total_pnl":     round(sum(pnls), 4),
        "profit_factor": round(pf, 2),
        "best_trade":    round(max(pnls), 4),
        "worst_trade":   round(min(pnls), 4),
    }

def _build_chart_data(trades: list) -> dict:
    recent  = list(reversed(trades))   # كل الصفقات بدون قيد
    labels  = [t["exit_time"] for t in recent]   # عرض التاريخ كاملاً
    running = 0.0
    data    = []
    for t in recent:
        running += t["pnl"]
        data.append(round(running, 4))
    bar_colors = ["rgba(52,211,153,.85)" if t["pnl"] >= 0 else "rgba(248,113,113,.85)" for t in recent]
    return {"labels": labels, "cumulative": data, "bar_colors": bar_colors}

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/dashboard")
@_require_auth
def api_dashboard():
    trades         = _load_trades()
    stats          = _compute_stats(trades)
    chart          = _build_chart_data(trades)
    wallet         = _get_live_wallet()        # ← رصيد حقيقي من باينانس
    open_positions = _get_live_positions()     # ← صفقات حقيقية من باينانس

    return jsonify({
        "stats":          stats,
        "trades":         trades,
        "open_positions": open_positions,
        "chart":          chart,
        "wallet":         wallet,
        "runtime_config": _effective_runtime_config(),
        "updated_at":     datetime.now().strftime("%H:%M:%S"),
    })

@app.route("/api/latest_scan")
@_require_auth
def api_latest_scan():
    """يُرجع آخر نتائج مسح السوق من state_manager"""
    try:
        if _state_manager:
            scan = _state_manager.get("latest_scan", {})
        else:
            state = _load_state()
            scan = state.get("latest_scan", {})
        return jsonify(scan or {})
    except Exception as e:
        logger.error(f"[WebApp] خطأ في /api/latest_scan: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    initialized = _state_manager is not None and _client is not None
    risk_status = _state_manager.get("risk_equity_status", "UNKNOWN") if _state_manager else "UNKNOWN"
    return jsonify({
        "status": "ok" if initialized else "starting",
        "initialized": initialized,
        "risk_equity_status": risk_status,
    })

@app.route("/ready")
def ready():
    if _state_manager is None or _client is None:
        return jsonify({"status": "not_ready", "reason": "components_not_initialized"}), 503
    try:
        log_dir = get_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=log_dir, prefix=".ready_", delete=True):
            pass
        _client.exchange.fetch_time()
        return jsonify({"status": "ready"})
    except OSError as e:
        return jsonify({"status": "not_ready", "reason": "log_volume_unwritable", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"status": "not_ready", "reason": "binance_unreachable", "detail": str(e)}), 503

# ------------------------------------------------------------------
# Launcher
# ------------------------------------------------------------------

@app.route("/api/forensics")
@_require_auth
def api_forensics():
    """يُرجع سجل القرارات التفصيلي لمراجعة أداء البوت"""
    rows = _load_signal_forensics(limit=50)
    return jsonify({"signals": rows, "total": len(rows)})

class WebAppServerHandle:
    def __init__(self, server):
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="WebAppServer"
        )

    def start(self):
        self.thread.start()

    def stop(self, timeout=5.0):
        self.server.shutdown()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=timeout)
        self.server.server_close()


def start_webapp(state_manager=None, client=None, port: int = 8080):
    """يشغّل WSGI server قابلاً للإغلاق بدلاً من Flask dev loop غير القابل للإدارة."""
    from werkzeug.serving import make_server

    init_webapp(state_manager, client)
    logger.info(f"[WebApp] Starting dashboard on port {port}...")
    server = make_server("0.0.0.0", port, app, threaded=True)
    handle = WebAppServerHandle(server)
    handle.start()
    host_url = os.getenv("WEBAPP_PUBLIC_URL", f"http://localhost:{port}")
    logger.info(f"[WebApp] Dashboard running at {host_url}")
    return handle
