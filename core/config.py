import os
from dotenv import load_dotenv

# تحميل المتغيرات من ملف .env
load_dotenv()

def split_keys(value):
    if not value:
        return []
    return [
        k.strip()
        for k in str(value).split(",")
        if k and k.strip() and "your_" not in k.lower()
    ]

def merge_keys(*values):
    keys = []
    for value in values:
        if isinstance(value, list):
            keys.extend(value)
        else:
            keys.extend(split_keys(value))

    # إزالة التكرار مع الحفاظ على الترتيب
    cleaned = []
    seen = set()
    for key in keys:
        if key not in seen:
            cleaned.append(key)
            seen.add(key)
    return cleaned

class Config:
    # Feature Toggles
    AI_DEBATE_MODE = True   # False = تحليل عادي سريع | True = لجنة Bull/Bear/Judge

    # Demo-only bridge: this mode runs the *current Telegram strategy* but
    # grants order authority exclusively to Binance Demo.  It is opt-in and
    # remains inert unless both flags are true.
    TELEGRAM_PROPOSAL_DEMO_MODE = os.getenv(
        "TELEGRAM_PROPOSAL_DEMO_MODE", "false"
    ).strip().lower() in ("true", "1", "yes")
    DEMO_EXECUTION_ENABLED = os.getenv(
        "DEMO_EXECUTION_ENABLED", "false"
    ).strip().lower() in ("true", "1", "yes")
    # Single-switch rollback for the SHORT-only confidence layer.  Disabling
    # it restores the pre-1.6.18 direct HYBRID SELL behaviour; LONG is never
    # routed through this policy.
    SHORT_HIGH_CONFIDENCE_V1_ENABLED = os.getenv(
        "SHORT_HIGH_CONFIDENCE_V1_ENABLED", "true"
    ).strip().lower() in ("true", "1", "yes")
    # Independent rollback for structure-first initial protective stops.  It
    # is opt-in so other deployments keep the historical stop calculation
    # unless the reviewed Demo runtime explicitly enables it.
    STRUCTURAL_INITIAL_STOP_V1_ENABLED = os.getenv(
        "STRUCTURAL_INITIAL_STOP_V1_ENABLED", "false"
    ).strip().lower() in ("true", "1", "yes")
    STRUCTURAL_STOP_ROUND_TRIP_COST_PCT = float(
        os.getenv("STRUCTURAL_STOP_ROUND_TRIP_COST_PCT", "0.0012")
    )
    TELEGRAM_POLLING_ENABLED = os.getenv(
        "TELEGRAM_POLLING_ENABLED", "true"
    ).strip().lower() in ("true", "1", "yes")
    # Only the user-facing Telegram runtime may emit chat messages or manage
    # the shared Demo account.  A disabled secondary executor used to keep
    # monitoring externally-owned positions and therefore duplicated close
    # notifications (and could cancel protection after the primary closed a
    # trade).  Defaults preserve the primary runtime while making a service
    # with both polling and execution disabled genuinely passive.
    TELEGRAM_OUTBOUND_ENABLED = os.getenv(
        "TELEGRAM_OUTBOUND_ENABLED",
        "true" if TELEGRAM_POLLING_ENABLED else "false",
    ).strip().lower() in ("true", "1", "yes")
    ACCOUNT_MANAGEMENT_ENABLED = os.getenv(
        "ACCOUNT_MANAGEMENT_ENABLED",
        "true" if (TELEGRAM_POLLING_ENABLED or DEMO_EXECUTION_ENABLED) else "false",
    ).strip().lower() in ("true", "1", "yes")

    # Binance Config.  Demo credentials are read only in the explicit bridge
    # mode; the ordinary Telegram runtime keeps its existing variable names.
    BINANCE_API_KEY = (
        os.getenv("BINANCE_DEMO_API_KEY")
        if TELEGRAM_PROPOSAL_DEMO_MODE
        else os.getenv("BINANCE_API_KEY")
    )
    BINANCE_API_SECRET = (
        os.getenv("BINANCE_DEMO_API_SECRET")
        if TELEGRAM_PROPOSAL_DEMO_MODE
        else os.getenv("BINANCE_API_SECRET")
    )
    USE_TESTNET = os.getenv("USE_TESTNET", "true").strip().lower() in ("true", "1", "yes")  # افتراضي: Testnet حتى يُضبط صراحةً في البيئة

    # News Config
    NEWS_API_KEY = os.getenv("NEWS_API_KEY")
    
    # Sentiment & Social API Config
    GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
    GROQ_API_KEYS = merge_keys(os.getenv("GROQ_API_KEY"), os.getenv("GROQ_API_KEYS"))
    GROQ_MODEL    = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

    # Gemini (Primary — supports Vision)
    GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
    GEMINI_API_KEYS = merge_keys(os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEYS"))
    GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # DeepSeek (Fallback #3)
    DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_API_KEYS = merge_keys(os.getenv("DEEPSEEK_API_KEY"), os.getenv("DEEPSEEK_API_KEYS"))
    DEEPSEEK_MODEL    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # Qwen (Fallback #4)
    QWEN_API_KEY  = os.getenv("QWEN_API_KEY", "")
    QWEN_API_KEYS = merge_keys(os.getenv("QWEN_API_KEY"), os.getenv("QWEN_API_KEYS"))
    QWEN_MODEL    = os.getenv("QWEN_MODEL", "qwen-plus")

    # AI Engine Provider: "gemini" → يحاول Gemini ثم Groq ثم DeepSeek ثم Qwen تلقائياً
    AI_ENGINE_PROVIDER = os.getenv("AI_ENGINE_PROVIDER", "gemini")

    # خدمات المشاعر/LLM اختيارية ولا تدخل المسار الآلي افتراضياً. لا توجد
    # أفضلية مثبتة لها في السجل، وتعطلها يجب ألا يؤخر التداول أو الإغلاق.
    AUTOMATED_SENTIMENT_ENABLED = os.getenv(
        "AUTOMATED_SENTIMENT_ENABLED", "false"
    ).strip().lower() in ("true", "1", "yes")
    AI_NEWS_SENTIMENT_ENABLED = os.getenv(
        "AI_NEWS_SENTIMENT_ENABLED", "false"
    ).strip().lower() in ("true", "1", "yes")

    # ── نقد الذكاء الاصطناعي بعد إغلاق الصفقة ──────────────────────
    AI_CRITIQUE_ENABLED   = os.getenv("AI_CRITIQUE_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    AI_CRITIQUE_TIMEFRAME = os.getenv("AI_CRITIQUE_TIMEFRAME", "15m")

    LUNARCRUSH_API_KEY = os.getenv("LUNARCRUSH_API_KEY")

    # CoinMarketCap — يُستخدم كاحتياطي لـ Fear & Greed عند تعطل LunarCrush
    COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY", "")

    COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")
    FRED_API_KEY = os.getenv("FRED_API_KEY")

    # Telegram Config
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # إعدادات التداول الافتراضية
    TRADE_SYMBOL = "BTC/USDT"
    RISK_PER_TRADE_PERCENT = float(os.getenv("RISK_PER_TRADE_PERCENT", "0.25").strip().rstrip("%"))  # يقبل "1%" أو "1" أو "0.5"
    MIN_RISK_PER_TRADE_PERCENT = float(
        os.getenv("MIN_RISK_PER_TRADE_PERCENT", "0.05")
    )
    HARD_MAX_RISK_PER_TRADE_PERCENT = float(
        os.getenv("HARD_MAX_RISK_PER_TRADE_PERCENT", "1.0")
    )

    # Target initial margin as a percentage of real account equity.
    ENTRY_MARGIN_PERCENT = float(os.getenv("ENTRY_MARGIN_PERCENT", "1.0").strip().rstrip("%"))
    LEVERAGE = int(os.getenv("LEVERAGE", 5))
    MIN_LEVERAGE = int(os.getenv("MIN_LEVERAGE", 1))
    MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", 10))

    # ── حدود الـ Position Sizing ────────────────────────────────────────
    # الحد الأقصى للـ Notional لصفقة واحدة كنسبة من الرصيد المتاح
    # مثال: 0.30 = لا تتجاوز 30% من الرصيد كـ notional لأي صفقة
    # بالنسبة لرصيد 5000$ → max notional = 1500$ (هامش 150$ عند رافعة 10x)
    MAX_NOTIONAL_BALANCE_PCT = float(os.getenv("MAX_NOTIONAL_BALANCE_PCT", "0.30"))

    # الحد الأدنى لمسافة SL كنسبة من السعر (0.3% افتراضياً)
    # يمنع تضخم الحجم عندما يكون SL قريباً جداً من السعر
    MIN_SL_DISTANCE_PCT = float(os.getenv("MIN_SL_DISTANCE_PCT", "0.003"))

    
    # حماية مدمجة وآمنة من بقاء قيمة REFERENCE_BALANCE القديمة في متغيرات البيئة بـ Railway
    _ref_bal = float(os.getenv("REFERENCE_BALANCE", 100.0) or 100.0)
    REFERENCE_BALANCE = 100.0 if _ref_bal in (50.0, 60.0) else _ref_bal

    MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "10"))
    # Operator-requested default and ceiling: ten concurrent positions.
    HARD_MAX_OPEN_TRADES = int(os.getenv("HARD_MAX_OPEN_TRADES", "10"))
    DAILY_LOSS_LIMIT_PERCENT = float(os.getenv("DAILY_LOSS_LIMIT_PERCENT", "1.5"))

    # ==========================================================
    # Virtual Orders / Sniper Pending Orders Lifecycle
    # ==========================================================
    # المهلة مرتبطة بسياق 15m، بينما التأكيد التنفيذي نفسه يأتي من شموع 5m.
    VIRTUAL_ORDER_MAX_AGE_MINUTES_STRICT = int(os.getenv("VIRTUAL_ORDER_MAX_AGE_MINUTES_STRICT", 30))
    VIRTUAL_ORDER_MAX_AGE_MINUTES_MEDIUM = int(os.getenv("VIRTUAL_ORDER_MAX_AGE_MINUTES_MEDIUM", 20))
    VIRTUAL_ORDER_MAX_AGE_MINUTES_RELAXED = int(os.getenv("VIRTUAL_ORDER_MAX_AGE_MINUTES_RELAXED", 15))

    VIRTUAL_ORDER_CANCEL_IF_TP_HIT_FIRST = os.getenv("VIRTUAL_ORDER_CANCEL_IF_TP_HIT_FIRST", "true").lower() == "true"
    VIRTUAL_ORDER_CANCEL_IF_INVALIDATED = os.getenv("VIRTUAL_ORDER_CANCEL_IF_INVALIDATED", "true").lower() == "true"
    VIRTUAL_ORDER_CANCEL_IF_TREND_FLIPS = os.getenv("VIRTUAL_ORDER_CANCEL_IF_TREND_FLIPS", "true").lower() == "true"

    # إذا ابتعد السعر عن منطقة الدخول كثيرًا ولم يرجع، نعتبر الفرصة فاتت.
    VIRTUAL_ORDER_MAX_DISTANCE_FROM_ENTRY_PCT = float(os.getenv("VIRTUAL_ORDER_MAX_DISTANCE_FROM_ENTRY_PCT", 0.04))

    # كم شمعة 15m نستخدم لفحص تغيّر الترند.
    # One EMA seed window for scanner, stream, cancellation and revalidation.
    # The legacy per-guard environment override is intentionally retired: a
    # different seed can reverse EMA50/EMA200 on the very same closed candle.
    DECISION_CANDLE_HISTORY = 260
    VIRTUAL_ORDER_TREND_CHECK_LIMIT = DECISION_CANDLE_HISTORY
    # Binance REST may briefly lag the just-closed 15m candle.  Retry only on
    # this bounded post-close schedule; exhausting it is fail-closed and never
    # grants an entry using stale structural evidence.
    VIRTUAL_ORDER_STRUCTURE_RETRY_DELAYS_SECONDS = tuple(
        max(1, int(value.strip()))
        for value in os.getenv(
            "VIRTUAL_ORDER_STRUCTURE_RETRY_DELAYS_SECONDS", "3,10,25,50,90"
        ).split(",")
        if value.strip()
    )
    VIRTUAL_ORDER_STRUCTURE_MAX_ATTEMPTS = max(
        1,
        int(os.getenv("VIRTUAL_ORDER_STRUCTURE_MAX_ATTEMPTS", "5")),
    )

    ENTRY_CONFIRMATION_TIMEFRAME = os.getenv("ENTRY_CONFIRMATION_TIMEFRAME", "5m")
    ENTRY_CONFIRMATION_LIMIT = int(os.getenv("ENTRY_CONFIRMATION_LIMIT", 80))
    ENTRY_CONFIRMATION_DATA_RETRY_DELAYS_SECONDS = tuple(
        max(1, int(value.strip()))
        for value in os.getenv(
            "ENTRY_CONFIRMATION_DATA_RETRY_DELAYS_SECONDS", "3,10,25,50"
        ).split(",")
        if value.strip()
    )
    ENTRY_CONFIRMATION_DATA_MAX_ATTEMPTS = max(
        1, int(os.getenv("ENTRY_CONFIRMATION_DATA_MAX_ATTEMPTS", "4"))
    )
    ENTRY_CONFIRMATION_WINDOW_MINUTES = int(
        os.getenv("ENTRY_CONFIRMATION_WINDOW_MINUTES", 12)
    )

    # Setup cooldowns are multiples of the 15m decision timeframe.  They block
    # only the same structural fingerprint; a genuinely new OB/BOS is allowed.
    SETUP_COOLDOWN_TIMEOUT_MINUTES = int(os.getenv("SETUP_COOLDOWN_TIMEOUT_MINUTES", 60))
    SETUP_COOLDOWN_INVALIDATED_MINUTES = int(os.getenv("SETUP_COOLDOWN_INVALIDATED_MINUTES", 120))
    SETUP_COOLDOWN_STOP_MINUTES = int(os.getenv("SETUP_COOLDOWN_STOP_MINUTES", 240))
    SETUP_COOLDOWN_MANUAL_MINUTES = int(os.getenv("SETUP_COOLDOWN_MANUAL_MINUTES", 60))
    SETUP_COOLDOWN_TP_MINUTES = int(os.getenv("SETUP_COOLDOWN_TP_MINUTES", 30))

    # ==========================================================
    # API Key Pools / Failover Rotation
    # ==========================================================

    COINGLASS_API_KEYS = merge_keys(
        os.getenv("COINGLASS_API_KEY"),
        os.getenv("COINGLASS_API_KEYS")
    )

    NEWS_API_KEYS = merge_keys(
        os.getenv("NEWS_API_KEY"),
        os.getenv("NEWS_API_KEYS")
    )



    GROQ_API_KEYS = merge_keys(
        os.getenv("GROQ_API_KEY"),
        os.getenv("GROQ_API_KEYS")
    )

    LUNARCRUSH_API_KEYS = merge_keys(
        os.getenv("LUNARCRUSH_API_KEY"),
        os.getenv("LUNARCRUSH_API_KEYS")
    )
    
    LUNARCRUSH_CACHE_TTL_SECONDS    = int(os.getenv("LUNARCRUSH_CACHE_TTL_SECONDS", 3600))
    LUNARCRUSH_429_COOLDOWN_SECONDS = int(os.getenv("LUNARCRUSH_429_COOLDOWN_SECONDS", 600))
    LUNARCRUSH_402_COOLDOWN_SECONDS = int(os.getenv("LUNARCRUSH_402_COOLDOWN_SECONDS", 3600))
    LUNARCRUSH_MAX_RPM              = int(os.getenv("LUNARCRUSH_MAX_RPM", 8))
    SNIPER_MAX_WAIT_MINUTES         = int(os.getenv("SNIPER_MAX_WAIT_MINUTES", 15))
    # A mark-price touch can remain inside the zone while the contract
    # bid/ask is temporarily unavailable or too wide.  Do not hammer the
    # order book every monitor tick; this is a polling backoff only and does
    # not relax the executable-price guard.
    ENTRY_BASIS_RETRY_SECONDS       = float(os.getenv("ENTRY_BASIS_RETRY_SECONDS", 15))

    # ── إدارة المخاطر ─────────────────────────────────────────────────
    MAX_SL_PCT        = float(os.getenv("MAX_SL_PCT", 0.05))       # الحد الأقصى للـ SL (5%)
    ATR_SL_MULTIPLIER = float(os.getenv("ATR_SL_MULTIPLIER", 1.8)) # مضاعف ATR للـ SL — القيمة الفعلية دائماً 1.8 (تم توحيدها هنا وإزالة التعريف المكرر سطر 184 القديم)

    TRADE_JOURNAL_TZ = os.getenv("TRADE_JOURNAL_TZ", "UTC")

    COINGECKO_API_KEYS = merge_keys(
        os.getenv("COINGECKO_API_KEY"),
        os.getenv("COINGECKO_API_KEYS")
    )

    # [API Health Monitor]
    API_HEALTH_CHECK_ENABLED = os.getenv("API_HEALTH_CHECK_ENABLED", "true").lower() == "true"
    API_HEALTH_CHECK_INTERVAL_MINUTES = int(os.getenv("API_HEALTH_CHECK_INTERVAL_MINUTES", 60))
    API_HEALTH_ALERT_COOLDOWN_MINUTES = int(os.getenv("API_HEALTH_ALERT_COOLDOWN_MINUTES", 60))
    API_HEALTH_TIMEOUT_SECONDS = int(os.getenv("API_HEALTH_TIMEOUT_SECONDS", 15))
    API_HEALTH_NOTIFY_WARNINGS = os.getenv("API_HEALTH_NOTIFY_WARNINGS", "true").lower() == "true"

    # Railway API اختياري، لا تستخدمه إلا إذا قررت إدارة Variables برمجياً لاحقاً
    RAILWAY_API_TOKEN = os.getenv("RAILWAY_API_TOKEN")
    RAILWAY_PROJECT_ID = os.getenv("RAILWAY_PROJECT_ID")
    RAILWAY_ENVIRONMENT_ID = os.getenv("RAILWAY_ENVIRONMENT_ID")
    RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID")

    # [إعدادات فلتر أمان السوق - Universe Filter]
    MIN_24H_VOLUME_USDT = 10_000_000 # الحد الأدنى لحجم التداول 10M USDT
    MAX_SPREAD_PCT = 0.001           # الحد الأقصى للسبريد 0.10%
    BLACKLIST = ['USDC/USDT', 'FDUSD/USDT', 'TUSD/USDT', 'AEUR/USDT', 'EUR/USDT']
    
    # حد المسح لعملات السوق المشتعلة (الحد الأقصى لعدد العملات التي يفحصها البوت ديناميكياً)
    MARKET_SCAN_LIMIT = int(os.getenv("MARKET_SCAN_LIMIT", 50))

    # [إعدادات صيد القيعان وملاحقة الموجة الكبرى - Bottom Fishing & Trend Rider]
    BOTTOM_FISHING_ENABLED = os.getenv("BOTTOM_FISHING_ENABLED", "true").lower() == "true"
    BOTTOM_FISHING_RSI_THRESHOLD = float(os.getenv("BOTTOM_FISHING_RSI_THRESHOLD", 30.0))
    BOTTOM_FISHING_HTF = os.getenv("BOTTOM_FISHING_HTF", "4h")
    RUNNER_REMAINING_PCT = float(os.getenv("RUNNER_REMAINING_PCT", 0.25))  # إبقاء 25% من حجم الصفقة كملاحق بعد أهداف TP
    RUNNER_TRAILING_DEVIATION_PCT = float(os.getenv("RUNNER_TRAILING_DEVIATION_PCT", 0.02)) # 2% تراجع ملاحق للـ Runner لركوب الاتجاه الطويل

    # [إعدادات العملات الجديدة والميمية - Meme/New Coins Mode]
    MEME_RISK_PER_TRADE_PERCENT = 0.25  # تخفيض المخاطرة لـ 0.25%
    MEME_LEVERAGE = int(os.getenv("MEME_LEVERAGE", 1)) # رافعة منخفضة لضمان الأمان في الميم (يمكن تخصيصها عبر البيئة)

    # [إعدادات الخروج الديناميكي - ATR TP/SL Config]
    USE_ATR_TARGETS   = True            # تفعيل أهداف جني الأرباح ووقف الخسارة المبنية على ATR
    ATR_WINDOW        = 14              # عدد الشموع لحساب الـ ATR
    # ATR_SL_MULTIPLIER محدَّد أعلى في قسم "إدارة المخاطر" ويُقرأ من env var (افتراضي 1.8)
    ATR_TP_MULTIPLIER = 3.0             # مضاعف جني الأرباح للـ ATR (عائد لمخاطرة 1:2)

    # ═══════════════════════════════════════════════════════════════
    # [فلاتر جودة الدخول — Entry Quality Filters]
    # ═══════════════════════════════════════════════════════════════
    MIN_ATR_PCT_OF_PRICE = float(os.getenv("MIN_ATR_PCT_OF_PRICE", 0.008))  # 0.8%: الحد الأدنى لـ ATR نسبة للسعر (رفض السوق الجانبي)
    ENTRY_EXTREME_ZONE_PCT = float(os.getenv("ENTRY_EXTREME_ZONE_PCT", 0.30))  # 30%: رفض الدخول في أعلى/أدنى 30% من الشمعة
    REQUIRE_RETRACEMENT_AFTER_BIG_MOVE = os.getenv("REQUIRE_RETRACEMENT_AFTER_BIG_MOVE", "true").lower() == "true"
    BIG_MOVE_PCT_THRESHOLD = float(os.getenv("BIG_MOVE_PCT_THRESHOLD", 0.015))  # 1.5%: الحد الأقصى حركة الشمعة السابقة (انتظر تصحيح)
    ENTRY_EXTENSION_LOOKBACK_BARS = int(os.getenv("ENTRY_EXTENSION_LOOKBACK_BARS", "96"))
    ENTRY_MAX_DIRECTIONAL_MOVE_PCT = float(os.getenv("ENTRY_MAX_DIRECTIONAL_MOVE_PCT", "0.12"))
    ENTRY_MAX_DIRECTIONAL_ATR = float(os.getenv("ENTRY_MAX_DIRECTIONAL_ATR", "8.0"))
    ENTRY_EXTENSION_RANGE_ZONE = float(os.getenv("ENTRY_EXTENSION_RANGE_ZONE", "0.75"))

    # درجة تشخيصية لفرز وعرض المرشحين فقط؛ لا تتجاوز فلاتر المخاطر ولا تسمح
    # بالتنفيذ. يمكن تعديل الأوزان من البيئة بعد التحقق التاريخي دون تغيير الكود.
    CANDIDATE_SCORE_WEIGHTS = {
        "technical_setup": float(os.getenv("CANDIDATE_WEIGHT_TECHNICAL_SETUP", "10")),
        "context_4h": float(os.getenv("CANDIDATE_WEIGHT_CONTEXT_4H", "15")),
        "htf_alignment": float(os.getenv("CANDIDATE_WEIGHT_HTF_ALIGNMENT", "5")),
        "setup_1h": float(os.getenv("CANDIDATE_WEIGHT_SETUP_1H", "10")),
        "momentum": float(os.getenv("CANDIDATE_WEIGHT_MOMENTUM", "10")),
        "volume": float(os.getenv("CANDIDATE_WEIGHT_VOLUME", "10")),
        "entry_quality": float(os.getenv("CANDIDATE_WEIGHT_ENTRY_QUALITY", "15")),
        "trade_geometry": float(os.getenv("CANDIDATE_WEIGHT_TRADE_GEOMETRY", "15")),
        "entry_trigger": float(os.getenv("CANDIDATE_WEIGHT_ENTRY_TRIGGER", "5")),
        "derivatives_context": float(os.getenv("CANDIDATE_WEIGHT_DERIVATIVES", "5")),
    }

    # دورة تنفيذ الأمر وإدارة الحماية
    ORDER_INTENT_RECOVERY_GRACE_SECONDS = float(os.getenv("ORDER_INTENT_RECOVERY_GRACE_SECONDS", "30"))
    SL_REPLACE_BACKOFF_SECONDS = float(os.getenv("SL_REPLACE_BACKOFF_SECONDS", "30"))

    # Demo discovery only; ordinary entry and risk gates still apply.
    DEMO_ACTIVITY_UNIVERSE_ENABLED = os.getenv("DEMO_ACTIVITY_UNIVERSE_ENABLED", "true").lower() == "true"
    DEMO_ACTIVITY_UNIVERSE_SLOTS = max(0, min(50, int(os.getenv("DEMO_ACTIVITY_UNIVERSE_SLOTS", "20"))))
    DEMO_ACTIVITY_MAX_AGE_SECONDS = max(30, min(300, int(os.getenv("DEMO_ACTIVITY_MAX_AGE_SECONDS", "180"))))

    # Testnet Dynamic Screener
    TESTNET_DYNAMIC_TOP_VOLUME_LIMIT = int(os.getenv("TESTNET_DYNAMIC_TOP_VOLUME_LIMIT", 30))

    # [إعدادات تقارير Testnet والمراقبة]
    TESTNET_REPORT_INTERVAL_HOURS = 24  # إرسال تقرير المقارنة والـ Testnet كل 24 ساعة
    DYNAMIC_BLACKLIST_ACTIVE = os.getenv("DYNAMIC_BLACKLIST_ACTIVE", "false").lower() == "true"
    # لا نحكم على زوج من بضعة أحداث فقط؛ القيمة تخص الصفقات المغلقة المستقلة.
    DYNAMIC_BLACKLIST_MIN_TRADES = max(
        20, int(os.getenv("DYNAMIC_BLACKLIST_MIN_TRADES", "20"))
    )
    
    # [إعدادات Testnet التفصيلية]
    TESTNET_DEBUG_MODE = True
    TESTNET_RELAXED_ENTRY = False
    TESTNET_MAX_OPEN_TRADES = int(os.getenv("TESTNET_MAX_OPEN_TRADES", "10"))
    TESTNET_RISK_MULTIPLIER = 1.0
    
    # تم إجبار القيمة على 0.0 لتخطي أي إعدادات قديمة في Railway
    # بضبطه على 0.0، البوت سيقوم بجلب رصيد الـ Testnet الفعلي من باينانس (مثلاً 10,000$) ولن يستخدم المحاكاة الداخلية.
    TESTNET_SIMULATED_BALANCE = 0.0

    # [إعدادات المحفظة الوهمية والتقارير]
    # Railway/Linux: اجعل الـVolume هو الافتراضي. محلياً على Windows نبقى داخل
    # المشروع حتى لا يتحول /app/data إلى مسار قرص محلي غير مقصود.
    _DEFAULT_DATA_DIR = "." if os.name == "nt" else "/app/data"
    DATA_DIR = os.getenv("DATA_DIR", _DEFAULT_DATA_DIR)
    TESTNET_TRADES_LOG = os.getenv(
        "TESTNET_TRADES_LOG",
        os.path.join(DATA_DIR, "testnet_trades_log.csv")
    )

    REPORT_TZ = os.getenv("REPORT_TZ", "Asia/Aden")
    REPORT_RECENT_TRADES_LIMIT = int(os.getenv("REPORT_RECENT_TRADES_LIMIT", 10))

    # [SMC Liquidity Sweep Filter]
    REQUIRE_LIQUIDITY_SWEEP_FOR_OB = True
    LIQUIDITY_SWEEP_LOOKBACK = 10       # كم شمعة نستخدم لاستخراج القمم/القيعان السابقة
    LIQUIDITY_SWEEP_RECENT_BARS = 8     # يجب أن يكون السويب قريبًا من الـ OB

    # ==========================================================
    # [Fallback Strategy — RSI+MACD بدون OB]
    # ⚠️ السبب الرئيسي للخسائر: يدخل بدون Order Block حقيقي
    # اتركه False حتى تثبت الاستراتيجية الرئيسية (SMC فقط)
    # ==========================================================
    ENABLE_FALLBACK_STRATEGY = os.getenv("ENABLE_FALLBACK_STRATEGY", "false").lower() == "true"

    # لا يوجد إغلاق جزئي: TP1/TP2 مراحل Step Trail فقط، وTP3 يغلق كامل المركز.
    MOVE_SL_TO_BE_AFTER_TP1 = False    # ← لا ننقل SL للـ Breakeven بعد TP1 مباشرة
    BREAKEVEN_BUFFER_PCT = 0.001       # 0.1% فوق الدخول للشراء وتحت الدخول للبيع

    # [Step Trailing — حماية الأرباح المرحلية]
    # بعد TP1: يتم رفع SL ليحجز 75% من مسافة TP1 (بدلاً من نقله للـ Breakeven)
    STEP_TRAIL_TP1_PROTECTION_PCT = 0.75
    # بعد TP2: يتم رفع SL إلى أعلى TP1 بهذه النسبة
    STEP_TRAIL_TP2_BUFFER_PCT = 0.001
    # بعد TP3: يتم رفع SL ليحجز 50% من مسافة TP2 → TP3
    STEP_TRAIL_TP3_PROTECTION_PCT = 0.50

    # [Scan Scheduler]
    SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", 900))  # 15 دقيقة افتراضيًا
    MIN_SCAN_INTERVAL_SECONDS = 300     # 5 دقائق
    MAX_SCAN_INTERVAL_SECONDS = 3600    # ساعة
    MONITOR_TICK_SECONDS = 10

    # [إعدادات فلتر المشتقات - CoinGlass Filter]
    DERIVATIVES_FILTER_MODE = "enforce"     # off / audit (مراقبة فقط) / enforce (حظر فعلي)
    MAX_FUNDING_RATE_ABS = 0.001          # 0.1% الحد الأقصى لمعدل التمويل المطلق المسموح به
    OI_CHANGE_LOOKBACK = 4                # عدد الشموع لمراقبة تغير الاهتمام المفتوح
    MAX_LONG_SHORT_RATIO = 2.5            # الحد الأقصى لنسبة الشراء/البيع المزدحمة
    COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")
    
    # العملات المطلوب فرض فحصها واختبارها في بيئة Testnet
    TESTNET_FORCE_SYMBOLS = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "DOGE/USDT",
        "XRP/USDT",
        "ADA/USDT",
        "AVAX/USDT",
        "NEAR/USDT",
        "LINK/USDT",
        "1000PEPE/USDT",
        
        # عملات السكالبينج الإضافية المقترحة
        "ZEC/USDT",
        "DEGEN/USDT",
        "HOME/USDT"
    ]

    # ==========================================================
    # Trade Journal / Post-Trade Forensics
    # ==========================================================
    TRADE_JOURNAL_ENABLED = os.getenv("TRADE_JOURNAL_ENABLED", "true").lower() == "true"
    TRADE_JOURNAL_DIR = os.getenv("TRADE_JOURNAL_DIR", os.path.join(DATA_DIR, "trade_journal"))
    TRADE_JOURNAL_TIMEFRAMES = os.getenv("TRADE_JOURNAL_TIMEFRAMES", "1m,5m,15m")
    TRADE_JOURNAL_PRE_MINUTES = int(os.getenv("TRADE_JOURNAL_PRE_MINUTES", 45))
    TRADE_JOURNAL_POST_MINUTES = int(os.getenv("TRADE_JOURNAL_POST_MINUTES", 15))
    TRADE_JOURNAL_FETCH_TRADES = os.getenv("TRADE_JOURNAL_FETCH_TRADES", "true").lower() == "true"


    # ==========================================================
    # Smart Exit Management (3Commas-Inspired)
    # ==========================================================

    # Take Profit Laddering — جني الأرباح السُّلَّمي
    TP_LADDER_ENABLED = os.getenv("TP_LADDER_ENABLED", "true").lower() == "true"
    # توزيع النسب المئوية لكل هدف (يجب أن مجموعها = 1.0)
    TP_STEP_VOLUMES   = [0.25, 0.25, 0.50]   # 25% ثم 25% ثم 50%
    # مضاعفات ATR لكل هدف بالترتيب
    TP_STEP_MULTIPLIERS = [
        float(os.getenv("TP1_ATR_MULT", 1.5)),
        float(os.getenv("TP2_ATR_MULT", 2.5)),
        float(os.getenv("TP3_ATR_MULT", 3.0)),
    ]

    # SL Timeout — مهلة وقف الخسارة (لتجنب Wicks)
    # SL الحقيقي موجود على Binance؛ مهلة محلية توحي خطأً بأنها تؤخر أمر المنصة.
    SL_TIMEOUT_ENABLED = os.getenv("SL_TIMEOUT_ENABLED", "false").lower() == "true"
    SL_TIMEOUT_SECONDS = int(os.getenv("SL_TIMEOUT_SECONDS", 60))

    # Trailing Take Profit — جني الأرباح الملاحق (يُفعَّل بعد آخر هدف)
    # TP3 الكامل موجود على المنصة. لا نعرض TTP وهمياً عند نفس السعر.
    TTP_ENABLED           = os.getenv("TTP_ENABLED", "false").lower() == "true"
    TTP_DEVIATION_PCT     = float(os.getenv("TTP_DEVIATION_PCT", 0.005))  # 0.5% تراجع يُغلق الصفقة

    # ==========================================================
    # Protection Features
    # ==========================================================

    # [ميزة 1: StoplossGuard]
    SL_GUARD_ENABLED = True
    SL_GUARD_TRADE_LIMIT      = int(os.getenv("SL_GUARD_TRADE_LIMIT", 3))
    SL_GUARD_LOOKBACK_MINUTES = int(os.getenv("SL_GUARD_LOOKBACK_MINUTES", 60))
    SL_GUARD_PAUSE_MINUTES    = int(os.getenv("SL_GUARD_PAUSE_MINUTES", 60))

    # [ميزة 2: MaxDrawdown]
    MAX_DRAWDOWN_ENABLED = True
    MAX_DRAWDOWN_PCT = 5.0
    MAX_DRAWDOWN_LOOKBACK_HOURS = 24
    MAX_DRAWDOWN_PAUSE_HOURS = 4
    ACCOUNT_HISTORY_MAX_PAGES = int(os.getenv("ACCOUNT_HISTORY_MAX_PAGES", "20"))

    # [ميزة 3: LowProfitPairs]
    LOW_PROFIT_ENABLED = True
    LOW_PROFIT_LOSS_COUNT = 2
    LOW_PROFIT_FREEZE_HOURS = 2

    # [ميزة 4: SL On Exchange]
