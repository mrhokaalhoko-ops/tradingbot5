# strategy/filter_profiles.py
#
# مركز تعريف بروفايلات الفلاتر — 6 مستويات متدرجة
# كل بروفايل يحدد بالضبط ما الذي يشتغل وما الذي يُوقَف في محرك SMC والقرار.

from core.logger import logger


FILTER_PROFILE_KEY = "trading_filter_profile"

# ─────────────────────────────────────────────────────────────────
# تعريف البروفايلات
#
# المفاتيح:
#   label              — الاسم القصير المعروض في التيليجرام
#   emoji              — الإيموجي الدال على المستوى
#   short              — الكود المختصر
#   opportunity_pct    — نسبة الفرص التقريبية من عملات السوق
#   suitable_for       — لمن هذا الخيار؟
#   not_suitable_for   — لمن لا يناسب؟
#
#   # ─── شروط محرك SMC (ta_engine) ───
#   require_ob         — هل يجب وجود Order Block صالح؟
#   require_sweep      — هل يُشترط Liquidity Sweep سابق لتكوين OB؟
#   require_bos        — هل يُشترط كسر هيكل السوق (Break of Structure)؟
#   max_ob_age         — الحد الأقصى لعمر الـ OB بالشموع (15 دقيقة/شمعة)
#   enable_fallback    — هل يعمل البديل RSI+MACD عند غياب OB؟
#
#   # ─── شروط الدخول الفعلي ───
#   require_volume     — هل يُشترط تأكيد السيولة (Volume) لحظة الدخول؟
#   require_entry_sweep — هل يُشترط Sweep إضافي لحظة تأكيد الدخول؟
#   allow_rr_fallback  — هل يُسمح بهدف بديل إذا لم يُحسب RR من SMC؟
#   fallback_rr        — قيمة RR البديلة
#   min_rr             — الحد الأدنى لنسبة العائد للمخاطرة
#   min_atr_pct        — الحد الأدنى لنسبة ATR/السعر لقبول الدخول
#
#   # ─── نصوص العرض ───
#   conditions_display — قائمة من dict: {emoji, term, explain, active}
# ─────────────────────────────────────────────────────────────────

FILTER_PROFILES = {

    # ══════════════════════════════════════════════════
    # 1. مؤسسي كامل — أشد المستويات حزماً
    # ══════════════════════════════════════════════════
    "institutional": {
        "label": "🏰 مؤسسي كامل",
        "emoji": "🏰",
        "short": "Institutional",
        "opportunity_pct": "1-2%",
        "suitable_for": "من يريد دقة قصوى ولا يمانع ندرة الفرص",
        "not_suitable_for": "من يريد صفقات متكررة أو يريد اختبار البوت بسرعة",

        "require_ob": True,
        "require_sweep": True,
        "require_bos": True,
        "max_ob_age": 60,
        "enable_fallback": False,

        "require_volume": True,
        "require_entry_sweep": True,
        "trigger_evidence_min": 3,
        "allow_countertrend_reversal": False,
        "allow_rr_fallback": False,
        "fallback_rr": None,
        "min_rr": 2.0,
        "min_atr_pct": 0.008,   # 0.80% — السوق يجب أن يكون متحركاً بما يكفي

        "conditions_display": [
            {"emoji": "✅", "term": "Order Block", "explain": "منطقة سعرية تجمّع فيها المال الذكي قبل حركة قوية", "active": True},
            {"emoji": "✅", "term": "Liquidity Sweep", "explain": "اصطياد وقفات البيع/الشراء قبل الانعكاس", "active": True},
            {"emoji": "✅", "term": "Break of Structure (BOS)", "explain": "كسر قمة أو قاع سابق يؤكد اتجاه جديد", "active": True},
            {"emoji": "✅", "term": "Volume Confirmation", "explain": "تأكيد أن الحجم كافٍ يدل على تدخل فعلي", "active": True},
            {"emoji": "✅", "term": "Entry Sweep", "explain": "تأكيد إضافي بأن السيولة اصطيدت لحظة الدخول", "active": True},
            {"emoji": "❌", "term": "Fallback RSI/MACD", "explain": "دخول بديل عند غياب OB — مغلق في هذا الوضع", "active": False},
            {"emoji": "⏳", "term": "عمر OB الأقصى", "explain": "عمر منطقة المال الذكي: 60 شمعة (15 ساعة فقط)", "active": True},
            {"emoji": "📊", "term": "نسبة العائد/المخاطرة (RR)", "explain": "الحد الأدنى للربح مقارنةً بالخسارة المحتملة", "active": True},
        ],
    },

    # ══════════════════════════════════════════════════
    # 2. SMC صارم — كل شروط SMC + EMA ثنائي
    # ══════════════════════════════════════════════════
    "smc_strict": {
        "label": "🔒 SMC صارم",
        "emoji": "🔒",
        "short": "SMC Strict",
        "opportunity_pct": "3-7%",
        "suitable_for": "التداول المحافظ مع التمسك باستراتيجية المال الذكي",
        "not_suitable_for": "من يريد نشاطاً يومياً أو صفقات متكررة",

        "require_ob": True,
        "require_sweep": True,
        "require_bos": True,
        "max_ob_age": 80,
        "enable_fallback": False,

        "require_volume": True,
        "require_entry_sweep": True,
        "trigger_evidence_min": 2,
        "allow_countertrend_reversal": False,
        "allow_rr_fallback": False,
        "fallback_rr": None,
        "min_rr": 1.5,
        "min_atr_pct": 0.007,   # 0.70%

        "conditions_display": [
            {"emoji": "✅", "term": "Order Block", "explain": "منطقة تجمّع المال الذكي — مطلوبة", "active": True},
            {"emoji": "✅", "term": "Liquidity Sweep", "explain": "اصطياد وقفات البيع/الشراء — مطلوب", "active": True},
            {"emoji": "✅", "term": "Break of Structure (BOS)", "explain": "كسر قمة/قاع سابق — مطلوب", "active": True},
            {"emoji": "✅", "term": "Volume Confirmation", "explain": "تأكيد السيولة الكافية — مطلوب", "active": True},
            {"emoji": "✅", "term": "Entry Sweep", "explain": "تأكيد اصطياد السيولة لحظة الدخول — مطلوب", "active": True},
            {"emoji": "❌", "term": "Fallback RSI/MACD", "explain": "دخول بديل بالمؤشرات عند غياب OB — مغلق", "active": False},
            {"emoji": "⏳", "term": "عمر OB الأقصى", "explain": "80 شمعة × 15 دقيقة = 20 ساعة", "active": True},
            {"emoji": "📊", "term": "نسبة العائد/المخاطرة (RR)", "explain": "1.5x كحد أدنى", "active": True},
        ],
    },

    # ══════════════════════════════════════════════════
    # 3. SMC متوازن — OB + BOS بدون اشتراط Volume/Sweep لحظة الدخول
    # ══════════════════════════════════════════════════
    "smc_balanced": {
        "label": "⚖️ SMC متوازن",
        "emoji": "⚖️",
        "short": "SMC Balanced",
        "opportunity_pct": "غير ثابت — يُقاس من سجل القرار",
        "suitable_for": "التوازن بين الأمان والنشاط — الخيار الموصى به للبداية",
        "not_suitable_for": "من يريد أقصى درجات الأمان أو أكبر قدر من الصفقات",

        "require_ob": True,
        "require_sweep": True,
        "require_bos": True,
        "max_ob_age": 100,
        "enable_fallback": False,

        "require_volume": False,
        "require_entry_sweep": False,
        # لا نسمي إغلاق wick وحده "تأكيداً": يلزم دليل واحد على الأقل من
        # الحجم أو sweep لحظة الدخول، دون تحويل البروفايل إلى الصارم.
        "min_sniper_confirmations": 1,
        "trigger_evidence_min": 1,
        "allow_countertrend_reversal": True,
        # لا نختلق هدف 4R عندما لا توجد سيولة مقابلة حقيقية.
        "allow_rr_fallback": False,
        "fallback_rr": None,
        "min_rr": 1.5,
        "min_atr_pct": 0.005,   # 0.50%

        "conditions_display": [
            {"emoji": "✅", "term": "Order Block", "explain": "منطقة تجمّع المال الذكي — مطلوبة", "active": True},
            {"emoji": "✅", "term": "Liquidity Sweep", "explain": "اصطياد وقفات البيع/الشراء — مطلوب لتكوين OB", "active": True},
            {"emoji": "✅", "term": "Break of Structure (BOS)", "explain": "كسر قمة/قاع سابق — مطلوب", "active": True},
            {"emoji": "🔁", "term": "Volume Confirmation", "explain": "يلزم الحجم أو Entry Sweep — واحد منهما على الأقل", "active": True},
            {"emoji": "🔁", "term": "Entry Sweep", "explain": "يلزم Sweep أو تأكيد الحجم — واحد منهما على الأقل", "active": True},
            {"emoji": "❌", "term": "Fallback RSI/MACD", "explain": "دخول بديل عند غياب OB — مغلق", "active": False},
            {"emoji": "⏳", "term": "عمر OB الأقصى", "explain": "100 شمعة × 15 دقيقة = 25 ساعة", "active": True},
            {"emoji": "📊", "term": "نسبة العائد/المخاطرة (RR)", "explain": "1.5x كحد أدنى", "active": True},
        ],
    },

    # ══════════════════════════════════════════════════
    # 4. SMC + Fallback — يُضاف دخول RSI/MACD عند غياب OB
    # ══════════════════════════════════════════════════
    "smc_fallback": {
        "label": "🔓 SMC + بديل",
        "emoji": "🔓",
        "short": "SMC + Fallback",
        "opportunity_pct": "25-40%",
        "suitable_for": "من يريد صفقات أكثر مع الإبقاء على أسلوب SMC كأولوية",
        "not_suitable_for": "من يريد التمسك الكامل بمنهجية المال الذكي فقط",

        "require_ob": True,
        "require_sweep": True,
        "require_bos": True,
        "max_ob_age": 100,
        "enable_fallback": True,

        "require_volume": False,
        "require_entry_sweep": False,
        "trigger_evidence_min": 1,
        "allow_countertrend_reversal": True,
        "allow_rr_fallback": True,
        "fallback_rr": 1.3,
        "min_rr": 1.3,
        "min_atr_pct": 0.004,   # 0.40%

        "conditions_display": [
            {"emoji": "✅", "term": "Order Block", "explain": "منطقة تجمّع المال الذكي — مطلوبة عند وجودها", "active": True},
            {"emoji": "✅", "term": "Liquidity Sweep", "explain": "اصطياد وقفات البيع/الشراء — مطلوب لتكوين OB", "active": True},
            {"emoji": "✅", "term": "Break of Structure (BOS)", "explain": "كسر قمة/قاع سابق — مطلوب", "active": True},
            {"emoji": "〰️", "term": "Volume Confirmation", "explain": "تأكيد السيولة — غير مشترط", "active": False},
            {"emoji": "〰️", "term": "Entry Sweep", "explain": "اصطياد لحظة الدخول — غير مشترط", "active": False},
            {"emoji": "✅", "term": "Fallback RSI/MACD", "explain": "دخول بديل بمؤشرات RSI+MACD عند غياب OB — مفعّل", "active": True},
            {"emoji": "⏳", "term": "عمر OB الأقصى", "explain": "100 شمعة × 15 دقيقة = 25 ساعة", "active": True},
            {"emoji": "📊", "term": "نسبة العائد/المخاطرة (RR)", "explain": "1.3x كحد أدنى", "active": True},
        ],
    },

    # ══════════════════════════════════════════════════
    # 5. فني متقدم — RSI + MACD + ADX بدون OB إلزامي
    # ══════════════════════════════════════════════════
    "ta_advanced": {
        "label": "⚡ فني متقدم",
        "emoji": "⚡",
        "short": "TA Advanced",
        "opportunity_pct": "50-65%",
        "suitable_for": "الاختبار السريع أو من يثق بالمؤشرات الفنية التقليدية",
        "not_suitable_for": "التداول الحذر — هذا الوضع يقبل إشارات أكثر وبعضها أضعف",

        "require_ob": False,
        "require_sweep": False,
        "require_bos": False,
        "max_ob_age": 120,
        "enable_fallback": True,

        "require_volume": False,
        "require_entry_sweep": False,
        "trigger_evidence_min": 1,
        "allow_countertrend_reversal": True,
        "allow_rr_fallback": True,
        "fallback_rr": 1.2,
        "min_rr": 1.2,
        "min_atr_pct": 0.003,   # 0.30%

        "conditions_display": [
            {"emoji": "〰️", "term": "Order Block", "explain": "منطقة تجمّع المال الذكي — غير مشترطة في هذا الوضع", "active": False},
            {"emoji": "〰️", "term": "Liquidity Sweep", "explain": "اصطياد الوقفات — غير مشترط", "active": False},
            {"emoji": "〰️", "term": "Break of Structure (BOS)", "explain": "كسر قمة/قاع — غير مشترط", "active": False},
            {"emoji": "✅", "term": "RSI (مؤشر القوة النسبية)", "explain": "يقيس قوة الاتجاه (تشبع شراء / بيع) — مطلوب", "active": True},
            {"emoji": "✅", "term": "MACD (مؤشر التقارب والتباعد)", "explain": "يكشف التقاطعات ومناطق الزخم — مطلوب", "active": True},
            {"emoji": "✅", "term": "ADX ≥ 25 (مؤشر قوة الاتجاه)", "explain": "يؤكد أن السوق في ترند حقيقي لا عشوائي — مطلوب", "active": True},
            {"emoji": "✅", "term": "Fallback دائم", "explain": "يعمل بالمؤشرات الفنية فقط دون انتظار OB", "active": True},
            {"emoji": "📊", "term": "نسبة العائد/المخاطرة (RR)", "explain": "1.2x كحد أدنى", "active": True},
        ],
    },

    # ══════════════════════════════════════════════════
    # 6. اختبار حر — أقل القيود للاختبار السريع
    # ══════════════════════════════════════════════════
    "free_test": {
        "label": "🧪 اختبار حر",
        "emoji": "🧪",
        "short": "Free Test",
        "opportunity_pct": "75-90%",
        "suitable_for": "الاختبار السريع فقط — رؤية كيف يتصرف البوت بحرية",
        "not_suitable_for": "⚠️ غير مناسب للتداول الحقيقي أو أي تداول جدي",

        "require_ob": False,
        "require_sweep": False,
        "require_bos": False,
        "max_ob_age": 150,
        "enable_fallback": True,

        "require_volume": False,
        "require_entry_sweep": False,
        "trigger_evidence_min": 0,
        "allow_countertrend_reversal": True,
        "allow_rr_fallback": True,
        "fallback_rr": 1.0,
        "min_rr": 1.0,
        "min_atr_pct": 0.0025,  # 0.25% — الحد الأدنى المطلق للاختبار

        "conditions_display": [
            {"emoji": "〰️", "term": "Order Block", "explain": "منطقة تجمّع المال الذكي — غير مشترطة", "active": False},
            {"emoji": "〰️", "term": "Liquidity Sweep", "explain": "اصطياد الوقفات — غير مشترط", "active": False},
            {"emoji": "〰️", "term": "Break of Structure (BOS)", "explain": "كسر قمة/قاع — غير مشترط", "active": False},
            {"emoji": "〰️", "term": "Volume Confirmation", "explain": "تأكيد السيولة — غير مشترط", "active": False},
            {"emoji": "〰️", "term": "Entry Sweep", "explain": "اصطياد لحظة الدخول — غير مشترط", "active": False},
            {"emoji": "✅", "term": "ATR (متوسط نطاق الحركة)", "explain": "يُعطي أولوية للعملات ذات التذبذب الكافي فقط", "active": True},
            {"emoji": "✅", "term": "Fallback دائم", "explain": "يعمل بالمؤشرات الفنية فقط", "active": True},
            {"emoji": "📊", "term": "نسبة العائد/المخاطرة (RR)", "explain": "1.0x كحد أدنى", "active": True},
        ],
    },
}


# ─────────────────────────────────────────────────────────────────
# ترتيب العرض في تيليجرام (من الأكثر تحفظاً إلى الأكثر حرية)
# ─────────────────────────────────────────────────────────────────
FILTER_PROFILE_ORDER = [
    "institutional",
    "smc_strict",
    "smc_balanced",
    "smc_fallback",
    "ta_advanced",
    "free_test",
]


def normalize_filter_profile(value: str) -> str:
    val_clean = str(value or "").lower().strip()
    if not val_clean:
        return "smc_strict"
    if val_clean not in FILTER_PROFILES:
        logger.warning(f"[FilterProfile] قيمة غير معروفة '{value}'، تم الرجوع لـ smc_strict تلقائياً.")
        return "smc_strict"
    return val_clean


def get_filter_profile(state_manager=None) -> dict:
    """
    قراءة وضع الفلاتر الحالي من state_manager.
    الافتراضي smc_strict حفاظاً على الأمان.
    """
    profile_name = "smc_strict"

    try:
        if state_manager:
            saved = state_manager.get(FILTER_PROFILE_KEY, "smc_strict")
            # backward compat: القيم القديمة (strict/medium/relaxed) → smc_strict
            _legacy_map = {
                "strict":  "smc_strict",
                "medium":  "smc_balanced",
                "relaxed": "smc_fallback",
            }
            profile_name = _legacy_map.get(saved, saved)
    except Exception as e:
        logger.warning(f"[FilterProfile] تعذر قراءة وضع الفلاتر، سيتم استخدام smc_strict: {e}")
        profile_name = "smc_strict"

    profile_name = normalize_filter_profile(profile_name)
    profile = FILTER_PROFILES[profile_name].copy()
    profile["key"] = profile_name
    return profile


def set_filter_profile(state_manager, profile_name: str) -> dict:
    profile_name = normalize_filter_profile(profile_name)

    if state_manager:
        state_manager.set(FILTER_PROFILE_KEY, profile_name)

    profile = FILTER_PROFILES[profile_name].copy()
    profile["key"] = profile_name

    logger.info(f"[FilterProfile] تم تغيير وضع الفلاتر إلى: {profile_name}")
    return profile


def format_filter_profile_status(state_manager=None) -> str:
    """رسالة الحالة الموجزة للبروفايل الحالي."""
    profile = get_filter_profile(state_manager)

    lines = []
    lines.append(f"🎛️ <b>مركز الفلاتر والشروط</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔰 البروفايل الحالي: <b>{profile['label']}</b>")
    lines.append(f"📈 رصد تاريخي تقريبي للمرشحات: <code>{profile['opportunity_pct']}</code>")
    lines.append(f"📉 الحد الأدنى للتذبذب (ATR): <code>{profile['min_atr_pct']*100:.2f}%</code>")
    lines.append("")
    lines.append("⚙️ <b>الشروط النشطة:</b>")
    for c in profile.get("conditions_display", []):
        lines.append(f"  {c['emoji']} {c['term']}")
    lines.append("")
    lines.append(f"✅ مناسب لـ: {profile['suitable_for']}")

    return "\n".join(lines)


def format_profile_detail(profile_name: str) -> str:
    """
    رسالة تفصيلية كاملة لبروفايل معين قبل تأكيد التطبيق.
    تُستخدم في شاشة التأكيد ذات المرحلتين.
    """
    profile_name = normalize_filter_profile(profile_name)
    p = FILTER_PROFILES[profile_name]

    lines = []
    lines.append(f"{p['emoji']} <b>{p['label']}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("📋 <b>الشروط التفصيلية:</b>")

    for c in p.get("conditions_display", []):
        lines.append(f"  {c['emoji']} <b>{c['term']}</b> — {c['explain']}")

    lines.append("")
    lines.append(f"  📉 الحد الأدنى للتذبذب (ATR): <code>{p['min_atr_pct']*100:.2f}%</code> من سعر العملة")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <b>التوقعات:</b>")
    lines.append(f"  📈 رصد تاريخي تقريبي للمرشحات: <code>{p['opportunity_pct']}</code>")
    lines.append(f"  ✅ مناسب لـ: {p['suitable_for']}")
    lines.append(f"  ⛔ غير مناسب لـ: {p['not_suitable_for']}")

    if profile_name == "free_test":
        lines.append("")
        lines.append("⚠️ <b>تحذير:</b> هذا الوضع مخصص للاختبار فقط.")
        lines.append("لا يُنصح باستخدامه مع رأس مال حقيقي.")

    lines.append("")
    lines.append("هل تريد تطبيق هذا البروفايل الآن؟")

    return "\n".join(lines)
