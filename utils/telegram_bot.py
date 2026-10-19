import requests
import threading
import time
import os
import sys
import re
import hashlib
import math
from html import escape, unescape
from core.config import Config
from core.logger import logger
from core.version import (
    APP_VERSION,
    DEPLOYMENT_ID,
    GIT_BRANCH,
    GIT_COMMIT,
    GIT_REPO,
    SHORT_COMMIT,
    VERSION_LABEL,
)
from utils.performance_tracker import PerformanceTracker
from utils.api_status_monitor import APIStatusMonitor
from strategy.filter_profiles import (
    FILTER_PROFILES,
    FILTER_PROFILE_ORDER,
    get_filter_profile,
    set_filter_profile,
    format_filter_profile_status,
    format_profile_detail,
)


# تصدير 24/48/72 قد يُطلب سريعاً من الأزرار الثلاثة. تشغيلها معاً كان
# يضاعف قراءة/ضغط السجلات ويرفع RSS بقوة، لذلك تُنفذ كطابور داخل العملية.
_runtime_export_lock = threading.Lock()


def _fetch_live_positions_for_status(client, state_manager):
    """يجلب المراكز للحالة دون مسح الذاكرة المحلية عند فشل Binance."""
    try:
        all_positions = client.exchange.fetch_positions()
        live_positions = [
            position for position in (all_positions or [])
            if float((position.get("info", {}) or {}).get("positionAmt", 0) or 0) != 0
        ]
    except Exception as exc:
        logger.error(f"فشل جلب الصفقات المباشرة في /status: {exc}")
        return None

    live_symbols = {
        position["symbol"].split(":")[0] for position in live_positions
    }
    state_manager.save_active_symbols(live_symbols)
    return live_positions


class TelegramNotifier:
    """
    Telegram GUI/UX Enhanced Notifier

    ✅ التحسينات هنا تخص تجربة المستخدم في تيليجرام فقط:
    - أزرار Inline Keyboard احترافية.
    - قائمة رئيسية ثابتة.
    - أزرار داخل رسائل الحالة والتحليل.
    - دعم callback_query للأزرار.
    - رسائل ترحيب ومساعدة محسّنة.
    - تنسيق أفضل للتقارير.
    - لا يوجد تغيير في منطق التداول أو إدارة الصفقات.
    """

    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

        logger.info(
            "[Build] version=%s commit=%s branch=%s repo=%s deployment=%s",
            APP_VERSION,
            GIT_COMMIT or "unavailable",
            GIT_BRANCH or "unavailable",
            GIT_REPO or "unavailable",
            DEPLOYMENT_ID or "unavailable",
        )

        # حالات انتظار إدخال المستخدم، مفصولة حسب chat_id لتجنب تداخل الأوامر.
        self.user_state = {}
        self.selected_candles_state = {}
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self._poll_session = None

        # ── قاموس أرقام تعريف الأيقونات المخصصة (Telegram Bot API 9.4) ──
        # ملاحظة: يجب استبدال المعرفات بالقيم الحقيقية بعد رفعها لبوت @Stickers
        self.ICONS = {
            "bar_chart":        "5951542220791226980",   # bar chart.png
            "blacklist_ban":     "PASTE_ID_HERE",   # blacklist ban.png
            "brain_ai":          "PASTE_ID_HERE",   # brain AI analysis.png
            "filter_settings":   "PASTE_ID_HERE",   # filter settings.png
            "heartbeat":         "PASTE_ID_HERE",   # heartbeat pulse.png
            "home":              "PASTE_ID_HERE",   # home button.png
            "leverage_gauge":    "PASTE_ID_HERE",   # leverage gauge.png
            "lightning":         "PASTE_ID_HERE",   # lightning bolt.png
            "max_limit_gauge":   "PASTE_ID_HERE",   # max limit gauge.png
            "percentage_chart":  "PASTE_ID_HERE",   # percentage chart.png
            "power_on":          "PASTE_ID_HERE",   # power on button.png
            "question_help":     "PASTE_ID_HERE",   # question mark help.png
            "refresh_restart":   "PASTE_ID_HERE",   # refresh restart.png
            "report_document":   "PASTE_ID_HERE",   # report document.png
            "shield_protection": "PASTE_ID_HERE",   # shield protection.png
            "stop_emergency":    "PASTE_ID_HERE",   # stop sign emergency.png
            "test_tube":         "PASTE_ID_HERE",   # test tube.png
            "timer_clock":       "PASTE_ID_HERE",   # timer clock.png
            "trash_delete":      "PASTE_ID_HERE",   # trash bin delete.png
            "wallet_money":      "PASTE_ID_HERE",   # wallet money.png
        }

        # The dedicated Demo proposal executor shares the Production bot token
        # only for outbound trade lifecycle alerts. When polling is disabled it
        # must not mutate the shared bot command list or Mini App menu button.
        if Config.TELEGRAM_POLLING_ENABLED:
            self.set_bot_commands()
        else:
            logger.info(
                "[Telegram] polling disabled; command and Mini App menu "
                "registration skipped."
            )

    # ==========================================================
    # Telegram API Helpers
    # ==========================================================
    def _strip_leading_emoji(self, text: str) -> str:
        """
        إزالة الإيموجيات القديمة/البدائية من بداية النص لتجنب التكرار عند تفعيل الأيقونة المخصصة.
        تزيل أي رموز أو إيموجيات يونيكود في بداية السطر حتى تصل للحروف أو الأرقام.
        """
        # تعبير نمطي يطابق أي رموز تعبيرية أو أشكال هندسية غير alphanumeric في البداية
        cleaned = re.sub(r'^[^\w\s\(\)\[\]\{\}\d\+\-\*\/]+', '', text).strip()
        return cleaned if cleaned else text

    def _make_btn(self, text: str, callback_data: str = None, style: str = None, web_app: dict = None, icon_key: str = None) -> dict:
        """
        أداة مساعدة لبناء أزرار الكيبورد بشكل آمن:
        تتأكد من وجود المعرّف وصحته قبل إضافته لمنع أخطاء Bad Request من خوادم تيليجرام.
        كما تقوم بإزالة الإيموجي التقليدي من النص تلقائياً إذا تفعّلت الأيقونة المخصصة.
        """
        btn = {"text": text}
        if callback_data:
            btn["callback_data"] = callback_data
        if style:
            btn["style"] = style
        if web_app:
            btn["web_app"] = web_app
        if icon_key and hasattr(self, "ICONS"):
            icon_id = self.ICONS.get(icon_key)
            # نتحقق أن المعرف حقيقي وليس Placeholder فارغ أو يبدأ بـ PASTE
            if icon_id and not icon_id.startswith("PASTE_") and not icon_id.startswith("YOUR_") and icon_id.strip():
                btn["icon_custom_emoji_id"] = icon_id
                # إزالة الإيموجي البدائي من النص ليكون المظهر ناصعاً ومطابقاً للصور المرفقة
                btn["text"] = self._strip_leading_emoji(text)
        return btn
    # ==========================================================
    def _is_configured(self):
        return bool(
            self.token
            and self.token != "your_telegram_bot_token_here"
            and self.chat_id
        )

    def _post(self, method: str, payload: dict, timeout: int = 10):
        if not self._is_configured():
            logger.debug("إعدادات تيليجرام غير مكتملة، تم تخطي الطلب.")
            return None

        url = f"{self.base_url}/{method}"
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if method == "editMessageText" and response.status_code == 400:
                pass # Message is too old to edit, ignore the spammy log
            else:
                logger.error(f"فشل طلب تيليجرام {method}: {e}")
            return None
        except Exception as e:
            logger.error(f"فشل طلب تيليجرام {method}: {e}")
            return None

    def _get(self, method: str, params: dict = None, timeout: int = 10):
        if not self._is_configured():
            logger.debug("إعدادات تيليجرام غير مكتملة، تم تخطي الطلب.")
            return None

        url = f"{self.base_url}/{method}"
        try:
            response = requests.get(url, params=params or {}, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"فشل طلب تيليجرام {method}: {e}")
            return None

    def _safe_html(self, value):
        return escape(str(value or ""), quote=False)

    @staticmethod
    def _scan_direction_label(side):
        """اسم تداول واضح بدلاً من إخفاء BUY/SELL داخل القرار الداخلي."""
        normalized = str(side or "").strip().lower()
        if normalized == "buy":
            return "🟢 LONG — شراء"
        if normalized == "sell":
            return "🔴 SHORT — بيع"
        return ""

    @staticmethod
    def _scan_stage_label(stage):
        labels = {
            "SETUP_COOLDOWN": "تكرار نفس الفكرة",
            "DERIVATIVES_FILTER": "مخاطر سوق العقود",
            "ANTI_PUMP_FILTER": "الحماية من مطاردة الصعود",
            "QUALITY_FILTER": "جودة نقطة الدخول",
            "DATA_QUALITY": "جودة البيانات",
            "RR_FILTER": "العائد مقابل المخاطرة",
            "EXECUTION": "تنفيذ الأمر",
        }
        return labels.get(str(stage or "").strip().upper(), "شروط الدخول")

    @staticmethod
    def _scan_trend_label(trend):
        labels = {
            "bullish": "صاعد",
            "bearish": "هابط",
            "neutral": "محايد",
            "unknown": "غير معروف",
        }
        return labels.get(str(trend or "").strip().lower(), "")

    @staticmethod
    def _format_scan_price(value):
        try:
            price = float(value or 0)
        except (TypeError, ValueError):
            return ""
        if price <= 0:
            return ""
        if price >= 1000:
            return f"{price:,.2f}"
        if price >= 1:
            return f"{price:.4f}"
        if price >= 0.01:
            return f"{price:.5f}"
        return f"{price:.8f}".rstrip("0").rstrip(".")

    def _scan_rejection_explanation(self, result):
        """تحويل سبب الرفض التقني إلى تفسير مختصر قابل للقراءة."""
        stage = str(result.get("rejection_stage") or "").upper()
        plan = result.get("target_plan") or {}
        if stage == "RR_FILTER" and isinstance(plan, dict):
            try:
                rr = float(plan.get("rr"))
                min_rr = float(plan.get("min_rr"))
            except (TypeError, ValueError):
                rr = min_rr = None
            if rr is not None and min_rr is not None:
                return (
                    f"الهدف الواقعي الأقرب يعطي عائداً {rr:.2f} مقابل مخاطرة 1، "
                    f"بينما الحد المطلوب {min_rr:.2f}؛ لذلك لم يدخل البوت."
                )

        reason = str(result.get("reason") or "").strip()
        reason = re.sub(r"^تم حظر الصفقة:\s*", "", reason)
        reason = reason.replace("RR غير كافٍ", "عائد مقابل المخاطرة غير كافٍ")
        reason = reason.replace("RR ضعيف", "عائد مقابل المخاطرة ضعيف")
        reason = reason.replace("ATR=0", "بيانات التذبذب (ATR) غير متاحة")
        return reason or "لم تستوفِ الإشارة شروط الدخول الآمن."

    def _strip_html(self, text: str) -> str:
        """إزالة وسوم HTML عند فشل الإرسال."""
        return unescape(re.sub(r"<[^>]+>", "", str(text or "")))

    def _sanitize_html(self, text: str) -> str:
        """
        تنظيف النص وإبقاء الوسوم المسموحة من تيليجرام فقط.
        أي وسم غير مسموح (مثل <!doctype>, <html>, <div>) يتم تحويله إلى نص آمن.
        """
        ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a"}
        
        def replace_tag(match):
            full_tag = match.group(0)          # مثال: <b> أو </b> أو <div class="x">
            tag_name = match.group(1).lower()  # مثال: b أو div أو !doctype
            # إذا الوسم مسموح (مع مراعاة الإغلاق /b) نتركه كما هو
            clean_name = tag_name.lstrip("/")
            if clean_name in ALLOWED_TAGS:
                return full_tag
            # غير مسموح: نحوّله إلى نص آمن
            return full_tag.replace("<", "&lt;").replace(">", "&gt;")
        
        sanitized = re.sub(
            r"<(/?[!\w][\w]*)([^>]*)>", replace_tag, str(text or "")
        )
        # Comparisons embedded in runtime reasons (for example ``1.08R <
        # 1.50R``) are plain text, not tags. Telegram's HTML parser rejects a
        # raw ``<`` and previously forced a second plain-text request for every
        # RR rejection. Preserve only the already-reviewed allowed tags.
        allowed = r"/?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>"
        sanitized = re.sub(
            rf"<(?!(?:{allowed}))",
            "&lt;",
            sanitized,
            flags=re.IGNORECASE,
        )
        # Preserve valid entities while escaping any other raw ampersand.
        return re.sub(
            r"&(?!(?:#\d+|#x[0-9A-Fa-f]+|\w+);)",
            "&amp;",
            sanitized,
        )

    def _split_html_message(self, text: str, limit: int = 3900) -> list:
        """Split HTML without cutting tags/entities; close and reopen active tags."""
        safe = self._sanitize_html(str(text or ""))
        token_pattern = re.compile(r"<[^>]+>|&(?:#\d+|#x[0-9A-Fa-f]+|\w+);|[^<&]|[<&]")
        tokens = token_pattern.findall(safe)
        chunks = []
        current = ""
        stack = []  # (tag_name, opening_tag)

        def closing_suffix(active):
            return "".join(f"</{name}>" for name, _ in reversed(active))

        def reopen_prefix(active):
            return "".join(opening for _, opening in active)

        def classify(token, active):
            match = re.fullmatch(r"<\s*(/?)\s*([A-Za-z0-9]+)[^>]*>", token)
            if not match:
                return list(active)
            closing, name = match.group(1), match.group(2).lower()
            next_stack = list(active)
            if closing:
                if next_stack and next_stack[-1][0] == name:
                    next_stack.pop()
            elif not token.rstrip().endswith("/>"):
                next_stack.append((name, token))
            return next_stack

        for token in tokens:
            next_stack = classify(token, stack)
            required = len(current) + len(token) + len(closing_suffix(next_stack))
            if current and required > limit:
                completed = current + closing_suffix(stack)
                if completed:
                    chunks.append(completed)
                current = reopen_prefix(stack)
                next_stack = classify(token, stack)
            if len(current) + len(token) + len(closing_suffix(next_stack)) > limit:
                # An abnormally long tag cannot be made safe as HTML.  Fall
                # back to bounded plain text rather than emitting invalid HTML.
                plain = self._strip_html(safe)
                return [
                    escape(plain[i:i + limit], quote=False)
                    for i in range(0, len(plain), limit)
                ] or [""]
            current += token
            stack = next_stack

        if current or not chunks:
            chunks.append(current + closing_suffix(stack))
        return chunks

    # ==========================================================
    # GUI Keyboards
    # ==========================================================
    def main_menu_keyboard(self):
        """
        القائمة الرئيسية التفاعلية.
        """
        web_url = os.getenv("WEBAPP_URL", "https://tradingbot-production-0b71.up.railway.app/")
        return {
            "inline_keyboard": [
                [
                    self._make_btn("🚀 Open Dashboard", web_app={"url": web_url}),
                ],
                [
                    self._make_btn("📊 حالة البوت", "gui_status", "primary", icon_key="bar_chart"),
                    self._make_btn("🧠 تحليل عملة", "gui_analyze", "primary", icon_key="brain_ai"),
                ],
                [
                    self._make_btn("📋 التقارير والأداء", "gui_reports_menu", "primary", icon_key="report_document"),
                ],
                [
                    self._make_btn("🎛️ مركز الفلاتر والشروط", "gui_filters_center", "primary", icon_key="filter_settings"),
                ],
                [
                    self._make_btn("🎯 وضع تنفيذ إشارات Demo", "gui_demo_signal_mode", "success"),
                ],
                [
                    self._make_btn("⏱️ فترة المسح", "gui_scan_interval", "success", icon_key="timer_clock"),
                    self._make_btn("⚡ مسح الآن", "force_scan_now", "success", icon_key="lightning"),
                ],
                [
                    self._make_btn("🟢 تفعيل البحث (إلغاء Kill Switch)", "reset_kill_switch", "success", icon_key="power_on"),
                ],
                [
                    self._make_btn("🩺 صحة النظام", "gui_health", "primary", icon_key="heartbeat"),
                    self._make_btn("🧪 فحص الخدمات", "gui_api_status", "primary", icon_key="test_tube"),
                ],
                [
                    self._make_btn("⚙️ الحد الأقصى", "gui_setmax", "primary", icon_key="max_limit_gauge"),
                    self._make_btn("⚙️ الرافعة", "gui_leverage", "primary", icon_key="leverage_gauge"),
                    self._make_btn("🛡️ مخاطرة الوقف", "gui_risk", "primary", icon_key="percentage_chart"),
                ],
                [
                    self._make_btn("💰 محفظة محاكاة التقارير", "gui_ref_balance", "primary", icon_key="wallet_money"),
                    self._make_btn("🛡️ أنظمة الحماية", "gui_protections", "primary", icon_key="shield_protection"),
                ],
                [
                    self._make_btn("🚫 القائمة السوداء (Blacklist)", "gui_blacklist_menu", "danger", icon_key="blacklist_ban"),
                ],
                [
                    self._make_btn("🔄 إعادة تشغيل", "gui_restart_confirm", "danger", icon_key="refresh_restart"),
                    self._make_btn("🛑 إيقاف طوارئ", "gui_stop_confirm", "danger", icon_key="stop_emergency"),
                ],
                [
                    self._make_btn("🗑️ مسح بيانات البوت", "gui_reset_confirm", "danger", icon_key="trash_delete"),
                ],
                [
                    self._make_btn(
                        f"🏷️ الإصدار {VERSION_LABEL}",
                        "gui_version",
                        "primary",
                    ),
                ],
                [
                    self._make_btn("❓ المساعدة", "gui_help", "primary", icon_key="question_help"),
                    self._make_btn("🏠 القائمة الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }


    def back_home_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("🏠 القائمة الرئيسية", "gui_home", "primary", icon_key="home"),
                    self._make_btn("🔄 تحديث الحالة", "gui_status", "success", icon_key="refresh_restart"),
                ]
            ]
        }

    def analyze_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("BTC",  "analyze_BTC",  "primary"),
                    self._make_btn("ETH",  "analyze_ETH",  "primary"),
                    self._make_btn("BNB",  "analyze_BNB",  "primary"),
                ],
                [
                    self._make_btn("SOL",  "analyze_SOL",  "primary"),
                    self._make_btn("XRP",  "analyze_XRP",  "primary"),
                    self._make_btn("DOGE", "analyze_DOGE", "primary"),
                ],
                [
                    self._make_btn("✍️ إدخال يدوي", "gui_analyze_manual", "primary"),
                    self._make_btn("🏠 الرئيسية",   "gui_home",            "primary", icon_key="home"),
                ],
            ]
        }

    def reports_menu_keyboard(self):
        """
        قائمة التقارير الفرعية.
        """
        return {
            "inline_keyboard": [
                [
                    self._make_btn("📆 تقرير يومي",   "report_daily",   "primary", icon_key="report_document"),
                    self._make_btn("🗓️ تقرير أسبوعي", "report_weekly",  "primary", icon_key="report_document"),
                ],
                [
                    self._make_btn("📅 تقرير شهري",   "report_monthly", "primary", icon_key="report_document"),
                    self._make_btn("🏆 تقرير سنوي",   "report_yearly",  "primary", icon_key="report_document"),
                ],
                [
                    self._make_btn("🕯️ سجلات الشموع والصفقات", "gui_candles_menu",  "primary"),
                ],
                [
                    self._make_btn("🔥 القائمة الساخنة — مراقبة فقط", "hot_list_shadow", "primary"),
                ],
                [
                    self._make_btn("📥 سجل آخر ساعة", "export_runtime_logs_1h", "primary"),
                    self._make_btn("⏱️ مدة مخصصة بالدقائق", "export_runtime_logs_custom_minutes", "primary"),
                ],
                [
                    self._make_btn("📥 سجل 24 ساعة", "export_runtime_logs_24h", "primary"),
                    self._make_btn("📥 سجل 48 ساعة", "export_runtime_logs_48h", "primary"),
                    self._make_btn("📥 سجل 72 ساعة", "export_runtime_logs_72h", "primary"),
                ],
                [
                    self._make_btn("🧪 بدء اختبار 72 ساعة", "start_72h_test_session", "success"),
                    self._make_btn("📦 حزمة اختبار 72 ساعة", "export_72h_test_bundle", "primary"),
                ],
                [
                    self._make_btn("💾 مساحة التخزين المتاحة", "storage_status", "primary"),
                    self._make_btn("🧹 تنظيف السجلات المؤرشفة", "storage_cleanup_request", "danger"),
                ],
                [
                    self._make_btn("🧽 تهيئة سجلات اختبار جديد", "storage_fresh_test_request", "danger"),
                ],
                [
                    self._make_btn("📓 مراجعة الصفقات (Journal)", "gui_journal_menu", "primary", icon_key="report_document"),
                ],
                [
                    self._make_btn("🏠 الرجوع للرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def journal_menu_keyboard(self):
        """
        قائمة مراجعة الصفقات الفرعية.
        """
        return {
            "inline_keyboard": [
                [
                    self._make_btn("📊 آخر 5 صفقات",    "journal_last5",     "primary", icon_key="bar_chart"),
                ],
                [
                    self._make_btn("🕐 آخر 24 ساعة",     "journal_24h",       "primary", icon_key="timer_clock"),
                ],
                [
                    self._make_btn("🔍 تقييم عملة محددة", "journal_pick_coin", "primary"),
                ],
                [
                    self._make_btn("↩️ رجوع للتقارير", "gui_reports_menu", "primary"),
                    self._make_btn("🏠 الرئيسية",      "gui_home",          "primary", icon_key="home"),
                ],
            ]
        }

    def storage_cleanup_confirm_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("✅ تأكيد التنظيف الآمن", "storage_cleanup_confirm", "danger"),
                ],
                [
                    self._make_btn("↩️ إلغاء والعودة", "gui_reports_menu", "primary"),
                ],
            ]
        }

    def storage_fresh_test_confirm_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn(
                        "⚠️ نعم، امسح السجلات القديمة", "storage_fresh_test_confirm", "danger"
                    ),
                ],
                [
                    self._make_btn("↩️ إلغاء والعودة", "gui_reports_menu", "primary"),
                ],
            ]
        }

    def setmax_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("1",  "setmax_1",  "primary"),
                    self._make_btn("2",  "setmax_2",  "primary"),
                    self._make_btn("3",  "setmax_3",  "primary"),
                    self._make_btn("5",  "setmax_5",  "primary"),
                ],
                [
                    self._make_btn("✍️ إدخال يدوي", "gui_setmax_manual", "primary"),
                ],
                [
                    self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def leverage_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("1x",  "setlev_1",  "primary"),
                    self._make_btn("3x",  "setlev_3",  "primary"),
                    self._make_btn("5x",  "setlev_5",  "primary"),
                    self._make_btn("10x", "setlev_10", "primary", icon_key="leverage_gauge"),
                ],
                [self._make_btn("✍️ إدخال يدوي", "gui_setlev_manual", "primary")],
                [
                    self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def ref_balance_keyboard(self, state_manager=None):
        current = 60.0
        if state_manager:
            val = state_manager.get("reference_balance")
            if val is not None:
                current = float(val)

        def label(v):
            return f"✅ ${v}" if abs(current - v) < 0.01 else f"${v}"

        def lstyle(v):
            return "success" if abs(current - v) < 0.01 else "primary"

        return {
            "inline_keyboard": [
                [
                    self._make_btn(label(30),  "setref_30",  lstyle(30),  icon_key="wallet_money"),
                    self._make_btn(label(50),  "setref_50",  lstyle(50),  icon_key="wallet_money"),
                    self._make_btn(label(60),  "setref_60",  lstyle(60),  icon_key="wallet_money"),
                    self._make_btn(label(100), "setref_100", lstyle(100), icon_key="wallet_money"),
                ],
                [
                    self._make_btn(label(150), "setref_150", lstyle(150), icon_key="wallet_money"),
                    self._make_btn(label(200), "setref_200", lstyle(200), icon_key="wallet_money"),
                    self._make_btn("✍️ إدخال يدوي", "gui_setref_manual", "primary"),
                ],
                [
                    self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def risk_keyboard(self, state_manager=None):
        """لوحة اختيار مخاطرة الوقف الأساسية لكل صفقة."""
        current = (
            state_manager.get_risk_pct()
            if state_manager else float(Config.RISK_PER_TRADE_PERCENT)
        )

        def label(v):
            return f"✅ {v}%" if abs(current - v) < 0.001 else f"{v}%"

        def lstyle(v):
            return "success" if abs(current - v) < 0.001 else "primary"

        return {
            "inline_keyboard": [
                [
                    self._make_btn(label(0.25), "setrisk_0.25", lstyle(0.25), icon_key="percentage_chart"),
                    self._make_btn(label(0.5),  "setrisk_0.5",  lstyle(0.5),  icon_key="percentage_chart"),
                    self._make_btn(label(1.0),  "setrisk_1.0",  lstyle(1.0),  icon_key="percentage_chart"),
                ],
                [
                    self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def blacklist_menu_keyboard(self):
        """قائمة إدارة العملات المحظورة (القائمة السوداء)"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("➕ إضافة عملة (حظر)", "gui_blacklist_add", "danger", icon_key="blacklist_ban"),
                    self._make_btn("➖ إزالة عملة (فك الحظر)", "gui_blacklist_remove", "success"),
                ],
                [
                    self._make_btn("📋 عرض المحظورات", "gui_blacklist_show", "primary", icon_key="report_document"),
                ],
                [
                    self._make_btn("🔙 القائمة الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    # ==========================================================
    # 🎛️ Filter Center Keyboards (New)
    # ==========================================================

    def filters_center_keyboard(self):
        """الشاشة الرئيسية لمركز الفلاتر — 3 خيارات: البروفايل + المشاعر + المتوسطات"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("🔰 اختيار البروفايل (مستوى الشروط)", "gui_filter_profile_list", "primary", icon_key="filter_settings"),
                ],
                [
                    self._make_btn("🧠 وضع المشاعر (Sentiment)", "gui_sentiment_mode", "primary", icon_key="brain_ai"),
                    self._make_btn("📈 وضع المتوسطات (EMA)",    "gui_ema_mode",        "primary", icon_key="bar_chart"),
                ],
                [
                    self._make_btn("🏠 الرجوع للرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def demo_signal_mode_keyboard(self, state_manager):
        """Switch between the original strict path and direct Hybrid Demo."""
        current = state_manager.get_demo_signal_execution_mode()
        strict_mark = "✅ " if current == "strict_proposal" else ""
        hybrid_mark = "✅ " if current == "hybrid_decision" else ""
        return {
            "inline_keyboard": [
                [
                    self._make_btn(
                        f"{hybrid_mark}⚡ تنفيذ كل BUY/SELL",
                        "demo_signal_mode_hybrid_decision",
                        "success" if current == "hybrid_decision" else "primary",
                    )
                ],
                [
                    self._make_btn(
                        f"{strict_mark}🔒 الوضع الصارم السابق",
                        "demo_signal_mode_strict_proposal",
                        "success" if current == "strict_proposal" else "primary",
                    )
                ],
                [
                    self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

    def filter_profile_list_keyboard(self, state_manager=None):
        """قائمة البروفايلات الست — يُعلَّم الوضع الحالي بـ ✅"""
        current = get_filter_profile(state_manager).get("key", "smc_strict")
        rows = []
        for key in FILTER_PROFILE_ORDER:
            p = FILTER_PROFILES[key]
            active_mark = "✅ " if key == current else ""
            rows.append([
                self._make_btn(
                    f"{active_mark}{p['emoji']} {p['label'].split(' ', 1)[-1]} ({p['opportunity_pct']})",
                    f"filterprofile_preview_{key}",
                    "success" if key == current else "primary"
                )
            ])
        rows.append([
            self._make_btn("🔙 مركز الفلاتر", "gui_filters_center", "primary", icon_key="filter_settings"),
            self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
        ])
        return {"inline_keyboard": rows}

    def filter_profile_confirm_keyboard(self, profile_name: str):
        """أزرار التأكيد / الرجوع بعد عرض تفاصيل البروفايل"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("✅ تطبيق الآن", f"filterprofile_confirm_{profile_name}", "success"),
                    self._make_btn("↩️ رجوع للقائمة", "gui_filter_profile_list", "primary"),
                ],
            ]
        }

    def filter_profile_keyboard(self):
        """قائمة اختيار وضع الفلاتر (legacy — تعيد توجيه للقائمة الجديدة)"""
        return self.filter_profile_list_keyboard()

    def sentiment_mode_keyboard(self):
        """قائمة اختيار وضع تأثير المشاعر على قرار الدخول"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("🧠 كامل (المشاعر شرط)",        "sentimentmode_full",    "primary", icon_key="brain_ai")
                ],
                [
                    self._make_btn("📉 ثانوي (يعدّل الحجم فقط)",   "sentimentmode_weight",  "primary", icon_key="percentage_chart")
                ],
                [
                    self._make_btn("📊 معطّل (فني فقط)",           "sentimentmode_ta_only", "primary", icon_key="bar_chart")
                ],
                [
                    self._make_btn("🔙 مركز الفلاتر", "gui_filters_center", "primary", icon_key="filter_settings"),
                    self._make_btn("🏠 الرئيسية",     "gui_home",           "primary", icon_key="home"),
                ],
            ]
        }

    def ema_mode_keyboard(self):
        """قائمة اختيار وضع تأكيد الاتجاه عبر المتوسطات الأسية"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("📊 ثنائي (EMA 50, EMA 200) - الافتراضي", "emamode_dual",   "primary", icon_key="bar_chart")
                ],
                [
                    self._make_btn("📈 ثلاثي (EMA 50, EMA 100, EMA 200)",    "emamode_triple", "primary", icon_key="bar_chart")
                ],
                [
                    self._make_btn("🔙 مركز الفلاتر", "gui_filters_center", "primary", icon_key="filter_settings"),
                    self._make_btn("🏠 الرئيسية",     "gui_home",           "primary", icon_key="home"),
                ],
            ]
        }

    def scan_interval_keyboard(self):
        """قائمة إعداد فترة المسح"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("5 دقائق",  "setscan_300",  "primary", icon_key="timer_clock"),
                    self._make_btn("10 دقائق", "setscan_600",  "primary", icon_key="timer_clock"),
                    self._make_btn("15 دقيقة", "setscan_900",  "primary", icon_key="timer_clock"),
                ],
                [
                    self._make_btn("30 دقيقة", "setscan_1800", "primary", icon_key="timer_clock"),
                    self._make_btn("ساعة",     "setscan_3600", "primary", icon_key="timer_clock"),
                ],
                [
                    self._make_btn("✍️ إدخال مخصص", "gui_setscan_manual", "primary"),
                    self._make_btn("🏠 الرجوع",      "gui_home",           "primary", icon_key="home"),
                ],
            ]
        }

    def confirm_restart_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("✅ نعم، أعد التشغيل", "confirm_restart", "success", icon_key="refresh_restart"),
                    self._make_btn("❌ إلغاء", "gui_home", "danger", icon_key="home"),
                ]
            ]
        }

    def confirm_stop_keyboard(self):
        return {
            "inline_keyboard": [
                [
                    self._make_btn("🛑 نعم، أوقف الآن", "confirm_stop", "danger", icon_key="stop_emergency"),
                    self._make_btn("❌ إلغاء", "gui_home", "primary", icon_key="home"),
                ]
            ]
        }

    def confirm_reset_keyboard(self):
        """تأكيد مسح بيانات البوت — مرحلتان للأمان"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("✅ نعم، امسح كل البيانات", "confirm_reset_data", "danger", icon_key="trash_delete"),
                    self._make_btn("❌ إلغاء", "gui_home", "primary", icon_key="home"),
                ]
            ]
        }

    def handle_reset_bot_data(self, client, state_manager, chat_id, message_id):
        """
        مسح بيانات البوت بأمان:
        - يرفض العملية إذا كانت هناك صفقات مفتوحة على المنصة.
        - يمسح: bot_state.json, trade_journal/, testnet_trades_log.csv.
        - يُعيد تهيئة initial_balance من الرصيد الحالي.
        """
        import shutil, glob

        # 1. التحقق من عدم وجود صفقات مفتوحة
        try:
            positions = client.exchange.fetch_positions()
            open_pos  = [p for p in positions if float(p.get("info", {}).get("positionAmt", 0)) != 0]
            if open_pos:
                syms = ", ".join(p.get("symbol", "") for p in open_pos)
                self.edit_message(
                    chat_id, message_id,
                    f"⛔ <b>لا يمكن المسح!</b>\n"
                    f"هناك <b>{len(open_pos)}</b> صفقة مفتوحة حالياً:\n"
                    f"<code>{syms}</code>\n\n"
                    f"أغلق جميع الصفقات أولاً ثم اضغط مسح البيانات.",
                    self.main_menu_keyboard(),
                )
                return
        except Exception as e:
            self.edit_message(
                chat_id, message_id,
                f"⚠️ فشل التحقق من الصفقات المفتوحة: {e}",
                self.main_menu_keyboard(),
            )
            return

        cleared = []
        errors  = []

        # 2. مسح bot_state.json
        try:
            from utils.state_manager import CURRENT_STATE_SCHEMA_VERSION
            with state_manager._lock:
                state_manager.state = {
                    "schema_version": CURRENT_STATE_SCHEMA_VERSION,
                    "last_reset_date": "",
                    "risk_session_utc_date": "",
                    "initial_balance": 0.0,
                    "active_symbols": [],
                    "trailing_trades": {},
                    "entry_metadata": {},
                    "pair_locks": {},
                    "setup_cooldowns": {},
                    "consecutive_losses": {},
                    "virtual_orders": {},
                    "order_intents": {},
                    "archived_order_intents": {},
                    "close_intents": {},
                    "archived_close_intents": {},
                    "protection_replacements": {},
                    "archived_protection_replacements": {},
                    "recovery_complete": False,
                    "last_successful_reconciliation_at": None,
                    "last_scan_completed_at": None,
                    "sl_guard_enabled": True,
                    "sl_guard_trade_limit": 3,
                    "sl_guard_pause_until": None,
                    "max_drawdown_enabled": True,
                    "max_drawdown_pct": 5.0,
                    "max_drawdown_pause_until": None,
                    "low_profit_enabled": True,
                    "low_profit_loss_count": 2,
                    "frozen_pairs": {}
                }
            state_manager.save_state(force=True)
            cleared.append("bot_state.json")
        except Exception as e:
            errors.append(f"bot_state.json: {e}")

        # 3. مسح مجلد trade_journal
        journal_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
        try:
            if os.path.exists(journal_dir):
                shutil.rmtree(journal_dir)
                os.makedirs(journal_dir, exist_ok=True)
            cleared.append("trade_journal/")
        except Exception as e:
            errors.append(f"trade_journal: {e}")

        # 4. مسح سجل الصفقات CSV
        for pattern in ["testnet_trades_log.csv", "trades_log*.csv"]:
            for f in glob.glob(pattern):
                try:
                    os.remove(f)
                    cleared.append(f)
                except Exception as e:
                    errors.append(f"{f}: {e}")

        # 5. إعادة تهيئة initial_balance من الرصيد الحالي
        try:
            bal = client.get_balance()
            if bal:
                wallet_bal = float(bal.get("info", {}).get("totalWalletBalance", 0) or 0)
                if wallet_bal > 0:
                    state_manager.set("initial_balance", wallet_bal)
                    state_manager.set("daily_start_balance", wallet_bal)
                    cleared.append(f"initial_balance → {wallet_bal:.2f} USDT")
        except Exception as e:
            errors.append(f"initial_balance: {e}")

        # 6. بناء رسالة النتيجة
        cleared_txt = "\n".join(f"  ✅ {x}" for x in cleared)
        errors_txt  = "\n".join(f"  ⚠️ {x}" for x in errors) if errors else "  لا يوجد"

        logger.info(f"[Reset] تم مسح بيانات البوت. محذوف: {cleared}")
        self.edit_message(
            chat_id, message_id,
            f"🗑️ <b>تم مسح بيانات البوت بنجاح</b>\n\n"
            f"<b>ما تم مسحه:</b>\n{cleared_txt}\n\n"
            f"<b>أخطاء:</b>\n{errors_txt}\n\n"
            f"✅ البوت الآن في حالة نظيفة جاهز للاختبار.",
            self.main_menu_keyboard(),
        )

    def protections_menu_keyboard(self, state_manager):
        """لوحة أزرار إعدادات أنظمة الحماية"""
        sl_on = state_manager.get_sl_guard_enabled()
        dd_on = state_manager.get_max_drawdown_enabled()
        lp_on = state_manager.get_low_profit_enabled()
        sl_ex = True  # حماية Binance الأساسية إلزامية ولا تُعطل من الواجهة

        sl_guard_pause = state_manager.get_sl_guard_pause()
        from datetime import datetime as _dt
        sl_paused = bool(sl_guard_pause and _dt.now() < sl_guard_pause)
        dd_pause = state_manager.get_max_drawdown_pause()
        dd_paused = bool(dd_pause and _dt.now() < dd_pause)

        rows = [
            [
                self._make_btn(f"🛡️ حارس وقف الخسارة: {'✅' if sl_on else '❌'}", "prot_slguard_toggle", "success" if sl_on else "danger", icon_key="shield_protection"),
            ],
            [
                self._make_btn("حد الخسائر: [1]", "prot_slguard_limit_1", "primary"),
                self._make_btn("[2]",              "prot_slguard_limit_2", "primary"),
                self._make_btn("[3]",              "prot_slguard_limit_3", "primary"),
                self._make_btn("[4]",              "prot_slguard_limit_4", "primary"),
                self._make_btn("[5]",              "prot_slguard_limit_5", "primary"),
            ],
            [
                self._make_btn(f"📉 حارس الخسارة المحققة: {'✅' if dd_on else '❌'}", "prot_dd_toggle", "success" if dd_on else "danger", icon_key="percentage_chart"),
            ],
            [
                self._make_btn("3%",  "prot_dd_pct_3",  "primary"),
                self._make_btn("5%",  "prot_dd_pct_5",  "primary"),
                self._make_btn("7%",  "prot_dd_pct_7",  "primary"),
                self._make_btn("10%", "prot_dd_pct_10", "primary"),
                self._make_btn("15%", "prot_dd_pct_15", "primary"),
            ],
            [
                self._make_btn(f"❄️ تجميد أزواج الخسارة: {'✅' if lp_on else '❌'}", "prot_lp_toggle", "success" if lp_on else "danger"),
            ],
            [
                self._make_btn("خسائر: [1]", "prot_lp_count_1", "primary"),
                self._make_btn("[2]",        "prot_lp_count_2", "primary"),
                self._make_btn("[3]",        "prot_lp_count_3", "primary"),
            ],
            [self._make_btn("🔓 رفع التجميد عن الكل", "prot_lp_unfreeze_all", "primary")],
            [
                self._make_btn("🔒 وقف خسارة باينانس: إلزامي ✅", "prot_sl_exchange_toggle", "success"),
            ],
            [self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home")],
        ]

        # إضافة زر إلغاء الإيقاف فقط إذا كان البوت موقوفاً فعلاً
        if sl_paused:
            rows.insert(2, [self._make_btn("🔓 إلغاء إيقاف حارس وقف الخسارة", "prot_slguard_unpause", "success")])
        if dd_paused:
            rows.insert(-3, [self._make_btn("🔓 إلغاء إيقاف حارس الخسارة", "prot_dd_unpause", "success")])

        return {"inline_keyboard": rows}

    # ==========================================================
    # Commands Registration
    # ==========================================================
    def set_bot_commands(self):
        """
        تسجيل الأوامر لدى خوادم تيليجرام لتظهر تلقائياً للمستخدم في القائمة.
        """
        if not self._is_configured():
            return
        if self._poll_thread and self._poll_thread.is_alive():
            logger.warning("[Telegram] polling يعمل مسبقاً؛ تم تجاهل start مكرر.")
            return self._poll_thread
        self._poll_stop.clear()

        commands = [
            {"command": "start", "description": "فتح لوحة التحكم الرئيسية"},
            {"command": "menu", "description": "عرض القائمة الرئيسية"},
            {"command": "analyze", "description": "تحليل ذكي لعملة معينة"},
            {"command": "status", "description": "عرض حالة الرصيد والصفقات المفتوحة"},
            {"command": "health", "description": "عرض صحة الخادم والشبكة"},
            {"command": "setmax", "description": "تغيير الحد الأقصى للصفقات المتزامنة"},
            {"command": "restart", "description": "إعادة تشغيل البوت بالكامل"},
            {"command": "stop", "description": "إيقاف طوارئ"},
            {"command": "help", "description": "شرح استخدام البوت"},
            {"command": "set_risk",     "description": "تعديل مخاطرة الوقف — مثال: /set_risk 0.25"},
            {"command": "set_leverage", "description": "تعديل الرافعة المالية — مثال: /set_leverage 10"},
            {"command": "blacklist",    "description": "تجميد عملة — مثال: /blacklist BTCUSDT"},
            {"command": "whitelist",    "description": "رفع التجميد — مثال: /whitelist BTCUSDT"},
            {"command": "shadow_report", "description": "تقرير تحليل أداء البوت الأسبوعي"},
            {"command": "journal",        "description": "آخر 5 صفقات مع تقييم جودة الدخول"},
        ]

        payload = {
            "commands": commands,
            "scope": {"type": "default"},
            "language_code": "ar",
        }

        try:
            requests.post(f"{self.base_url}/setMyCommands", json=payload, timeout=5)
            logger.info("تم تسجيل أوامر القائمة في تيليجرام بنجاح.")
        except Exception as e:
            logger.debug(f"فشل تسجيل أوامر تيليجرام: {e}")

        # ضبط زر القائمة السفلي ليفتح Mini App مباشرة (مثل زر Play)
        self._set_webapp_menu_button()

    def _set_webapp_menu_button(self):
        """
        يستبدل زر 'Menu' السفلي بزر يفتح Mini App مباشرة.
        النتيجة: ظهور زر 'لوحة التحكم' أسفل المحادثة مثل زر Play في Hamster Kombat.
        """
        if not self._is_configured():
            return
        payload = {
            "menu_button": {
                "type": "web_app",
                "text": "Open",
                "web_app": {"url": os.getenv("WEBAPP_URL", "https://tradingbot-production-0b72.up.railway.app/")}
            }
        }
        try:
            requests.post(f"{self.base_url}/setChatMenuButton", json=payload, timeout=5)
            logger.info("[WebApp] تم ضبط زر Mini App كزر القائمة الرئيسي بنجاح.")
        except Exception as e:
            logger.debug(f"[WebApp] فشل ضبط زر Mini App: {e}")


    def send_api_status_report(self, client, state_manager):
        try:
            monitor = APIStatusMonitor(client=client, state_manager=state_manager, notifier=self)
            payload = monitor.run_full_check()
            text = monitor.format_report(payload)
            monitor.notify_if_needed(payload)
            self.send_message(text, reply_markup=self.back_home_keyboard())
        except Exception as e:
            logger.error(f"فشل إرسال تقرير API Status: {e}")
            self.send_message(f"⚠️ فشل فحص الخدمات:\n<code>{e}</code>")

    # ==========================================================
    # Sending / Editing Messages
    # ==========================================================
    def _persistent_shortcuts(self) -> dict:
        """زران ثابتان يظهران أسفل كل رسالة للوصول السريع"""
        return {
            "inline_keyboard": [
                [
                    self._make_btn("🏠 القائمة الرئيسية", "gui_home", "primary", icon_key="home"),
                    self._make_btn("📊 حالة البوت",        "gui_status", "primary", icon_key="bar_chart"),
                ]
            ]
        }

    def send_message(self, text: str, reply_markup: dict = None, disable_web_page_preview: bool = True):
        # إذا لم يُحدَّد كيبورد خاص، نضيف زري الاختصار الدائمين في أسفل كل رسالة
        if reply_markup is None:
            reply_markup = self._persistent_shortcuts()
        """
        إرسال رسالة تيليجرام مع fallback:
        - المحاولة الأولى HTML.
        - إذا فشل Bad Request، يعيد الإرسال كنص عادي بدون HTML.
        - يمنع سقوط الرد بسبب كسر وسوم HTML أو تقسيم الرسالة.
        """
        if not getattr(Config, "TELEGRAM_OUTBOUND_ENABLED", True):
            if not getattr(self, "_outbound_disabled_logged", False):
                logger.info("[Telegram] outbound disabled for this runtime; message suppressed.")
                self._outbound_disabled_logged = True
            return False

        if not self._is_configured():
            logger.debug("إعدادات تيليجرام غير مكتملة، تم تخطي إرسال الإشعار.")
            return False

        MAX_LEN = 3900  # أقل من 4096 لتجنب مشاكل الحدود
        text = str(text or "")
        chunks = self._split_html_message(text, limit=MAX_LEN)
        success = True

        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": self.chat_id,
                "text": self._sanitize_html(chunk),
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_web_page_preview,
            }

            # نضيف الأزرار فقط لآخر جزء حتى لا تتكرر مع الرسائل الطويلة.
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup

            try:
                response = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)

                if response.status_code == 400:
                    try:
                        description = str((response.json() or {}).get("description") or "")
                    except Exception:
                        description = ""
                    description_hash = hashlib.sha256(
                        description.encode("utf-8", errors="replace")
                    ).hexdigest()[:12]
                    logger.warning(
                        "Telegram Bad Request HTML; retrying plain text. "
                        f"status=400 description_hash={description_hash}"
                    )
                    plain_payload = {
                        "chat_id": self.chat_id,
                        "text": self._strip_html(chunk),
                        "disable_web_page_preview": disable_web_page_preview,
                    }
                    if reply_markup and index == len(chunks) - 1:
                        plain_payload["reply_markup"] = reply_markup

                    plain_response = requests.post(
                        f"{self.base_url}/sendMessage", json=plain_payload, timeout=10
                    )
                    plain_response.raise_for_status()
                else:
                    response.raise_for_status()

            except Exception as e:
                logger.error(f"فشل إرسال إشعار تيليجرام: {e}")
                success = False

        if success:
            logger.info("تم إرسال إشعار تيليجرام بنجاح.")
        return success

    def send_document(self, file_path: str, caption: str = ""):
        """
        إرسال ملف/مستند عبر تيليجرام.
        """
        if not self._is_configured():
            logger.debug("إعدادات تيليجرام غير مكتملة، تم تخطي إرسال المستند.")
            return False

        if not os.path.exists(file_path):
            logger.error(f"المستند المطلوب إرساله غير موجود: {file_path}")
            return False

        url = f"{self.base_url}/sendDocument"
        from utils.runtime_health import log_memory_milestone
        log_memory_milestone(
            "TELEGRAM_FILE_SEND_START",
            force=True,
            details={"file": os.path.basename(file_path), "bytes": os.path.getsize(file_path)},
        )
        try:
            with open(file_path, 'rb') as f:
                files = {'document': f}
                payload = {
                    'chat_id': self.chat_id,
                    'caption': caption,
                    'parse_mode': 'HTML'
                }
                timeout = max(30, int(os.getenv("TELEGRAM_DOCUMENT_TIMEOUT_SECONDS", "120")))
                response = requests.post(url, data=payload, files=files, timeout=timeout)
                response.raise_for_status()
                logger.info(f"تم إرسال المستند {file_path} بنجاح.")
                return True
        except Exception as e:
            logger.error(f"فشل إرسال المستند {file_path} عبر تيليجرام: {e}")
            return False
        finally:
            log_memory_milestone(
                "TELEGRAM_FILE_SEND_END",
                force=True,
                details={"file": os.path.basename(file_path)},
            )

    def send_runtime_logs(self, hours=24, *, minutes=None, state_manager=None, client=None):
        """تجهيز وإرسال سجل التشغيل للنطاق المطلوب مع تنظيف الملفات المؤقتة."""
        from utils.runtime_log_exporter import cleanup_export, export_runtime_logs_zip

        if minutes is None:
            hours = int(hours)
            if hours not in (1, 24, 48, 72):
                logger.error(f"[RuntimeExport] مدة تصدير غير مسموحة: {hours}h")
                return False
            window_label = f"{hours} ساعة"
            export_kwargs = {"hours": hours}
            thread_label = f"{hours}h"
        else:
            minutes = int(minutes)
            if minutes < 1 or minutes > 168 * 60:
                logger.error(f"[RuntimeExport] مدة تصدير غير مسموحة: {minutes}m")
                return False
            window_label = f"{minutes} دقيقة"
            export_kwargs = {"hours": 1, "minutes": minutes}
            thread_label = f"{minutes}m"

        result = None
        acquired_immediately = _runtime_export_lock.acquire(blocking=False)
        if not acquired_immediately:
            self.send_message(
                f"🕒 طلب سجل {window_label} في قائمة الانتظار؛ "
                "يوجد تصدير آخر قيد التجهيز أو الإرسال."
            )
            _runtime_export_lock.acquire()
        try:
            state_manager = state_manager or getattr(
                self, "_runtime_state_manager", None
            )
            client = client or getattr(self, "_runtime_client", None)
            if state_manager is not None or client is not None:
                export_kwargs.update({
                    "state_manager": state_manager,
                    "client": client,
                })
            result = export_runtime_logs_zip(**export_kwargs)
            if result.no_data:
                self.send_message(f"ℹ️ لا توجد سجلات متاحة خلال آخر {window_label}.")
                return False

            sent_all = True
            total = len(result.paths)
            for index, path in enumerate(result.paths, start=1):
                caption = f"📥 سجل تشغيل البوت — آخر {window_label}"
                if total > 1:
                    caption += f" ({index}/{total})"
                if not self.send_document(path, caption=caption):
                    sent_all = False

            if sent_all:
                summary = self._safe_html(result.summary)
                self.send_message(f"✅ <b>تم إرسال سجل التشغيل.</b>\n\n<pre>{summary}</pre>")
            else:
                self.send_message("⚠️ تم تجهيز السجل، لكن تعذر إرسال ملف أو أكثر عبر Telegram.")
            return sent_all
        except Exception as exc:
            logger.error(
                f"[RuntimeExport] فشل تجهيز/إرسال سجل {thread_label}: {exc}"
            )
            self.send_message("⚠️ تعذر تجهيز سجل التشغيل حالياً. راجع سجل الأخطاء.")
            return False
        finally:
            if result is not None:
                cleanup_export(result)
            _runtime_export_lock.release()

    def send_runtime_logs_last_24h(self):
        """واجهة توافقية للزر والاستخدامات القديمة."""
        return self.send_runtime_logs(24)

    def send_72h_evidence_bundle(self, state_manager):
        """Build and send the complete test artifact as bounded Telegram parts."""
        from utils.forensics_bundle import cleanup_evidence_bundle, export_72h_test_bundle

        result = None
        acquired_immediately = _runtime_export_lock.acquire(blocking=False)
        if not acquired_immediately:
            self.send_message("🕒 طلب حزمة 72 ساعة في الانتظار؛ يوجد تصدير آخر قيد التنفيذ.")
            _runtime_export_lock.acquire()
        try:
            result = export_72h_test_bundle(state_manager=state_manager)
            if not result.paths:
                self.send_message("ℹ️ لا توجد أدلة متاحة للتصدير.")
                return False
            sent_all = True
            for index, path in enumerate(result.paths, start=1):
                caption = f"📦 72H Test Bundle ({index}/{len(result.paths)})"
                if not self.send_document(path, caption=caption):
                    sent_all = False
            safe_summary = self._safe_html(result.summary)
            if sent_all:
                from utils.test_session import complete_test_session

                complete_test_session(
                    (result.manifest or {}).get("test_id"),
                    source="TELEGRAM_EVIDENCE_DELIVERED",
                )
                self.send_message(f"✅ <b>تم إرسال حزمة الأدلة.</b>\n\n<pre>{safe_summary}</pre>")
            else:
                self.send_message("⚠️ تم بناء الحزمة، لكن فشل إرسال جزء أو أكثر.")
            return sent_all
        except Exception as exc:
            logger.exception("[EvidenceBundle] فشل بناء/إرسال الحزمة: %s", exc)
            self.send_message("⚠️ تعذر بناء حزمة اختبار 72 ساعة. راجع سجل الأخطاء.")
            return False
        finally:
            if result is not None:
                cleanup_evidence_bundle(result)
            _runtime_export_lock.release()

    @staticmethod
    def _authorized_actor_user_id(user_id):
        """Fail closed: every Telegram action requires an explicit admin ID."""
        expected = os.getenv("TELEGRAM_WEBAPP_USER_ID", "").strip()
        if not expected:
            return False
        try:
            return int(expected) > 0 and int(user_id or 0) == int(expected)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _record_config_change(state_manager, setting, old, new):
        if old == new:
            return
        try:
            from utils.test_session import record_config_change

            record_config_change(
                setting, old, new, source="TELEGRAM", state_manager=state_manager
            )
        except Exception as exc:
            logger.error("[ConfigAudit] تعذر تسجيل تغيير %s: %s", setting, exc)

    @staticmethod
    def _storage_admin_user_id(callback):
        expected = os.getenv("TELEGRAM_WEBAPP_USER_ID", "").strip()
        callback_user_id = str((callback.get("from") or {}).get("id", "")).strip()
        try:
            if int(expected) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return callback_user_id if callback_user_id == expected else None

    @staticmethod
    def _format_storage_usage(usage, breakdown=None):
        message = (
            f"💾 <b>مساحة التخزين الدائم</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"المسار: <code>{escape(str(usage['path']))}</code>\n"
            f"الإجمالي: <code>{usage['total_mb']:,.2f} MB</code>\n"
            f"المستخدم: <code>{usage['used_mb']:,.2f} MB</code> "
            f"(<code>{usage['used_percent']:.2f}%</code>)\n"
            f"المتبقي: <code>{usage['free_mb']:,.2f} MB</code> "
            f"(<code>{usage['free_percent']:.2f}%</code>)"
        )
        if breakdown:
            labels = (
                ("active_logs", "السجلات النشطة (محمية)"),
                ("archived_logs", "السجلات المؤرشفة"),
                ("temporary_exports", "ملفات التصدير المؤقتة"),
                ("database_and_state", "قاعدة البيانات وملف الحالة (محمية)"),
                ("trade_journal", "سجل الصفقات والشموع"),
                ("other", "ملفات أخرى"),
            )
            message += "\n\n<b>تفصيل ملفات البوت:</b>"
            for key, label in labels:
                values = breakdown.get(key) or {}
                message += (
                    f"\n• {label}: <code>{float(values.get('mb', 0) or 0):,.2f} MB</code> "
                    f"(<code>{int(values.get('files', 0) or 0)}</code> ملف)"
                )
            totals = breakdown.get("_totals") or {}
            message += (
                "\n\n<b>مطابقة مساحة القرص:</b>"
                f"\n• مجموع أحجام الملفات الظاهرة: "
                f"<code>{float(totals.get('apparent_mb', 0) or 0):,.2f} MB</code>"
                f"\n• ملفات محذوفة لكنها ما زالت مفتوحة: "
                f"<code>{float(totals.get('deleted_open_mb', 0) or 0):,.2f} MB</code>"
                f"\n• مساحة لا تفسرها الملفات الظاهرة: "
                f"<code>{float(totals.get('unaccounted_mb', 0) or 0):,.2f} MB</code>"
                "\n<i>قد تشمل metadata/حجز نظام الملفات أو قياس المنصة المختلف؛ "
                "هذه مساحة Disk وليست RAM.</i>"
            )
        return message

    def send_storage_status(self):
        from utils.storage_manager import get_storage_breakdown, get_storage_usage

        try:
            usage = get_storage_usage()
            breakdown = get_storage_breakdown()
            return self.send_message(
                self._format_storage_usage(usage, breakdown),
                reply_markup=self.reports_menu_keyboard(),
            )
        except Exception as exc:
            logger.error(f"[Storage] تعذر قراءة مساحة التخزين: {exc}")
            return self.send_message(
                "⚠️ تعذر قراءة مساحة التخزين الدائم. تحقق من DATA_DIR وRailway Volume."
            )

    def run_safe_storage_cleanup(self):
        from utils.storage_manager import cleanup_data_volume

        try:
            result = cleanup_data_volume()
            usage = result["after"]
            if result["deleted_count"]:
                headline = (
                    f"✅ تم حذف <code>{result['deleted_count']}</code> ملف مؤرشف/مؤقت، "
                    f"وتحرير <code>{result['freed_mb']:.2f} MB</code>."
                )
            else:
                headline = (
                    "ℹ️ تم تنفيذ الفحص بنجاح، لكن لم يُحذف أي ملف: "
                    "لا توجد أرشيفات تجاوزت مدة الحماية المحددة."
                )
            error_line = (
                f"\nتعذر حذف: <code>{len(result['errors'])}</code> ملف."
                if result["errors"] else ""
            )
            breakdown = result.get("breakdown_after") or {}
            active_mb = float((breakdown.get("active_logs") or {}).get("mb", 0) or 0)
            archive_mb = float((breakdown.get("archived_logs") or {}).get("mb", 0) or 0)
            return self.send_message(
                f"🧹 <b>تقرير التنظيف الآمن</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{headline}{error_line}\n\n"
                f"السجلات النشطة المحمية: <code>{active_mb:,.2f} MB</code>\n"
                f"الأرشيفات المحمية/المتبقية: <code>{archive_mb:,.2f} MB</code>\n"
                f"المساحة المتبقية: <code>{usage['free_mb']:,.2f} MB</code> "
                f"(<code>{usage['free_percent']:.2f}%</code>)\n\n"
                "تم الحفاظ على السجل النشط وقاعدة البيانات وملف الحالة "
                "وكافة الأدلة داخل نافذة اختبار 72 ساعة.",
                reply_markup=self.reports_menu_keyboard(),
            )
        except Exception as exc:
            logger.error(f"[StorageCleanup] فشل التنظيف الآمن: {exc}")
            return self.send_message(
                "⚠️ فشل تنظيف السجلات. لم تُنفذ أي عملية حذف خارج القائمة الآمنة."
            )

    def run_fresh_test_storage_cleanup(self, state_manager):
        """Explicitly reset log evidence while preserving all trading state."""
        from utils.decision_audit import audit_decision_event
        from utils.storage_manager import prepare_storage_for_new_test

        active = sorted(state_manager.get_active_symbols() or [])
        if active:
            return self.send_message(
                "⛔ <b>رُفضت تهيئة السجلات</b>\n\n"
                "توجد صفقات متتبعة مفتوحة: "
                + ", ".join(f"<code>{escape(str(symbol))}</code>" for symbol in active)
                + "\nأغلقها أو انتظر إغلاقها ثم أعد المحاولة.",
                reply_markup=self.reports_menu_keyboard(),
            )
        if not _runtime_export_lock.acquire(blocking=False):
            return self.send_message(
                "⏳ يوجد تصدير سجلات قيد التنفيذ. أعد المحاولة بعد اكتمال إرساله.",
                reply_markup=self.reports_menu_keyboard(),
            )
        try:
            result = prepare_storage_for_new_test()
            if not result.get("ok") and result.get("reason") == "ACTIVE_72H_TEST_SESSION":
                return self.send_message(
                    "⛔ <b>رُفضت تهيئة السجلات</b>\n\n"
                    "توجد جلسة اختبار 72 ساعة نشطة أو متوقفة تحتاج التصدير أولاً.\n"
                    f"Test ID: <code>{escape(str(result.get('test_id') or 'unknown'))}</code>",
                    reply_markup=self.reports_menu_keyboard(),
                )
            audit_decision_event(
                stage="STORAGE_RESET",
                status="COMPLETED" if result.get("ok") else "COMPLETED_WITH_ERRORS",
                details={
                    "deleted_count": result.get("deleted_count", 0),
                    "truncated_count": result.get("truncated_count", 0),
                    "apparent_freed_mb": round(float(result.get("apparent_freed_mb", 0)), 3),
                    "disk_delta_mb": round(float(result.get("disk_delta_mb", 0)), 3),
                    "error_count": len(result.get("errors") or []),
                },
                durable=True,
            )
            after = result.get("after") or {}
            return self.send_message(
                "🧽 <b>تمت تهيئة سجلات اختبار جديد</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"الأرشيفات/ملفات التصدير المحذوفة: <code>{result.get('deleted_count', 0)}</code>\n"
                f"السجلات النشطة التي صُفّرت بأمان: <code>{result.get('truncated_count', 0)}</code>\n"
                f"الحجم الظاهري المحرر: <code>{float(result.get('apparent_freed_mb', 0)):,.2f} MB</code>\n"
                f"انخفاض استخدام القرص المقاس: <code>{float(result.get('disk_delta_mb', 0)):,.2f} MB</code>\n"
                f"الأخطاء: <code>{len(result.get('errors') or [])}</code>\n"
                f"المساحة المتبقية: <code>{float(after.get('free_mb', 0)):,.2f} MB</code>\n\n"
                "✅ لم تُحذف قاعدة البيانات أو ملف الحالة أو Journal.\n"
                "يمكنك الآن الضغط على «بدء اختبار 72 ساعة».",
                reply_markup=self.reports_menu_keyboard(),
            )
        except Exception as exc:
            logger.exception("[StorageReset] فشلت تهيئة سجلات الاختبار: %s", exc)
            return self.send_message(
                "⚠️ تعذرت تهيئة السجلات. لم يُسمح بحذف أي ملف خارج القائمة الآمنة.",
                reply_markup=self.reports_menu_keyboard(),
            )
        finally:
            _runtime_export_lock.release()

    def send_today_candle_logs(self):
        """
        تتجميع وإرسال ملف zip يحتوي على سجلات الشموع والصفقات لليوم الحالي.
        """
        import zipfile
        import tempfile
        from datetime import datetime

        journal_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
        if not os.path.exists(journal_dir):
            self.send_message("❌ لا يوجد مجلد لسجلات الصفقات (trade_journal) بعد. يبدو أنه لم يتم فتح أي صفقات حتى الآن.")
            return False

        today_str = datetime.now().strftime("%Y%m%d")

        # البحث عن المجلدات التابعة لليوم
        today_folders = []
        for d in os.listdir(journal_dir):
            full_path = os.path.join(journal_dir, d)
            if os.path.isdir(full_path) and d.startswith(today_str):
                today_folders.append(full_path)

        if not today_folders:
            self.send_message("ℹ️ لا توجد صفقات منفذة ومسجلة لليوم حتى الآن لجمع شمعاتها.")
            return False

        # إشعار البدء بالتحضير
        self.send_message("⏳ جاري ضغط شمعات وسجلات صفقات اليوم لإرسالها...")

        # إنشاء ملف مضغوط مؤقت
        zip_filename = f"candles_report_{today_str}.zip"
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, zip_filename)

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for folder in today_folders:
                    for root, dirs, files in os.walk(folder):
                        for file in files:
                            file_path = os.path.join(root, file)
                            # الحفاظ على الهيكل التنظيمي للمجلدات داخل الملف المضغوط
                            arcname = os.path.relpath(file_path, start=journal_dir)
                            zipf.write(file_path, arcname)

            # إرسال الملف المضغوط
            caption = f"📦 <b>سجل شمعات صفقات اليوم ({datetime.now().strftime('%Y-%m-%d')})</b>\nيحتوي الملف على شمعات (1m, 5m, 15m) ومبررات الدخول والخروج لكل صفقة."
            success = self.send_document(zip_path, caption=caption)
            if not success:
                self.send_message("❌ فشل إرسال الملف المضغوط. يرجى التحقق من سجلات البوت.")
                return False
            return True
        except Exception as e:
            logger.error(f"خطأ أثناء ضغط وإرسال شمعات اليوم: {e}")
            self.send_message(f"❌ حدث خطأ أثناء إعداد الملف المضغوط: {e}")
            return False
        finally:
            # تنظيف الملف المؤقت
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except Exception:
                    pass

    def candles_menu_keyboard(self):
        """
        قائمة خيارات تحميل سجلات الشموع.
        """
        return {
            "inline_keyboard": [
                [
                    self._make_btn("📥 آخر 24 ساعة (الكل)", "candles_dl_24h", "primary", icon_key="report_document"),
                    self._make_btn("📥 آخر ساعة (الكل)", "candles_dl_1h", "primary", icon_key="report_document"),
                ],
                [
                    self._make_btn("🔍 تصفية بعملة واحدة", "candles_filter_single", "primary", icon_key="filter_settings"),
                    self._make_btn("🗂️ اختيار متعدد للعملات", "candles_filter_multi", "primary", icon_key="filter_settings"),
                ],
                [
                    self._make_btn("🔙 الرجوع لقائمة التقارير", "gui_reports_menu", "primary", icon_key="home"),
                ],
            ]
        }

    def _get_symbols_last_24h(self) -> list:
        """جلب رموز العملات الفريدة التي تم تداولها خلال آخر 24 ساعة من المجلدات المتاحة"""
        import time
        journal_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
        if not os.path.exists(journal_dir):
            return []
        
        symbols = set()
        now = time.time()
        for d in os.listdir(journal_dir):
            full_path = os.path.join(journal_dir, d)
            if os.path.isdir(full_path):
                try:
                    mtime = os.path.getmtime(full_path)
                    age_hours = (now - mtime) / 3600
                    if age_hours <= 24:
                        parts = d.split('_')
                        if len(parts) >= 2:
                            symbol_part = parts[1].upper()
                            symbols.add(symbol_part)
                except Exception:
                    pass
        return sorted(list(symbols))

    def build_candles_single_filter_keyboard(self):
        """
        لوحة اختيار عملة واحدة من عملات آخر 24 ساعة.
        """
        symbols = self._get_symbols_last_24h()
        if not symbols:
            return {
                "inline_keyboard": [
                    [self._make_btn("ℹ️ لا توجد صفقات في آخر 24 ساعة", "none", "primary")],
                    [self._make_btn("🔙 الرجوع", "gui_candles_menu", "primary", icon_key="home")],
                ]
            }
        
        keyboard = []
        row = []
        for sym in symbols:
            row.append(self._make_btn(sym, f"c_dl_single_{sym}", "primary"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
            
        keyboard.append([self._make_btn("🔙 الرجوع لقائمة الشموع", "gui_candles_menu", "primary", icon_key="home")])
        return {"inline_keyboard": keyboard}

    def build_candles_multi_filter_keyboard(self, chat_id):
        """
        لوحة اختيار متعدد لعملات آخر 24 ساعة.
        """
        symbols = self._get_symbols_last_24h()
        if not symbols:
            return {
                "inline_keyboard": [
                    [self._make_btn("ℹ️ لا توجد صفقات في آخر 24 ساعة", "none", "primary")],
                    [self._make_btn("🔙 الرجوع", "gui_candles_menu", "primary", icon_key="home")],
                ]
            }
        
        selected = self.selected_candles_state.get(chat_id, set())
        
        keyboard = []
        row = []
        for sym in symbols:
            prefix = "✅ " if sym in selected else "⬜ "
            row.append(self._make_btn(
                f"{prefix}{sym}",
                f"c_tgl_{sym}",
                "success" if sym in selected else "primary"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
            
        keyboard.append([
            self._make_btn("📥 تحميل العملات المحددة", "c_dl_multi_confirm", "success", icon_key="report_document"),
            self._make_btn("🧹 تصفية الاختيار", "c_multi_clear", "danger", icon_key="trash_delete")
        ])
        keyboard.append([self._make_btn("🔙 الرجوع لقائمة الشموع", "gui_candles_menu", "primary", icon_key="home")])
        return {"inline_keyboard": keyboard}

    def send_journal_candles_by_time(self, hours: int = 24, symbols: list = None):
        """
        تجميع وإرسال ملف zip يحتوي على سجلات الشموع والصفقات التي تقع في النطاق الزمني المحدد.
        """
        import zipfile
        import tempfile
        import time
        from datetime import datetime

        journal_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
        if not os.path.exists(journal_dir):
            self.send_message("❌ لا يوجد مجلد لسجلات الصفقات (trade_journal) بعد. يبدو أنه لم يتم فتح أي صفقات حتى الآن.")
            return False

        target_folders = []
        now = time.time()
        for d in os.listdir(journal_dir):
            full_path = os.path.join(journal_dir, d)
            if os.path.isdir(full_path):
                try:
                    mtime = os.path.getmtime(full_path)
                    age_hours = (now - mtime) / 3600
                    if age_hours <= hours:
                        if symbols:
                            parts = d.split('_')
                            if len(parts) >= 2:
                                symbol_part = parts[1].upper()
                                clean_symbols = [s.replace("/", "").upper() for s in symbols]
                                if symbol_part not in clean_symbols:
                                    continue
                        target_folders.append(full_path)
                except Exception as e:
                    logger.warning(f"تعذر قراءة ميعاد تعديل المجلد {d}: {e}")

        if not target_folders:
            self.send_message(f"ℹ️ لا توجد صفقات منفذة ومسجلة خلال آخر {hours} ساعة تطابق فلتر العملات.")
            return False

        self.send_message(f"⏳ جاري ضغط شمعات وسجلات آخر {hours} ساعة لإرسالها...")

        zip_filename = f"candles_report_{hours}h.zip"
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, zip_filename)

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for folder in target_folders:
                    for root, dirs, files in os.walk(folder):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, start=journal_dir)
                            zipf.write(file_path, arcname)

            caption = f"📦 <b>سجل شمعات آخر {hours} ساعة</b>\nيحتوي الملف على مبررات الدخول/الخروج والشموع للعملات المحددة."
            success = self.send_document(zip_path, caption=caption)
            if not success:
                self.send_message("❌ فشل إرسال الملف المضغوط. يرجى التحقق من سجلات البوت.")
                return False
            return True
        except Exception as e:
            logger.error(f"خطأ أثناء ضغط وإرسال شمعات آخر {hours} ساعة: {e}")
            self.send_message(f"❌ حدث خطأ أثناء إعداد الملف المضغوط: {e}")
            return False
        finally:
            if os.path.exists(zip_path):
                try:
                    os.remove(zip_path)
                except Exception:
                    pass

    # ─────────────────────────────────────────────
    # Journal: تحليل روائي للصفقات
    # ─────────────────────────────────────────────

    def _send_journal_report(self, chat_id: int, mode: str = "last5",
                             client=None, symbol_filter: str = None):
        """
        يقرأ سجلات trade_journal ويُرسل تحليلاً روائياً لكل صفقة.
        mode: 'last5' | '24h' | 'symbol'
        """
        import glob, json, time as _time
        from utils.trade_journal import TradeJournal

        try:
            journal_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
            if not os.path.exists(journal_dir):
                self.send_message("📭 لا يوجد مجلد سجلات بعد.", chat_id=chat_id)
                return

            # ── جمع ملفات التلخيص ───────────────────────────
            all_summaries = sorted(
                glob.glob(os.path.join(journal_dir, "*/trade_summary.json")),
                key=os.path.getmtime,
                reverse=True
            )

            now = _time.time()

            if mode == "last5":
                targets = all_summaries[:5]
                header  = "📊 <b>آخر 5 صفقات — مراجعة تفصيلية</b>"
            elif mode == "24h":
                targets = [p for p in all_summaries
                           if (now - os.path.getmtime(p)) <= 86400]
                header  = "🕐 <b>صفقات آخر 24 ساعة — مراجعة تفصيلية</b>"
            elif mode == "symbol" and symbol_filter:
                sym_clean = symbol_filter.replace("/", "").upper()
                targets   = [p for p in all_summaries
                             if sym_clean in os.path.basename(os.path.dirname(p)).upper()]
                header    = f"🔍 <b>مراجعة صفقات {symbol_filter.upper()}</b>"
            else:
                targets = all_summaries[:5]
                header  = "📊 <b>آخر 5 صفقات</b>"

            if not targets:
                self.send_message(
                    "📭 لا توجد صفقات مسجلة تطابق هذا الفلتر.",
                    chat_id=chat_id
                )
                return

            self.send_message(
                f"{header}\n<i>جاري قراءة بيانات {len(targets)} صفقة...</i>",
                chat_id=chat_id
            )

            tj = TradeJournal()

            for path in targets:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        data = json.load(f)

                    narrative = self._build_trade_narrative(data, tj, client)
                    self.send_message(narrative, chat_id=chat_id)

                except Exception as e:
                    logger.warning(f"[Journal] فشل تحليل {path}: {e}")

            # ── رسالة ختامية ────────────────────────────────
            wins    = 0
            losses  = 0
            for path in targets:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        d = json.load(f)
                    ex = d.get("exit_details", {}) or {}
                    pnl = float(ex.get("pnl_pct", 0) or 0)
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                except Exception:
                    pass

            total = wins + losses
            wr    = (wins / total * 100) if total > 0 else 0
            emoji = "🟢" if wr >= 50 else "🔴"

            self.send_message(
                f"{emoji} <b>خلاصة المراجعة:</b>\n"
                f"الصفقات المحللة: {total}\n"
                f"رابحة: {wins} | خاسرة: {losses}\n"
                f"نسبة النجاح: <b>{wr:.0f}%</b>\n\n"
                f"اكتب /journal لمراجعة إضافية، أو ارجع للقائمة.",
                chat_id=chat_id
            )

        except Exception as e:
            logger.error(f"[Journal] خطأ عام في _send_journal_report: {e}")
            self.send_message(f"❌ خطأ في تحليل السجل: {e}", chat_id=chat_id)

    def _build_trade_narrative(self, data: dict, tj, client=None) -> str:
        """
        يبني النص الروائي لصفقة واحدة بلغة طبيعية وإنسانية.
        يقرأ الشمعات ويحكي قصة السعر من الدخول حتى الخروج.
        """
        import json

        entry_meta  = data.get("entry_meta",    {}) or {}
        exit_det    = data.get("exit_details",  {}) or {}
        candle_snap = data.get("candle_snapshots", {}) or {}
        quick_read  = data.get("quick_read",    {}) or {}

        symbol      = data.get("symbol", entry_meta.get("symbol", "؟"))
        side        = str(entry_meta.get("side", "buy")).upper()
        entry_price = float(entry_meta.get("entry_price", 0) or 0)
        sl          = float(entry_meta.get("sl", 0) or 0)
        tp          = float(entry_meta.get("tp", 0) or 0)
        leverage    = float(entry_meta.get("leverage", 10) or 10)
        entry_time  = entry_meta.get("entry_time", "")
        entry_reason= entry_meta.get("entry_reason", "")

        exit_price  = float(exit_det.get("exit_price",  quick_read.get("exit_price", 0) or 0) or 0)
        exit_reason = str(exit_det.get("exit_reason",   quick_read.get("exit_reason", "؟")) or "؟")
        pnl_pct     = float(exit_det.get("pnl_pct",     quick_read.get("pnl_pct", 0) or 0) or 0)
        max_fav     = float(exit_det.get("max_favorable_pct", 0) or 0)
        max_adv     = float(exit_det.get("max_adverse_pct",   0) or 0)

        # ── اتجاه ونتيجة ──────────────────────────────────
        direction   = "شراء (Long) 📈" if side == "BUY" else "بيع (Short) 📉"
        is_win      = pnl_pct > 0 or exit_reason in ("TAKE_PROFIT", "TP")
        is_sl       = exit_reason in ("STOP_LOSS", "SL", "LIQUIDATION")
        outcome_emoji = "✅" if is_win else "❌"
        pnl_lev     = pnl_pct * leverage

        # ── قراءة شمعات ما بعد الدخول (1m) لفهم حركة السعر ──
        price_story = ""
        candles_1m  = candle_snap.get("1m", [])
        if candles_1m and entry_price > 0:
            try:
                # نأخذ أول 10 شمعات بعد الدخول
                moves = []
                for c in candles_1m[:10]:
                    if isinstance(c, (list, tuple)) and len(c) >= 5:
                        close = float(c[4])
                        chg   = (close - entry_price) / entry_price * 100
                        moves.append(chg)

                if moves:
                    first_3  = [m for m in moves[:3]]
                    peak_fav = max(moves) if side == "BUY" else -min(moves)
                    peak_adv = -min(moves) if side == "BUY" else max(moves)

                    first_dir = "صعد" if (side=="BUY" and first_3[0]>0) or (side=="SELL" and first_3[0]<0) else "هبط"
                    if abs(peak_adv) < 0.3:
                        price_story = f"السعر {first_dir} لصالحنا مباشرةً تقريباً بدون ارتداد يُذكر."
                    elif abs(peak_adv) < 1.0:
                        price_story = f"السعر ارتد قليلاً ({abs(peak_adv):.2f}%) ثم عاد لصالحنا."
                    elif abs(peak_adv) >= 1.0 and is_sl:
                        price_story = f"السعر ارتد بقوة ({abs(peak_adv):.2f}%) ضدنا ووصل للستوب."
                    else:
                        price_story = f"السعر تذبذب ({abs(peak_adv):.2f}% ضدنا) قبل أن يتحرك لصالحنا."
            except Exception:
                price_story = ""

        # ── تحليل جودة الدخول من TradeJournal ──────────────
        try:
            fb      = tj.build_post_trade_feedback(entry_meta, exit_det)
            quality = fb.get("quality", "UNKNOWN")
        except Exception:
            fb, quality = {}, "UNKNOWN"

        quality_map = {
            "PERFECT":    ("🎯 دخول مثالي", "السعر انطلق مباشرةً بدون ارتداد يُذكر"),
            "GOOD":       ("✅ دخول جيد",   "الدخول في نقطة مناسبة"),
            "EARLY_EXIT": ("⚠️ خروج مبكر", "ربما أغلقنا قبل الهدف الكامل"),
            "BAD_ENTRY":  ("🔴 دخول ضعيف", "الارتداد قبل الحركة كان كبيراً"),
            "UNKNOWN":    ("📋 تحليل", ""),
        }
        quality_title, quality_desc = quality_map.get(quality, ("📋 تحليل", ""))

        # ── تحديد سبب الخروج بلغة طبيعية ──────────────────
        if is_sl:
            exit_narrative = (
                f"ثم ارتدّ السعر {f'وضرب الستوب' if abs(max_adv)>0.5 else 'ولامس الستوب'}. "
                f"الحمد لله على كل حال، نتعوض بغيرها إن شاء الله. 💪"
            )
        elif is_win:
            if max_fav > 1.5:
                exit_narrative = (
                    f"ثم طار السعر لصالحنا {max_fav:.2f}% — ماشاء الله دخول موفق! 🚀"
                )
            else:
                exit_narrative = (
                    f"ثم تحرك لصالحنا وأغلق عند الهدف. الحمد لله ✅"
                )
        else:
            exit_narrative = f"أُغلقت بسبب: {exit_reason}."

        # ── إعادة بناء النص الروائي ────────────────────────
        entry_str  = f"{entry_price:.6g}" if entry_price else "؟"
        exit_str   = f"{exit_price:.6g}"  if exit_price  else "؟"
        sl_str     = f"{sl:.6g}"          if sl          else "؟"
        tp_str     = f"{tp:.6g}"          if tp          else "؟"

        lines = [
            f"{outcome_emoji} <b>{symbol}</b> — {direction}",
            f"──────────────────────",
        ]

        # القصة الروائية
        if entry_price:
            lines.append(
                f"📌 دخلنا <b>{direction.split()[0]}</b> من سعر <code>{entry_str}</code> "
                f"{'⬆️' if side=='BUY' else '⬇️'} | رافعة {leverage:.0f}x"
            )
        if sl_str != "؟":
            lines.append(f"🛡️ الستوب: <code>{sl_str}</code> | الهدف: <code>{tp_str}</code>")

        lines.append("")

        if price_story:
            lines.append(f"📖 <i>{price_story}</i>")

        lines.append(f"<i>{exit_narrative}</i>")
        lines.append("")

        # الأرقام
        lines.append(f"📊 <b>النتيجة:</b>")
        lines.append(f"الخروج: <code>{exit_str}</code> | سبب: {exit_reason}")
        if pnl_pct != 0:
            pnl_sign = "+" if pnl_pct > 0 else ""
            lines.append(
                f"P&L: <b>{pnl_sign}{pnl_pct:.2f}%</b> "
                f"({pnl_sign}{pnl_lev:.1f}% بالرافعة {leverage:.0f}x)"
            )
        if max_fav or max_adv:
            lines.append(
                f"أقصى تحرك لصالحنا: <b>+{max_fav:.2f}%</b> | ضدنا: <b>-{max_adv:.2f}%</b>"
            )

        lines.append("")
        lines.append(f"{quality_title}: {quality_desc}")
        if entry_time:
            lines.append(f"⏰ وقت الدخول: <code>{str(entry_time)[:16]}</code>")

        return "\n".join(lines)

    def edit_message(self, chat_id, message_id, text: str, reply_markup: dict = None):
        """
        تعديل رسالة موجودة عند الضغط على الأزرار.
        إذا فشل HTML، يرسل رسالة جديدة كنص عادي بدل أن يختفي الرد.
        """
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        if reply_markup:
            payload["reply_markup"] = reply_markup

        result = self._post("editMessageText", payload, timeout=10)

        # لو فشل HTML، نرسل رسالة جديدة كنص عادي بدل أن يختفي الرد
        if result is None:
            plain_payload = {
                "chat_id": chat_id,
                "text": self._strip_html(text),
                "disable_web_page_preview": True,
            }
            if reply_markup:
                plain_payload["reply_markup"] = reply_markup
            try:
                response = requests.post(
                    f"{self.base_url}/sendMessage", json=plain_payload, timeout=10
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(f"فشل fallback لإرسال رسالة بديلة بعد فشل editMessageText: {e}")
                return None

        return result

    def answer_callback(self, callback_query_id, text: str = "تم ✅", show_alert: bool = False):
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        return self._post("answerCallbackQuery", payload, timeout=5)

    # ==========================================================
    # GUI Pages
    # ==========================================================
    def welcome_text(self):
        return """
🤖 <b>لوحة التحكم الذكية لبوت التداول</b>

مرحباً بك في واجهة التحكم الجديدة.
يمكنك الآن إدارة البوت من الأزرار مباشرة بدون كتابة الأوامر يدوياً.

<b>اختر من القائمة:</b>
📊 متابعة الرصيد والصفقات.
🧠 تحليل عملة بالذكاء الاصطناعي.
🩺 فحص صحة النظام.
⚙️ تعديل الحد الأقصى للصفقات.
🔄 إعادة تشغيل آمنة.
🛑 إيقاف طوارئ.
"""

    def help_text(self):
        return """
❓ <b>دليل استخدام البوت</b>

<b>الأوامر المتاحة:</b>

🏠 <code>/start</code> أو <code>/menu</code>
فتح لوحة التحكم الرئيسية.

📊 <code>/status</code>
عرض الرصيد، الربح/الخسارة، والصفقات المفتوحة.

🧠 <code>/analyze BTC</code>
تحليل عملة محددة. مثال:
<code>/analyze BTC</code>
<code>/analyze ETH</code>

🧪 <code>/apis</code>
فحص حالة الخدمات والاتصال.

⚙️ <code>/setmax 5</code>
تغيير الحد الأقصى للصفقات المفتوحة.

🎚️ <code>/filters</code>
تغيير وضع الفلاتر وشروط الدخول.

🩺 <code>/health</code>
عرض تقرير صحة النظام.

🔄 <code>/restart</code>
إعادة تشغيل البوت بعد حفظ الحالة.

🛑 <code>/stop</code>
إيقاف طوارئ بعد حفظ الحالة.

<b>ملاحظة:</b>
يمكنك تنفيذ أغلب الأوامر عبر الأزرار بدون كتابة.
"""

    def send_home(self):
        return self.send_message(self.welcome_text(), reply_markup=self.main_menu_keyboard())

    def send_help(self):
        return self.send_message(self.help_text(), reply_markup=self.main_menu_keyboard())

    # ==========================================================
    # Alerts
    # ==========================================================
    def send_trade_proposal(self, proposal: dict):
        """Publish the one authoritative pre-order proposal.

        Monitoring, shadow and incomplete-candidate payloads never call this
        method.  A successful return is required before the Demo entry POST.
        """
        side = str(proposal.get("side") or "").upper()
        emoji = "🟢" if side == "BUY" else "🔴"
        action = "شراء (LONG)" if side == "BUY" else "بيع (SHORT)"
        stages = proposal.get("target_stages") or []
        stage_lines = []
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            try:
                price = float(stage.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                continue
            label = self._safe_html(str(stage.get("label") or f"مرحلة {index + 1}"))
            stage_lines.append(f"   • {label}: <code>{price:.6f}</code>")
        stages_text = "\n".join(stage_lines)
        if stages_text:
            stages_text = f"\n📶 <b>مراحل إدارة الصفقة:</b>\n{stages_text}"

        text = (
            f"{emoji} <b>صفقة مقترحة فعلية — Demo</b> {emoji}\n\n"
            f"📌 <b>الزوج:</b> <code>{self._safe_html(str(proposal.get('symbol') or ''))}</code>\n"
            f"📈 <b>العملية:</b> {action}\n"
            f"💰 <b>الدخول التنفيذي:</b> <code>{float(proposal['proposed_entry']):.6f}</code>\n"
            f"🛑 <b>الوقف الأصلي:</b> <code>{float(proposal['original_stop']):.6f}</code>\n"
            f"🎯 <b>الهدف الأصلي:</b> <code>{float(proposal['original_target']):.6f}</code>"
            f"{stages_text}\n\n"
            f"🆔 <code>{self._safe_html(str(proposal.get('proposal_id') or ''))}</code>\n"
            "<i>هذه هي نقطة التنفيذ authoritative نفسها؛ سيُرسل أمر Demo مرة واحدة فقط.</i>"
        )
        return self.send_message(text)

    def send_trade_alert(self, symbol, side, amount, price, sl, tp, sentiment_label):
        """
        تنسيق وإرسال رسالة تنبيه بصفقة جديدة.
        """
        emoji = "🟢" if side.lower() == "buy" else "🔴"
        action = "شراء (LONG)" if side.lower() == "buy" else "بيع (SHORT)"

        text = f"""
{emoji} <b>تنفيذ صفقة جديدة</b> {emoji}

📌 <b>الزوج:</b> <code>{self._safe_html(symbol)}</code>
📈 <b>العملية:</b> {action}
⚖️ <b>الكمية:</b> <code>{self._safe_html(amount)}</code>

💰 <b>دخول:</b> <code>{float(price):.6f}</code>
🛑 <b>وقف:</b> <code>{float(sl):.6f}</code>
🎯 <b>هدف:</b> <code>{float(tp):.6f}</code>

🧠 <b>المشاعر:</b> {self._safe_html(str(sentiment_label).upper())}
"""

        keyboard = {
            "inline_keyboard": [
                [
                    self._make_btn("📊 حالة البوت", "gui_status", "primary", icon_key="bar_chart"),
                    self._make_btn("🧠 تحليل نفس العملة", f"analyze_{symbol.split('/')[0].replace(':', '')}", "primary", icon_key="brain_ai"),
                ],
                [
                    self._make_btn("🏠 القائمة الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

        return self.send_message(text, reply_markup=keyboard)

    def send_scan_summary(self, total_scanned: int, executed_count: int, trade_executed: bool, scan_results: list = None):
        """
        رسالة ذكية بعد كل جولة مسح:
        - إذا وُجدت عملات أعطت إشارة ورُفضت → تُعرض مع السبب (مفيدة للمراقبة)
        - إذا لا توجد أي إشارات → رسالة قصيرة نظيفة مع زر Dashboard
        """
        webapp_url = getattr(Config, "WEBAPP_URL", "https://tradingbot-production-0b71.up.railway.app/")

        # المرشح ليس مجرد REJECT: قد يكون في انتظار تصحيح/تأكيد/تحسن RR.
        # ``pipeline_progress_pct`` يقيس مراحل المسار التي اجتازها فقط، وليس
        # احتمال الربح، ولذلك لا نسميه ثقة أو جودة الصفقة.
        notable = []
        if scan_results:
            for r in scan_results:
                assessment = r.get("candidate_assessment") or {}
                status = str(assessment.get("status") or "")
                if (
                    assessment.get("is_candidate")
                    and status not in {"IGNORED", "IN_POSITION", "EXECUTED"}
                ):
                    notable.append(r)
                elif "REJECT" in str(r.get("decision", "")):
                    notable.append(r)
        notable.sort(key=lambda item: int(
            (item.get("candidate_assessment") or {}).get("candidate_rank") or 9999
        ))

        # ── بناء نص الرسالة ──
        result_emoji = "✅" if trade_executed else "⏳"
        if trade_executed:
            result_text = f"✅ تم تنفيذ <b>{executed_count}</b> صفقة!"
        elif notable:
            result_text = (
                f"لم تُنفّذ صفقة؛ ظهرت <b>{len(notable)}</b> أفكار مراقبة أو إشارات أولية "
                "ولكل واحدة حالة وما تنتظره قبل الدخول."
            )
        else:
            result_text = "لم تظهر إشارة مكتملة تستحق الدخول حالياً."

        if not notable:
            # رسالة قصيرة ونظيفة عند غياب أي إشارات
            text = (
                f"📊 <b>جولة مسح السوق اكتملت</b>\n"
                f"🔍 تم فحص <code>{total_scanned}</code> عملة\n"
                f"{result_emoji} {result_text}\n\n"
                f"<i>افتح الداشبورد لرؤية تفاصيل كل عملة 👇</i>"
            )
        else:
            # Telegram ملخص سريع؛ التفاصيل الكاملة تبقى في Mini App.
            text = (
                f"📡 <b>تقرير مسح السوق</b>\n"
                f"🔍 فُحص <code>{total_scanned}</code> عملة | {result_text}\n"
                f"━━━━━━━━━━━━━━\n\n"
            )
            shown = notable[:5]
            for r in shown:
                coin   = r.get("coin", "?")
                price  = float(r.get("price", 0) or 0)
                rsi    = float(r.get("rsi", 0) or 0)
                stage = self._scan_stage_label(r.get("rejection_stage"))
                htf_trend = self._scan_trend_label(r.get("htf_trend"))
                reason = self._scan_rejection_explanation(r)
                assessment = r.get("candidate_assessment") or {}
                execution_side_confirmed = bool(
                    assessment.get("execution_side_confirmed", bool(r.get("side")))
                )
                direction = self._scan_direction_label(
                    r.get("side") or assessment.get("bias")
                )
                status_label = str(assessment.get("status_label_ar") or "")
                next_trigger = str(assessment.get("next_trigger") or "")

                # وصف RSI؛ الأيقونة وحدها كانت غامضة للمستخدم.
                if rsi >= 70:
                    rsi_label = "مرتفع"
                elif rsi <= 30:
                    rsi_label = "منخفض"
                else:
                    rsi_label = "محايد"

                candidate_title = "مرشح لم يكتمل" if execution_side_confirmed else "فكرة قيد المراقبة"
                text += f"⛔ <b>{candidate_title}</b> | <code>{self._safe_html(coin)}</code>\n"
                if direction:
                    direction_title = "الاتجاه المقترح" if execution_side_confirmed else "ميل المراقبة"
                    text += f"   ↕️ {direction_title}: <b>{direction}</b>\n"
                if status_label:
                    text += f"   🧭 الحالة: <b>{self._safe_html(status_label)}</b>\n"
                text += f"   🧱 توقّف عند: <b>{stage}</b>\n"
                if price > 0:
                    price_text = self._format_scan_price(price)
                    text += f"   💲 السعر: <code>{price_text}</code>\n"
                if rsi > 0:
                    text += f"   📊 RSI: <code>{rsi:.1f}</code> ({rsi_label})\n"
                if htf_trend:
                    text += f"   🕐 اتجاه الساعة: <b>{htf_trend}</b>\n"
                if next_trigger:
                    if len(next_trigger) > 110:
                        next_trigger = next_trigger[:107].rstrip() + "…"
                    text += f"   ⏭️ المطلوب تالياً: {self._safe_html(next_trigger)}\n"
                if reason:
                    # مساحة كافية لشرح السبب دون إغراق رسالة الجولة.
                    if len(reason) > 150:
                        cut = reason[:150]
                        last_space = cut.rfind(' ')
                        if last_space > 100:
                            cut = cut[:last_space]
                        reason_short = cut + "…"
                    else:
                        reason_short = reason
                    text += f"   📝 السبب: {self._safe_html(reason_short)}\n"
                text += "\n"

            if len(notable) > 5:
                text += f"<i>...و {len(notable) - 5} مرشح آخر؛ التفاصيل الكاملة في Mini App.</i>\n\n"
            else:
                text += "<i>التفاصيل ومراحل القرار كاملة داخل Mini App.</i>\n\n"

        keyboard = {
            "inline_keyboard": [
                [
                    self._make_btn("📊 فتح لوحة التحكم", web_app={"url": webapp_url}, style="primary", icon_key="bar_chart")
                ],
                [
                    self._make_btn("⚡ مسح فوري", "force_scan_now", "success", icon_key="lightning"),
                    self._make_btn("📋 حالة البوت", "gui_status", "primary", icon_key="report_document"),
                ],
            ]
        }
        return self.send_message(text, reply_markup=keyboard)

    def send_pnl_alert(self, symbol, order_type, price, pnl, pnl_pct=0.0, wallet_pnl_pct=0.0, pnl_ref_50=0.0, reference_balance=None):
        """
        إرسال إشعار عند إغلاق صفقة.
        """
        pnl_value = float(pnl or 0.0)
        is_profit = pnl_value > 1e-12
        is_loss = pnl_value < -1e-12
        emoji = "🎉" if is_profit else ("⚠️" if is_loss else "⚖️")

        order_type = str(order_type or "UNKNOWN_CLOSE").upper()
        if "TAKE_PROFIT" in order_type:
            result_text = "🎯 الحمد لله ضربت الهدف"
        elif "STOP" in order_type and is_profit:
            result_text = "🛡️ إغلاق بستوب متحرك رابح (Trailing Stop)"
        elif "STOP" in order_type:
            result_text = "🛑 تم الإغلاق بوقف الخسارة"
        elif "MANUAL" in order_type:
            result_text = "👤 إغلاق يدوي/خارجي مؤكد"
        elif "PROTECTION_FAILED" in order_type or "EMERGENCY" in order_type:
            result_text = "🚨 إغلاق طارئ بسبب فشل الحماية"
        else:
            result_text = "❔ إغلاق مؤكد وسبب التنفيذ غير محسوم"

        pnl_color = "🟢 ربح" if is_profit else ("🔴 خسارة" if is_loss else "⚪ تعادل")
        ref_bal = float(reference_balance) if reference_balance is not None else float(getattr(Config, "REFERENCE_BALANCE", 60.0))
        if not math.isfinite(ref_bal) or ref_bal <= 0:
            ref_bal = 100.0
        wallet_pct = float(wallet_pnl_pct or 0.0)
        if not math.isfinite(wallet_pct):
            wallet_pct = 0.0
        # Recompute the normalized amount from the displayed wallet return so
        # the two lines can never contradict each other.  pnl_ref_50 remains
        # in the signature for stored-record compatibility only.
        normalized_pnl = (wallet_pct / 100.0) * ref_bal
        supplied_normalized = float(pnl_ref_50 or 0.0)
        if math.isfinite(supplied_normalized) and abs(supplied_normalized - normalized_pnl) > 0.0001:
            logger.warning(
                "[PnLDisplay] corrected inconsistent normalized PnL: supplied=%s computed=%s",
                supplied_normalized,
                normalized_pnl,
            )
        normalized_pct = (normalized_pnl / ref_bal * 100.0) if ref_bal > 0 else 0.0

        text = f"""
{emoji} <b>إغلاق صفقة</b> {emoji}

📌 <b>الزوج:</b> <code>{self._safe_html(symbol)}</code>
🚦 <b>النتيجة:</b> {result_text}

💰 <b>سعر الإغلاق:</b> <code>{float(price):.6f}</code>
💵 <b>الصافي (Binance/Testnet):</b> <code>{pnl_value:+.4f} USDT</code> {pnl_color}
📊 <b>النسبة للمحفظة الفعلية:</b> <code>{wallet_pct:+.4f}%</code>

🧪 <b>نتيجة معيارية لمحفظة ({ref_bal:.2f}$):</b>
تطبيق نفس نسبة العائد الفعلية على محفظة بهذا الحجم:
• <b>الربح/الخسارة:</b> <code>{normalized_pnl:+.4f} USDT</code>
• <b>النمو/التراجع:</b> <code>{normalized_pct:+.4f}%</code>
"""

        keyboard = {
            "inline_keyboard": [
                [
                    self._make_btn("📊 تحديث الحالة", "gui_status", "primary", icon_key="refresh_restart"),
                    self._make_btn("🧠 تحليل نفس العملة", f"analyze_{symbol.split('/')[0].replace(':', '')}", "primary", icon_key="brain_ai"),
                ],
                [
                    self._make_btn("🏠 القائمة الرئيسية", "gui_home", "primary", icon_key="home"),
                ],
            ]
        }

        return self.send_message(text, reply_markup=keyboard)

    # ==========================================================
    # Status Report
    # ==========================================================
    def build_status_message(self, client, state_manager):
        """
        بناء رسالة حالة البوت.
        تم فصلها حتى تعمل مع الأمر النصي ومع الأزرار.
        """
        # 1. جلب الرصيد والـ Equity.
        # المصدر الموحد للرصيد: في وضع Testnet نستخدم PerformanceTracker (نفس مصدر الحقيقة في main.py)
        # وليس client.get_balance() مباشرةً لأن Binance Demo يعيد صفراً دائماً.
        simulated_cap = float(getattr(Config, "TESTNET_SIMULATED_BALANCE", 0) or 0)
        use_testnet   = getattr(Config, "USE_TESTNET", False)

        if use_testnet and simulated_cap > 0:
            # ── وضع Testnet: نعتمد على المحفظة الوهمية التراكمية ──
            try:
                bal  = client.get_balance()
                info = bal.get("info", {}) if bal else {}
                total_unrealized = float(info.get("totalUnrealizedProfit", 0) or 0)

                tracker    = PerformanceTracker(state_manager)
                sim_wallet = tracker.get_wallet(unrealized_pnl=total_unrealized)

                equity          = float(sim_wallet.get("equity",         simulated_cap))
                wallet_balance  = float(sim_wallet.get("wallet_balance", simulated_cap))
                usdt_available  = float(sim_wallet.get("available",      simulated_cap))
                realized_pnl    = float(sim_wallet.get("realized_pnl",   0.0))

                equity_str         = f"{equity:.2f} USDT (وهمي)"
                wallet_balance_str = f"{wallet_balance:.2f} USDT (وهمي)"
                usdt_available_str = f"{usdt_available:.2f} USDT (وهمي)"
                pnl_day_str        = f"{realized_pnl:+.2f} USDT"
            except Exception as e:
                logger.error(f"فشل حساب المحفظة الوهمية في حالة البوت: {e}")
                equity_str = wallet_balance_str = usdt_available_str = "غير متاح ⏳"
                pnl_day_str = "جاري التحديث ⏳"
        else:
            # ── وضع Live: نجلب الرصيد الحقيقي من Binance ──
            bal      = client.get_balance()
            bal_failed = bal is None

            if bal_failed:
                usdt_available_str = "غير متاح لحظياً ⏳"
                wallet_balance_str = "غير متاح لحظياً ⏳"
                equity_str         = "غير متاح لحظياً ⏳"
                pnl_day_str        = "جاري التحديث ⏳"
            else:
                info             = bal.get("info", {})
                usdt_available   = float(bal.get("USDT", {}).get("free", 0) or 0)
                wallet_balance   = float(info.get("totalWalletBalance", usdt_available) or 0)
                total_unrealized = float(info.get("totalUnrealizedProfit", 0) or 0)
                equity           = float(info.get("totalMarginBalance", wallet_balance + total_unrealized) or 0)
                initial          = state_manager.get_initial_balance()
                pnl_day          = wallet_balance - initial if initial else 0

                usdt_available_str = f"{usdt_available:.2f} USDT"
                wallet_balance_str = f"{wallet_balance:.2f} USDT"
                equity_str         = f"{equity:.2f} USDT"
                pnl_day_str        = f"{pnl_day:+.2f} USDT"

        # 2. جلب الصفقات مباشرة من باينانس. None تعني UNKNOWN، وليست صفراً.
        live_positions = _fetch_live_positions_for_status(client, state_manager)

        from utils.operational_snapshot import build_operational_snapshot
        operational = build_operational_snapshot(state_manager, client=client)
        runtime_info = operational.get("runtime") or {}
        runtime_mode = operational.get("mode") or {}
        reconciliation = operational.get("reconciliation") or {}
        proposal_summary = operational.get("proposal_summary") or {}
        protection_summary = operational.get("protection_summary") or {}

        msg = "📊 <b>حالة البوت الحالية</b>\n"
        msg += "━━━━━━━━━━━━━━\n\n"
        msg += (
            f"🏷️ <b>الإصدار:</b> <code>{self._safe_html(runtime_info.get('app_version') or APP_VERSION)}"
            f" • {self._safe_html(str(runtime_info.get('commit') or SHORT_COMMIT)[:7])}</code>\n"
            f"🌐 <b>التنفيذ:</b> <code>{'DEMO' if runtime_mode.get('use_testnet') else 'LIVE'}"
            f" | {'ON' if runtime_mode.get('demo_execution_enabled') else 'OFF'}"
            f" | cap={runtime_mode.get('effective_position_cap', 0)}</code>\n"
            f"♻️ <b>المصالحة:</b> <code>{'RECONCILED' if reconciliation.get('recovery_complete') else 'BLOCKED'}"
            f" | {self._safe_html(str(reconciliation.get('last_successful_at') or 'غير متاح'))}</code>\n"
            f"🔗 <b>Proposal→Entry:</b> <code>{proposal_summary.get('telegram_trade_proposals_count', 0)}"
            f"→{proposal_summary.get('demo_entry_posts_count', 0)}"
            f" | protected={proposal_summary.get('demo_protected_count', 0)}"
            f" | closed={proposal_summary.get('demo_closed_reconciled_count', 0)}</code>\n"
            f"🛡️ <b>الحماية المثبتة:</b> <code>"
            f"{protection_summary.get('managed_position_count', 0) - len(protection_summary.get('unprotected_managed_symbols') or [])}"
            f"/{protection_summary.get('managed_position_count', 0)}</code>\n\n"
        )
        msg += f"💼 <b>Equity:</b> <code>{equity_str}</code>\n"
        msg += f"💰 <b>Wallet Balance:</b> <code>{wallet_balance_str}</code>\n"
        msg += f"💵 <b>الرصيد المتاح:</b> <code>{usdt_available_str}</code>\n"
        msg += f"📈 <b>ربح/خسارة اليوم:</b> <code>{pnl_day_str}</code>\n"
        positions_count = len(live_positions) if live_positions is not None else "غير معروف"
        msg += f"🟢 <b>الصفقات المفتوحة:</b> <code>{positions_count}</code>\n"

        # Trading Filter Profile Status
        msg += "\n" + format_filter_profile_status(state_manager) + "\n"

        max_trades = state_manager.get_max_open_trades()
        msg += f"⚙️ <b>الحد الأقصى للصفقات:</b> <code>{max_trades}</code>\n"
        demo_signal_mode = state_manager.get_demo_signal_execution_mode()
        demo_signal_label = (
            "⚡ كل قرار HYBRID BUY/SELL"
            if demo_signal_mode == "hybrid_decision"
            else "🔒 Proposal الصارم بعد الفلاتر"
        )
        msg += f"🎯 <b>تنفيذ Demo:</b> <code>{demo_signal_label}</code>\n"
        mode_label = "TESTNET" if use_testnet else "LIVE"
        effective_risk = state_manager.get_risk_pct()
        effective_leverage = state_manager.get_leverage()
        auto_ai = getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False)
        msg += f"🧪 <b>وضع المنصة:</b> <code>{mode_label}</code>\n"
        msg += f"🎯 <b>حد المخاطرة عند الوقف/صفقة:</b> <code>{effective_risk:.3f}%</code>\n"
        msg += f"⚙️ <b>الرافعة الفعلية:</b> <code>{effective_leverage}x</code>\n"
        msg += f"💵 <b>هامش الدخول المستهدف:</b> <code>{Config.ENTRY_MARGIN_PERCENT:g}% من رأس المال</code>\n"
        msg += f"🧠 <b>AI الآلي:</b> {'مفعّل ⚠️' if auto_ai else 'معطّل ✅'}\n"

        ref_bal = state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0))
        msg += (
            f"💰 <b>محفظة محاكاة التقارير:</b> <code>${ref_bal:.2f} USDT</code>\n"
            "ℹ️ لا تحدد حجم الصفقة؛ المخاطرة تُحسب من رصيد Binance الفعلي.\n"
        )

        if live_positions:
            msg += "\n<b>تفاصيل الصفقات المفتوحة:</b>\n"
            msg += "━━━━━━━━━━━━━━\n"

            try:
                for pos in live_positions:
                    amt = float(pos.get("info", {}).get("positionAmt", 0))
                    sym = pos.get("symbol", "").split(":")[0]
                    unrealized_pnl = float(pos.get("info", {}).get("unRealizedProfit", 0))
                    realized_pnl   = float(pos.get("info", {}).get("realizedPnl", 0))
                    total_pnl      = unrealized_pnl + realized_pnl
                    entry_price    = float(pos.get("entryPrice", 0) or pos.get("info", {}).get("entryPrice", 0) or 0)
                    # Binance Futures: leverage يأتي من pos['leverage'] وليس من info
                    leverage = float(
                        pos.get("leverage") or
                        pos.get("info", {}).get("leverage") or
                        1
                    )

                    initial_margin = (abs(amt) * entry_price) / leverage if leverage else 0
                    roe = (total_pnl / initial_margin * 100) if initial_margin > 0 else 0.0

                    pos_type = "شراء LONG 🟢" if amt > 0 else "بيع SHORT 🔴"
                    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
                    sign = "+" if total_pnl >= 0 else ""

                    mark_price = float(pos.get("info", {}).get("markPrice", 0))
                    if mark_price == 0:
                        mark_price = client.get_current_price(sym) or entry_price

                    coin_name = sym.split("/")[0]

                    # جلب SL الأصلي والحالي من entry_metadata
                    try:
                        _meta = state_manager.get("entry_metadata", {}) if state_manager else {}
                        _bot  = _meta.get(sym) or _meta.get(sym.replace("/", "").replace(":USDT", "") + "USDT") or {}
                        _sl_init    = float(_bot.get("sl",         0) or 0)
                        _sl_current = float(_bot.get("current_sl", 0) or 0)
                        _sl_moved   = _sl_current > 0 and abs(_sl_current - _sl_init) > 0.000001
                        _external   = bool(_bot.get("external_position", False))
                        _protection = str(_bot.get("protection_status") or "UNKNOWN").upper()
                    except Exception:
                        _sl_init, _sl_current, _sl_moved, _external, _protection = 0, 0, False, False, "UNKNOWN"

                    msg += f"\n\u200F🔹 <b>{self._safe_html(sym)}</b> | {pos_type}\n"
                    if _external:
                        msg += (
                            "\u200F   ⚠️ <b>مركز خارجي/غير متتبّع — غير مُدار بواسطة البوت</b>\n"
                            "\u200F   ℹ️ لا يضيف البوت له SL أو TP أو Trailing Stop تلقائياً.\n"
                        )
                    msg += f"\u200F   ⚖️ الكمية: <code>{abs(amt)} {self._safe_html(coin_name)}</code>\n"
                    msg += f"\u200F   💰 الدخول: <code>{entry_price:.5f}</code>\n"
                    msg += f"\u200F   📍 الحالي: <code>{mark_price:.5f}</code>\n"
                    if _sl_init > 0:
                        msg += f"\u200F   🛑 SL الأصلي: <code>{_sl_init:.5f}</code>\n"
                    if _sl_moved:
                        msg += f"\u200F   🛡️ SL الحالي: <code>{_sl_current:.5f}</code> <i>(متحرك)</i>\n"
                    if not _external:
                        protection_icon = "✅" if _protection == "PROTECTED" else "🚨"
                        msg += f"\u200F   {protection_icon} الحماية: <code>{self._safe_html(_protection)}</code>\n"
                    realized_label = f" <i>(محقق: {realized_pnl:+.2f})</i>" if abs(realized_pnl) > 0.0001 else ""
                    msg += f"\u200F   {pnl_emoji} الربح: <code>{sign}{total_pnl:.2f} USDT</code> | ROE <code>{sign}{roe:.2f}%</code>{realized_label}\n"

            except Exception as e:
                logger.error(f"خطأ في جلب PNL للتيليجرام: {e}")
                msg += "\n⚠️ تعذر جلب بعض تفاصيل الصفقات.\n"
        elif live_positions == []:
            msg += "\n✅ لا توجد صفقات مفتوحة حالياً.\n"
        else:
            msg += (
                "\n⚠️ تعذر التحقق من المراكز على Binance؛ "
                "لم تُمسح حالة الصفقات المحلية.\n"
            )

        msg += "\n━━━━━━━━━━━━━━\n"
        msg += "🛡️ <b>أنظمة الحماية:</b>\n"

        sl_guard_on = state_manager.get_sl_guard_enabled()
        sl_guard_pause = state_manager.get_sl_guard_pause()
        from datetime import datetime as _dt
        sl_status = "⏸️ موقوف" if (sl_guard_pause and _dt.now() < sl_guard_pause) else ("✅" if sl_guard_on else "❌ معطّل")
        msg += f"  🛡️ حارس وقف الخسارة: {sl_status}\n"

        dd_on = state_manager.get_max_drawdown_enabled()
        dd_pause = state_manager.get_max_drawdown_pause()
        dd_status = "⏸️ موقوف" if (dd_pause and _dt.now() < dd_pause) else ("✅" if dd_on else "❌ معطّل")
        msg += f"  📉 حارس الخسارة المحققة المتحركة: {dd_status} (حد: {state_manager.get_max_drawdown_pct():.0f}%)\n"

        lp_on = state_manager.get_low_profit_enabled()
        frozen_count = len(state_manager.get_frozen_pairs())
        lp_status = "✅" if lp_on else "❌ معطّل"
        msg += f"  ❄️ تجميد أزواج الخسارة: {lp_status} ({frozen_count} مجمّدة)\n"

        msg += "  🔒 وقف خسارة باينانس: إلزامي ✅\n"

        msg += "\n━━━━━━━━━━━━━━\n"
        msg += "استخدم الأزرار بالأسفل للتحديث أو الرجوع."

        return msg

    def send_status(self, client, state_manager):
        try:
            msg = self.build_status_message(client, state_manager)
            return self.send_message(msg, reply_markup=self.back_home_keyboard())
        except Exception as e:
            logger.error(f"خطأ في بناء حالة البوت: {e}")
            return self.send_message("⚠️ تعذر عرض حالة البوت حالياً.", reply_markup=self.main_menu_keyboard())

    # ==========================================================
    # Polling / Commands / Callback GUI
    # ==========================================================
    def start_polling(self, client, state_manager, ai_engine=None, ta_engine=None, news_engine=None, social_engine=None):
        """
        تشغيل خيط بالخلفية للاستماع إلى أوامر التيليجرام.
        تم تحسينه لدعم الأزرار GUI عبر callback_query.
        """
        self._runtime_client = client
        self._runtime_state_manager = state_manager
        if not self._is_configured():
            return

        def poll():
            offset = None
            session = requests.Session()
            self._poll_session = session

            # تفريغ الرسائل القديمة لتجنب تنفيذ أوامر قديمة.
            try:
                init_resp = session.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": -1, "timeout": 5},
                    timeout=10,
                )
                if init_resp.status_code == 200:
                    init_data = init_resp.json()
                    if init_data.get("ok") and init_data.get("result"):
                        last_update_id = init_data["result"][0]["update_id"]
                        offset = last_update_id + 1
                        session.get(
                            f"{self.base_url}/getUpdates",
                            params={"offset": offset, "timeout": 5},
                            timeout=10,
                        )
            except Exception:
                pass

            logger.info("📡 بدء الاستماع لأوامر تيليجرام مع واجهة GUI محسّنة.")

            while not self._poll_stop.is_set():
                try:
                    params = {"timeout": 30}
                    if offset:
                        params["offset"] = offset

                    response = session.get(f"{self.base_url}/getUpdates", params=params, timeout=35)
                    data = response.json()

                    if data.get("ok"):
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1

                            if "callback_query" in update:
                                self.handle_callback_query(
                                    update["callback_query"],
                                    client,
                                    state_manager,
                                    ai_engine,
                                    ta_engine,
                                    news_engine,
                                    social_engine,
                                    session,
                                    offset,
                                )
                                continue

                            message = update.get("message", {})
                            text = message.get("text", "") or ""
                            chat_id = message.get("chat", {}).get("id")

                            if str(chat_id) != str(self.chat_id):
                                continue
                            actor_id = (message.get("from") or {}).get("id")
                            if not self._authorized_actor_user_id(actor_id):
                                logger.warning(
                                    "[Telegram] رُفض أمر نصي من مستخدم غير مصرح داخل chat مسموح."
                                )
                                continue

                            self.handle_text_message(
                                text,
                                chat_id,
                                client,
                                state_manager,
                                ai_engine,
                                ta_engine,
                                news_engine,
                                social_engine,
                                session,
                                offset,
                            )

                except requests.exceptions.ReadTimeout:
                    self._poll_stop.wait(1)
                except requests.exceptions.ConnectionError:
                    self._poll_stop.wait(5)
                except Exception as e:
                    logger.error(f"خطأ في الاستماع لأوامر التيليجرام: {e}")
                    self._poll_stop.wait(5)

            session.close()
            self._poll_session = None

        self._poll_thread = threading.Thread(
            target=poll, daemon=True, name="TelegramPolling"
        )
        self._poll_thread.start()
        return self._poll_thread

    def stop_polling(self, timeout=5.0):
        self._poll_stop.set()
        if self._poll_session is not None:
            try:
                self._poll_session.close()
            except Exception:
                pass
        if (
            self._poll_thread
            and self._poll_thread.is_alive()
            and self._poll_thread is not threading.current_thread()
        ):
            self._poll_thread.join(timeout=timeout)

    def handle_text_message(
        self,
        text,
        chat_id,
        client,
        state_manager,
        ai_engine,
        ta_engine,
        news_engine,
        social_engine,
        session=None,
        offset=None,
    ):
        """
        معالجة الرسائل النصية العادية.
        """
        user_key = str(chat_id)
        text = (text or "").strip()

        # معالجة حالات الانتظار أولاً.
        pending = self.user_state.get(user_key)

        if pending == "awaiting_runtime_log_minutes":
            self.user_state.pop(user_key, None)
            if not text.isdigit():
                self.send_message(
                    "⚠️ أدخل عددًا صحيحًا بالدقائق، مثل <code>30</code>.",
                    reply_markup=self.reports_menu_keyboard(),
                )
                return
            minutes = int(text)
            if minutes < 1 or minutes > 168 * 60:
                self.send_message(
                    "⚠️ المدة يجب أن تكون بين دقيقة واحدة و10080 دقيقة (7 أيام).",
                    reply_markup=self.reports_menu_keyboard(),
                )
                return
            self.send_message(
                f"⏳ جاري تجهيز سجل تشغيل البوت لآخر <b>{minutes}</b> دقيقة...",
                reply_markup=self.back_home_keyboard(),
            )
            threading.Thread(
                target=self.send_runtime_logs,
                kwargs={"minutes": minutes},
                name=f"runtime-log-export-{minutes}m",
                daemon=True,
            ).start()
            return

        if pending == "awaiting_setmax":
            if text.isdigit():
                new_max = int(text)
                old = state_manager.get_max_open_trades()
                if not state_manager.set_max_open_trades(new_max):
                    self.user_state.pop(user_key, None)
                    self.send_message(
                        f"❌ القيمة مرفوضة. الحد الصلب هو 1—"
                        f"{Config.HARD_MAX_OPEN_TRADES} صفقات.",
                        reply_markup=self.main_menu_keyboard(),
                    )
                    return
                self._record_config_change(state_manager, "max_open_trades", old, new_max)
                self.user_state.pop(user_key, None)
                self.send_message(
                    f"✅ تم تغيير الحد الأقصى للصفقات المفتوحة إلى: <b>{new_max}</b> صفقات.",
                    reply_markup=self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير الحد الأقصى للصفقات المفتوحة إلى {new_max} عبر تيليجرام.")
            else:
                self.user_state.pop(user_key, None)
                self.send_message(
                    "⚠️ إدخال غير صحيح. تم إلغاء العملية.",
                    reply_markup=self.main_menu_keyboard(),
                )
            return
        if pending == "awaiting_blacklist_gui":
            self.user_state.pop(user_key, None)
            symbol = self.normalize_symbol(text)
            if symbol:
                old = state_manager.get_blacklisted_pairs()
                state_manager.blacklist_pair(symbol)
                self._record_config_change(
                    state_manager, "blacklisted_pairs", old,
                    state_manager.get_blacklisted_pairs(),
                )
                self.send_message(
                    f"✅ تم إضافة <code>{symbol}</code> إلى القائمة السوداء بنجاح.",
                    reply_markup=self.blacklist_menu_keyboard(),
                )
            else:
                self.send_message(
                    "⚠️ صيغة العملة غير صحيحة. تم الإلغاء.",
                    reply_markup=self.blacklist_menu_keyboard(),
                )
            return

        if pending == "awaiting_whitelist_gui":
            self.user_state.pop(user_key, None)
            symbol = self.normalize_symbol(text)
            if symbol:
                old = state_manager.get_blacklisted_pairs()
                state_manager.whitelist_pair(symbol)
                self._record_config_change(
                    state_manager, "blacklisted_pairs", old,
                    state_manager.get_blacklisted_pairs(),
                )
                self.send_message(
                    f"✅ تم إزالة <code>{symbol}</code> من القائمة السوداء بنجاح.",
                    reply_markup=self.blacklist_menu_keyboard(),
                )
            else:
                self.send_message(
                    "⚠️ صيغة العملة غير صحيحة. تم الإلغاء.",
                    reply_markup=self.blacklist_menu_keyboard(),
                )
            return


        if pending == "awaiting_setlev":
            self.user_state.pop(user_key, None)
            if text.isdigit():
                new_lev = int(text)
                min_lev = int(getattr(Config, "MIN_LEVERAGE", 1))
                max_lev = int(getattr(Config, "MAX_LEVERAGE", 20))
                if not (min_lev <= new_lev <= max_lev):
                    self.send_message(
                        f"❌ الرافعة يجب أن تكون بين {min_lev} و{max_lev}x.",
                        reply_markup=self.main_menu_keyboard(),
                    )
                    return
                old = state_manager.get_leverage()
                if not state_manager.set_leverage(new_lev):
                    self.send_message("❌ تعذر حفظ الرافعة بأمان.")
                    return
                self._record_config_change(state_manager, "leverage", old, new_lev)
                self.send_message(
                    f"✅ تم تغيير الرافعة إلى: <b>{new_lev}x</b>.",
                    reply_markup=self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير الرافعة إلى {new_lev}x عبر تيليجرام.")
            else:
                self.send_message(
                    "⚠️ إدخال غير صحيح. تم إلغاء العملية.",
                    reply_markup=self.main_menu_keyboard(),
                )
            return

        if pending == "awaiting_setref":
            self.user_state.pop(user_key, None)
            try:
                clean_text = text.replace("$", "").strip()
                new_ref = float(clean_text)
                if new_ref <= 0:
                    raise ValueError("Must be positive")
                state_manager.set("reference_balance", new_ref)
                Config.REFERENCE_BALANCE = new_ref
                self.send_message(
                    f"✅ تم تغيير المحفظة المرجعية إلى: <b>${new_ref:.2f} USDT</b>.",
                    reply_markup=self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير المحفظة المرجعية إلى {new_ref} عبر تيليجرام.")
            except Exception:
                self.send_message(
                    "⚠️ إدخال غير صحيح. الرجاء إدخال رقم عشري/صحيح موجب.",
                    reply_markup=self.main_menu_keyboard(),
                )
            return

        if pending == "awaiting_setscan":
            self.user_state.pop(user_key, None)
            if text.isdigit():
                minutes = int(text)
                minutes = max(5, min(minutes, 60))
                new_sec = minutes * 60

                old = state_manager.get("scan_interval_seconds", 600)
                state_manager.set("scan_interval_seconds", new_sec)
                self._record_config_change(state_manager, "scan_interval_seconds", old, new_sec)
                self.send_message(
                    f"✅ تم تغيير فترة المسح إلى: <b>{minutes}</b> دقيقة.",
                    reply_markup=self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير فترة المسح إلى {new_sec} ثانية عبر تيليجرام.")
            else:
                self.send_message(
                    "⚠️ إدخال غير صحيح. الرجاء إدخال رقم بالدقائق.",
                    reply_markup=self.main_menu_keyboard(),
                )
            return

        if pending == "awaiting_analyze":
            sym = self.normalize_symbol(text)
            self.user_state.pop(user_key, None)
            threading.Thread(
                target=self.handle_analyze_request,
                args=(sym, client, state_manager, ai_engine, ta_engine, news_engine, social_engine),
                daemon=True,
            ).start()
            return

        if pending == "awaiting_journal_coin":
            self.user_state.pop(user_key, None)
            coin = text.strip().upper().replace("USDT", "").replace("/", "").replace(":", "")
            if not coin:
                self.send_message(
                    "⚠️ صيغة غير صحيحة. مثال: <code>PLUME</code>",
                    reply_markup=self.journal_menu_keyboard()
                )
                return
            self.send_message(f"⏳ جاري تحليل صفقات <b>{coin}</b>...")
            threading.Thread(
                target=lambda: self._send_journal_report(
                    chat_id=chat_id, mode="symbol",
                    client=client, symbol_filter=coin
                ),
                daemon=True
            ).start()
            return

        # الأوامر.
        if text in ("/start", "/menu"):
            self.send_home()

        elif text == "/help":
            self.send_help()

        elif text == "/status":
            self.send_status(client, state_manager)

        elif text == "/health":
            self.run_health_report(client, state_manager)

        elif text.startswith("/shadow_report"):
            self.send_message("⏳ جاري تحليل بيانات التداول...")
            try:
                from utils.shadow_analyzer import ShadowAnalyzer
                import os
                sa = ShadowAnalyzer(
                    csv_path=os.getenv("TRADE_LOG_PATH", "testnet_trades_log.csv")
                )
                report = sa.build_telegram_report()
                self.send_message(report, reply_markup=self.main_menu_keyboard())
            except Exception as e:
                self.send_message(f"❌ فشل تقرير Shadow Analyzer: {e}")

        elif text.startswith("/journal"):
            try:
                import os, json, glob
                from utils.trade_journal import TradeJournal
                journal_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
                summaries   = sorted(
                    glob.glob(os.path.join(journal_dir, "*/trade_summary.json")),
                    key=os.path.getmtime,
                    reverse=True
                )[:5]

                if not summaries:
                    self.send_message("📭 لا توجد صفقات مسجلة بعد.")
                else:
                    self.send_message(f"📋 <b>آخر {len(summaries)} صفقات:</b>")
                    tj = TradeJournal()
                    for path in summaries:
                        try:
                            with open(path, encoding="utf-8") as f:
                                data = json.load(f)
                            qr      = data.get("quick_read", {})
                            e_meta  = data.get("entry_meta", {})
                            e_exit  = data.get("exit_details", {})
                            sym     = data.get("symbol", "?")
                            pnl     = qr.get("pnl_pct") or 0
                            result  = "✅" if pnl > 0 else "❌"

                            # بناء التغذية الراجعة من البيانات المحفوظة
                            fb = tj.build_post_trade_feedback(e_meta, e_exit)
                            quality_emoji = {"PERFECT": "🌟", "GOOD": "✅", "EARLY_EXIT": "⚠️", "BAD_ENTRY": "🔴"}.get(fb.get("quality",""), "❓")

                            self.send_message(
                                f"{result} <b>{sym}</b>\n"
                                f"دخول: <code>{qr.get('entry')}</code> | خروج: <code>{qr.get('exit_price')}</code>\n"
                                f"P&L: {pnl:+.2f}% | سبب: {qr.get('exit_reason')}\n"
                                f"{quality_emoji} جودة الدخول: {fb.get('reversal_label', 'N/A')}"
                            )
                        except Exception:
                            pass
            except Exception as e:
                self.send_message(f"❌ خطأ في جلب السجل: {e}")

        elif text == "/filters":
            self.send_message(
                f"🎚️ <b>إعدادات الفلاتر الحالية:</b>\n\n"
                f"{format_filter_profile_status(state_manager)}\n\n"
                f"اختر الوضع الجديد من القائمة أدناه:",
                reply_markup=self.filter_profile_keyboard()
            )

        elif text in ["/apis", "/api", "/services"]:
            self.send_message("🧪 جاري فحص كل الخدمات... انتظر قليلاً.")
            threading.Thread(
                target=lambda: self.send_api_status_report(client, state_manager),
                daemon=True
            ).start()

        elif text == "/stop":
            self.stop_bot(state_manager, session=session, offset=offset)

        elif text.startswith("/setmax"):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit():
                new_max = int(parts[1])
                old = state_manager.get_max_open_trades()
                if not state_manager.set_max_open_trades(new_max):
                    self.send_message(
                        f"❌ الحد المسموح هو 1—{Config.HARD_MAX_OPEN_TRADES}."
                    )
                    return
                self._record_config_change(state_manager, "max_open_trades", old, new_max)
                self.send_message(
                    f"✅ تم تغيير الحد الأقصى للصفقات المفتوحة المتزامنة إلى: <b>{new_max}</b> صفقات.",
                    reply_markup=self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير الحد الأقصى للصفقات المفتوحة إلى {new_max} عبر تيليجرام.")
            else:
                self.user_state[user_key] = "awaiting_setmax"
                self.send_message(
                    "🔢 <b>أرسل الرقم الجديد فقط:</b>\nمثال: <code>5</code>",
                    reply_markup=self.setmax_keyboard(),
                )

        elif text == "/restart":
            self.restart_bot(state_manager, session=session, offset=offset)

        elif text.startswith("/analyze"):
            parts = text.split()
            if len(parts) == 2:
                sym = self.normalize_symbol(parts[1])
                threading.Thread(
                    target=self.handle_analyze_request,
                    args=(sym, client, state_manager, ai_engine, ta_engine, news_engine, social_engine),
                    daemon=True,
                ).start()
            else:
                self.user_state[user_key] = "awaiting_analyze"
                self.send_message(
                    "🔍 <b>اختر عملة من الأزرار أو أرسل رمز العملة:</b>\nمثال: <code>BTC</code> أو <code>ETH</code>",
                    reply_markup=self.analyze_keyboard(),
                )

        elif text in ["/candles", "/getcandles", "/getlogs"]:
            self.send_today_candle_logs()

        # ── /set_risk ─────────────────────────────────────────────
        elif text.startswith("/set_risk"):
            parts = text.split()
            if len(parts) != 2:
                self.send_message(
                    "⚠️ الاستخدام الصحيح:\n"
                    "<code>/set_risk [نسبة]</code>\n"
                    "مثال: <code>/set_risk 0.25</code> (حد خسارة الوقف كنسبة من الرصيد)\n"
                    f"النطاق المسموح: {Config.MIN_RISK_PER_TRADE_PERCENT} — "
                    f"{Config.HARD_MAX_RISK_PER_TRADE_PERCENT}"
                )
            else:
                try:
                    pct = float(parts[1])
                    if not (
                        Config.MIN_RISK_PER_TRADE_PERCENT
                        <= pct
                        <= Config.HARD_MAX_RISK_PER_TRADE_PERCENT
                    ):
                        self.send_message(
                            f"❌ نسبة المخاطرة يجب أن تكون بين "
                            f"{Config.MIN_RISK_PER_TRADE_PERCENT} و"
                            f"{Config.HARD_MAX_RISK_PER_TRADE_PERCENT}."
                        )
                    else:
                        old = state_manager.get_risk_pct()
                        if not state_manager.set_risk_pct(pct):
                            self.send_message("❌ تعذر حفظ نسبة المخاطرة بأمان.")
                            return
                        self._record_config_change(state_manager, "risk_percent", old, pct)
                        self.send_message(
                            f"✅ <b>تم تحديث نسبة المخاطرة</b>\n"
                            f"القيمة الجديدة: <code>{pct}%</code> مخاطرة وقف أساسية من الرصيد.\n"
                            f"هامش الدخول المستهدف مستقل ويبقى: "
                            f"<code>{Config.ENTRY_MARGIN_PERCENT}%</code>.\n"
                            f"⚠️ سيُطبَّق على الصفقات الجديدة فقط.",
                            reply_markup=self.main_menu_keyboard()
                        )
                        logger.info(f"[Telegram] تم تعيين نسبة المخاطرة إلى {pct}%")
                except ValueError:
                    self.send_message("❌ القيمة غير صالحة. أدخل رقماً مثل: <code>/set_risk 2</code>")

        # ── /set_leverage ──────────────────────────────────────────
        elif text.startswith("/set_leverage"):
            parts = text.split()
            if len(parts) != 2:
                self.send_message(
                    "⚠️ الاستخدام الصحيح:\n"
                    "<code>/set_leverage [رافعة]</code>\n"
                    "مثال: <code>/set_leverage 10</code>\n"
                    f"النطاق المسموح: {Config.MIN_LEVERAGE} — {Config.MAX_LEVERAGE}"
                )
            else:
                try:
                    lev = int(parts[1])
                    if not (Config.MIN_LEVERAGE <= lev <= Config.MAX_LEVERAGE):
                        self.send_message(
                            f"❌ الرافعة يجب أن تكون بين {Config.MIN_LEVERAGE} "
                            f"و {Config.MAX_LEVERAGE}."
                        )
                    else:
                        old = state_manager.get_leverage()
                        if not state_manager.set_leverage(lev):
                            self.send_message("❌ تعذر حفظ الرافعة بأمان.")
                            return
                        self._record_config_change(state_manager, "leverage", old, lev)
                        self.send_message(
                            f"✅ <b>تم تحديث الرافعة المالية</b>\n"
                            f"القيمة الجديدة: <code>{lev}x</code>\n"
                            f"⚠️ سيُطبَّق على الصفقات الجديدة فقط.",
                            reply_markup=self.main_menu_keyboard()
                        )
                        logger.info(f"[Telegram] تم تعيين الرافعة إلى {lev}x")
                except ValueError:
                    self.send_message("❌ القيمة غير صالحة. أدخل رقماً صحيحاً مثل: <code>/set_leverage 10</code>")

        # ── /blacklist ─────────────────────────────────────────────
        elif text.startswith("/blacklist"):
            parts = text.split()
            if len(parts) != 2:
                self.send_message(
                    "⚠️ الاستخدام الصحيح:\n"
                    "<code>/blacklist [رمز_العملة]</code>\n"
                    "مثال: <code>/blacklist BTCUSDT</code>\n"
                    "لإزالتها لاحقاً: <code>/whitelist BTCUSDT</code>"
                )
            else:
                raw_sym  = parts[1]
                symbol   = self.normalize_symbol(raw_sym)
                old = state_manager.get_blacklisted_pairs()
                state_manager.blacklist_pair(symbol)
                self._record_config_change(
                    state_manager, "blacklisted_pairs", old,
                    state_manager.get_blacklisted_pairs(),
                )
                self.send_message(
                    f"🚫 <b>تم إضافة العملة للقائمة السوداء</b>\n"
                    f"العملة: <code>{self._safe_html(symbol)}</code>\n"
                    f"لن يفتح البوت صفقات عليها حتى إزالتها.\n"
                    f"للإزالة: <code>/whitelist {self._safe_html(raw_sym)}</code>",
                    reply_markup=self.main_menu_keyboard()
                )
                logger.info(f"[Telegram] تم إضافة {symbol} للقائمة السوداء")

        # ── /whitelist ─────────────────────────────────────────────
        elif text.startswith("/whitelist"):
            parts = text.split()
            if len(parts) != 2:
                self.send_message(
                    "⚠️ الاستخدام الصحيح:\n"
                    "<code>/whitelist [رمز_العملة]</code>\n"
                    "مثال: <code>/whitelist BTCUSDT</code>"
                )
            else:
                symbol  = self.normalize_symbol(parts[1])
                bl_list = state_manager.get_blacklisted_pairs()
                if symbol not in bl_list:
                    self.send_message(
                        f"ℹ️ العملة <code>{self._safe_html(symbol)}</code> "
                        f"لم تكن في القائمة السوداء أصلاً."
                    )
                else:
                    old = list(bl_list)
                    state_manager.whitelist_pair(symbol)
                    self._record_config_change(
                        state_manager, "blacklisted_pairs", old,
                        state_manager.get_blacklisted_pairs(),
                    )
                    self.send_message(
                        f"✅ <b>تم إزالة العملة من القائمة السوداء</b>\n"
                        f"العملة: <code>{self._safe_html(symbol)}</code>\n"
                        f"البوت الآن يمكنه التداول عليها من جديد.",
                        reply_markup=self.main_menu_keyboard()
                    )
                    logger.info(f"[Telegram] تم إزالة {symbol} من القائمة السوداء")

        else:
            self.send_message(
                "لم أفهم الأمر. افتح القائمة الرئيسية واختر من الأزرار 👇",
                reply_markup=self.main_menu_keyboard(),
            )


    def handle_callback_query(
        self,
        callback,
        client,
        state_manager,
        ai_engine,
        ta_engine,
        news_engine,
        social_engine,
        session=None,
        offset=None,
    ):
        """
        معالجة ضغطات الأزرار GUI.
        """
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if str(chat_id) != str(self.chat_id):
            self.answer_callback(callback_id, "غير مصرح لك باستخدام هذا البوت.", show_alert=True)
            return
        storage_actions = {
            "storage_status",
            "storage_cleanup_request",
            "storage_cleanup_confirm",
            "storage_fresh_test_request",
            "storage_fresh_test_confirm",
            "start_72h_test_session",
            "export_72h_test_bundle",
        }
        if data in storage_actions and not self._storage_admin_user_id(callback):
            logger.warning("[Storage] رُفض طلب إدارة التخزين من مستخدم غير مصرح أو دون TELEGRAM_WEBAPP_USER_ID.")
            self.answer_callback(
                callback_id,
                "إدارة التخزين تتطلب TELEGRAM_WEBAPP_USER_ID مصرحاً وصريحاً.",
                show_alert=True,
            )
            return
        if not self._authorized_actor_user_id((callback.get("from") or {}).get("id")):
            logger.warning(
                "[Telegram] رُفض callback من مستخدم غير مصرح داخل chat مسموح."
            )
            self.answer_callback(
                callback_id, "غير مصرح لك باستخدام هذا البوت.", show_alert=True
            )
            return

        self.answer_callback(callback_id)

        try:
            if data == "gui_home":
                self.edit_message(chat_id, message_id, self.welcome_text(), self.main_menu_keyboard())

            elif data == "gui_reports_menu":

                self.edit_message(
                    chat_id,
                    message_id,
                    "📋 <b>قائمة التقارير والأداء:</b>\nاختر نوع التقرير الذي تريد عرضه:",
                    self.reports_menu_keyboard()
                )

            elif data in {
                "export_runtime_logs_1h",
                "export_runtime_logs_24h",
                "export_runtime_logs_48h",
                "export_runtime_logs_72h",
            }:
                hours = int(data.rsplit("_", 1)[-1].removesuffix("h"))
                self.edit_message(
                    chat_id,
                    message_id,
                    f"⏳ جاري تجهيز سجل تشغيل البوت لآخر {hours} ساعة...",
                    self.back_home_keyboard(),
                )
                threading.Thread(
                    target=self.send_runtime_logs,
                    args=(hours,),
                    name=f"runtime-log-export-{hours}h",
                    daemon=True,
                ).start()

            elif data == "export_runtime_logs_custom_minutes":
                self.user_state[str(chat_id)] = "awaiting_runtime_log_minutes"
                self.edit_message(
                    chat_id,
                    message_id,
                    "⏱️ <b>أرسل المدة المطلوبة بالدقائق الآن:</b>\n"
                    "مثال: <code>30</code> لتنزيل سجل آخر 30 دقيقة.\n"
                    "الحد المسموح: من دقيقة واحدة إلى 10080 دقيقة.",
                    self.back_home_keyboard(),
                )

            elif data == "hot_list_shadow":
                from utils.hot_list_monitor import get_latest_hot_list_snapshot

                snapshot = get_latest_hot_list_snapshot()
                status = str(snapshot.get("status") or "NOT_STARTED")
                timestamp = escape(str(snapshot.get("timestamp") or "لم يبدأ بعد"))
                candidates = snapshot.get("candidates") or []
                tag_labels = {
                    "NEW_CONTRACT": "عقد جديد",
                    "MOMENTUM_UP": "تسارع صاعد",
                    "MOMENTUM_DOWN": "تسارع هابط",
                    "LOW_RECLAIM_WATCH": "مراقبة ارتداد من قرب القاع",
                    "HIGH_REJECTION_WATCH": "مراقبة رفض من قرب القمة",
                }
                lines = [
                    "🔥 <b>القائمة الساخنة — مرصد تجريبي</b>",
                    "━━━━━━━━━━━━━━",
                    "⚠️ هذه مراقبة فقط؛ لا تُنشئ صفقة ولا تتجاوز فلاتر البوت.",
                    f"الحالة: <code>{escape(status)}</code>",
                    f"آخر تحديث UTC: <code>{timestamp}</code>",
                    f"عقود السوق المرصودة: <b>{int(snapshot.get('total_market_symbols') or 0)}</b>",
                    f"المرشحات الساخنة: <b>{len(candidates)}</b>",
                ]
                if not candidates:
                    lines.append("\nلا توجد حركة تستوفي عتبات المرصد حالياً.")
                else:
                    lines.append("\n<b>أعلى المرشحات:</b>")
                    for item in candidates[:12]:
                        labels = "، ".join(
                            tag_labels.get(tag, tag) for tag in item.get("tags", [])
                        )
                        lines.append(
                            f"• <b>{escape(str(item.get('symbol', '')))}</b> — {escape(labels)}\n"
                            f"  1m {float(item.get('change_1m_pct') or 0):+.2f}% | "
                            f"5m {float(item.get('change_5m_pct') or 0):+.2f}% | "
                            f"موضع نطاق 24h: {float(item.get('range_position_24h_pct') or 0):.0f}%"
                        )
                self.edit_message(
                    chat_id,
                    message_id,
                    "\n".join(lines),
                    self.reports_menu_keyboard(),
                )

            elif data == "start_72h_test_session":
                from utils.test_session import start_test_session

                test = start_test_session(state_manager, source="TELEGRAM")
                if test.get("already_running"):
                    title = "ℹ️ اختبار 72 ساعة قيد التشغيل أصلاً."
                else:
                    title = "✅ بدأ اختبار 72 ساعة."
                self.edit_message(
                    chat_id,
                    message_id,
                    f"{title}\n\n"
                    f"Test ID: <code>{escape(str(test.get('test_id')))}</code>\n"
                    f"البداية UTC: <code>{escape(str(test.get('started_at')))}</code>\n"
                    f"Config hash: <code>{escape(str(test.get('configuration_hash', ''))[:16])}</code>\n\n"
                    "أي تغيير مؤثر في الإعدادات أو الكود سيظهر كـ <code>INTERRUPTED</code>.",
                    self.reports_menu_keyboard(),
                )

            elif data == "export_72h_test_bundle":
                self.edit_message(
                    chat_id,
                    message_id,
                    "⏳ جاري بناء حزمة الأدلة لاختبار 72 ساعة...",
                    self.back_home_keyboard(),
                )
                threading.Thread(
                    target=self.send_72h_evidence_bundle,
                    args=(state_manager,),
                    name="72h-evidence-export",
                    daemon=True,
                ).start()

            elif data == "storage_status":
                from utils.storage_manager import get_storage_breakdown, get_storage_usage
                try:
                    usage = get_storage_usage()
                    breakdown = get_storage_breakdown()
                    self.edit_message(
                        chat_id,
                        message_id,
                        self._format_storage_usage(usage, breakdown),
                        self.reports_menu_keyboard(),
                    )
                except Exception as exc:
                    logger.error(f"[Storage] تعذر قراءة مساحة التخزين: {exc}")
                    self.edit_message(
                        chat_id,
                        message_id,
                        "⚠️ تعذر قراءة مساحة التخزين الدائم.",
                        self.reports_menu_keyboard(),
                    )

            elif data == "storage_cleanup_request":
                archive_age = os.getenv("BOT_STORAGE_ARCHIVE_MIN_AGE_HOURS", "96")
                self.edit_message(
                    chat_id,
                    message_id,
                    "🧹 <b>تأكيد التنظيف الآمن</b>\n"
                    "سيُحذف فقط:\n"
                    f"• أرشيف سجلات البوت الأقدم من <code>{escape(archive_age)}</code> ساعة.\n"
                    "• ملفات ZIP المؤقتة القديمة ذات الأسماء المعروفة.\n\n"
                    "لن تُحذف قاعدة البيانات، ملف الحالة، السجل النشط، أو أي أرشيف داخل نافذة اختبار 72 ساعة.",
                    self.storage_cleanup_confirm_keyboard(),
                )

            elif data == "storage_cleanup_confirm":
                self.edit_message(
                    chat_id,
                    message_id,
                    "⏳ جاري تنظيف الملفات المؤرشفة القديمة ضمن القائمة الآمنة...",
                    self.back_home_keyboard(),
                )
                threading.Thread(
                    target=self.run_safe_storage_cleanup,
                    name="safe-storage-cleanup",
                    daemon=True,
                ).start()

            elif data == "storage_fresh_test_request":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🧽 <b>تهيئة سجلات اختبار جديد</b>\n\n"
                    "سيتم حذف جميع أرشيفات السجلات وملفات ZIP المؤقتة الحالية، "
                    "وتصفير سجلي النشاط والقرارات النشطين بطريقة آمنة.\n\n"
                    "لن تُحذف قاعدة البيانات أو حالة البوت أو Journal. "
                    "سيرفض البوت التنفيذ إذا وجدت صفقة متتبعة أو جلسة 72 ساعة نشطة.\n\n"
                    "⚠️ نفّذ هذا مرة واحدة فقط قبل بدء الاختبار الجديد.",
                    self.storage_fresh_test_confirm_keyboard(),
                )

            elif data == "storage_fresh_test_confirm":
                self.edit_message(
                    chat_id,
                    message_id,
                    "⏳ جاري تهيئة السجلات مع الحفاظ على بيانات التداول...",
                    self.back_home_keyboard(),
                )
                threading.Thread(
                    target=self.run_fresh_test_storage_cleanup,
                    args=(state_manager,),
                    name="fresh-test-storage-reset",
                    daemon=True,
                ).start()

            elif data == "gui_version":
                commit_display = GIT_COMMIT or "غير متاح — النشر ليس من GitHub"
                branch_display = GIT_BRANCH or "غير متاح"
                repo_display = GIT_REPO or "غير متاح"
                deployment_display = DEPLOYMENT_ID[:12] if DEPLOYMENT_ID else "غير متاح"

                version_text = (
                    f"🏷️ <b>إصدار البوت العامل الآن</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"الإصدار: <code>{escape(APP_VERSION)}</code>\n"
                    f"Commit المختصر: <code>{escape(SHORT_COMMIT)}</code>\n"
                    f"Commit الكامل: <code>{escape(commit_display)}</code>\n"
                    f"الفرع: <code>{escape(branch_display)}</code>\n"
                    f"المستودع: <code>{escape(repo_display)}</code>\n"
                    f"Deployment: <code>{escape(deployment_display)}</code>\n\n"
                    f"✅ الإصلاحات التسعة تبدأ من commit:\n"
                    f"<code>99321e146b877408f85180b38aca27028c343605</code>"
                )
                self.edit_message(
                    chat_id,
                    message_id,
                    version_text,
                    self.back_home_keyboard(),
                )

            elif data == "gui_help":
                self.edit_message(chat_id, message_id, self.help_text(), self.main_menu_keyboard())

            elif data == "gui_status":
                try:
                    msg = self.build_status_message(client, state_manager)
                    self.edit_message(chat_id, message_id, msg, self.back_home_keyboard())
                except Exception as e:
                    logger.error(f"خطأ في تحديث الحالة من الزر: {e}")
                    self.edit_message(
                        chat_id,
                        message_id,
                        "⚠️ تعذر تحديث حالة البوت حالياً.",
                        self.main_menu_keyboard(),
                    )

            elif data in ["report_daily", "report_weekly", "report_monthly", "report_yearly"]:
                period = data.replace("report_", "")
                tracker = PerformanceTracker(state_manager)
                text = tracker.format_report(period, client=client)

                self.edit_message(
                    chat_id,
                    message_id,
                    text,
                    reply_markup=self.back_home_keyboard()
                )
                self.answer_callback(callback_id, "تم إنشاء التقرير ✅")

            elif data == "gui_journal_menu":
                self.edit_message(
                    chat_id, message_id,
                    "📓 <b>مراجعة الصفقات — Journal</b>\n"
                    "اختر نوع المراجعة:\n\n"
                    "• <b>آخر 5 صفقات</b> — تحليل روائي لآخر 5 صفقات مكتملة\n"
                    "• <b>آخر 24 ساعة</b> — كل الصفقات في آخر يوم\n"
                    "• <b>تقييم عملة</b> — اكتب اسم العملة لتقييم صفقاتها",
                    self.journal_menu_keyboard()
                )
                self.answer_callback(callback_id)

            elif data == "journal_last5":
                self.answer_callback(callback_id, "⏳ جاري التحليل...")
                threading.Thread(
                    target=lambda: self._send_journal_report(
                        chat_id=chat_id, mode="last5", client=client
                    ), daemon=True
                ).start()

            elif data == "journal_24h":
                self.answer_callback(callback_id, "⏳ جاري التحليل...")
                threading.Thread(
                    target=lambda: self._send_journal_report(
                        chat_id=chat_id, mode="24h", client=client
                    ), daemon=True
                ).start()

            elif data == "journal_pick_coin":
                user_key = str(chat_id)
                self.user_state[user_key] = "awaiting_journal_coin"
                self.edit_message(
                    chat_id, message_id,
                    "🔍 <b>تقييم عملة محددة</b>\n"
                    "أرسل اسم العملة كرسالة نصية، مثال:\n"
                    "<code>EIGEN</code> أو <code>PLUME</code> أو <code>BNB</code>",
                    self.journal_menu_keyboard()
                )
                self.answer_callback(callback_id)

            elif data == "download_candle_logs":
                self.send_today_candle_logs()
                self.answer_callback(callback_id, "تم إرسال الملف ✅")

            elif data == "gui_candles_menu":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🕯️ <b>سجلات الشموع والصفقات:</b>\nاختر خيار التحميل المناسب لك:",
                    self.candles_menu_keyboard()
                )
                self.answer_callback(callback_id)

            elif data == "candles_dl_24h":
                self.send_journal_candles_by_time(24)
                self.answer_callback(callback_id, "تم إرسال ملف الـ 24 ساعة ✅")

            elif data == "candles_dl_1h":
                self.send_journal_candles_by_time(1)
                self.answer_callback(callback_id, "تم إرسال ملف الساعة الأخيرة ✅")

            elif data == "candles_filter_single":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔍 <b>اختر العملة المراد تحميل سجلاتها (آخر 24 ساعة):</b>",
                    self.build_candles_single_filter_keyboard()
                )
                self.answer_callback(callback_id)

            elif data == "candles_filter_multi":
                self.selected_candles_state[chat_id] = set()
                self.edit_message(
                    chat_id,
                    message_id,
                    "🗂️ <b>اختر العملات المطلوبة ثم اضغط تحميل:</b>",
                    self.build_candles_multi_filter_keyboard(chat_id)
                )
                self.answer_callback(callback_id)

            elif data.startswith("c_dl_single_"):
                symbol = data.replace("c_dl_single_", "", 1)
                self.send_journal_candles_by_time(24, [symbol])
                self.answer_callback(callback_id, f"جاري إرسال ملف {symbol} ✅")

            elif data.startswith("c_tgl_"):
                symbol = data.replace("c_tgl_", "", 1)
                if chat_id not in self.selected_candles_state:
                    self.selected_candles_state[chat_id] = set()
                
                if symbol in self.selected_candles_state[chat_id]:
                    self.selected_candles_state[chat_id].remove(symbol)
                else:
                    self.selected_candles_state[chat_id].add(symbol)
                
                self.edit_message(
                    chat_id,
                    message_id,
                    "🗂️ <b>اختر العملات المطلوبة ثم اضغط تحميل:</b>",
                    self.build_candles_multi_filter_keyboard(chat_id)
                )
                self.answer_callback(callback_id)

            elif data == "c_dl_multi_confirm":
                selected = self.selected_candles_state.get(chat_id, set())
                if not selected:
                    self.answer_callback(callback_id, "⚠️ الرجاء اختيار عملة واحدة على الأقل!", show_alert=True)
                else:
                    self.send_journal_candles_by_time(24, list(selected))
                    self.selected_candles_state[chat_id] = set()
                    self.edit_message(
                        chat_id,
                        message_id,
                        "🕯️ <b>سجلات الشموع والصفقات:</b>\nاختر خيار التحميل المناسب لك:",
                        self.candles_menu_keyboard()
                    )
                    self.answer_callback(callback_id, "جاري إعداد الملف المجمع ✅")

            elif data == "c_multi_clear":
                self.selected_candles_state[chat_id] = set()
                self.edit_message(
                    chat_id,
                    message_id,
                    "🗂️ <b>اختر العملات المطلوبة ثم اضغط تحميل:</b>",
                    self.build_candles_multi_filter_keyboard(chat_id)
                )
                self.answer_callback(callback_id, "تم تصفية الاختيارات 🧹")

            elif data == "gui_health":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🩺 <b>جاري تجهيز تقرير صحة النظام...</b>",
                    self.main_menu_keyboard(),
                )
                self.run_health_report(client, state_manager)

            elif data == "gui_api_status":
                self.answer_callback(callback_id, "بدأ فحص الخدمات...")

                self.send_message(
                    "🧪 جاري فحص كل خدمات البوت...\n"
                    "قد يستغرق الفحص 20 إلى 60 ثانية. سأرسل النتيجة هنا عند الانتهاء."
                )

                def _run():
                    try:
                        monitor = APIStatusMonitor(
                            client=client,
                            state_manager=state_manager,
                            notifier=self
                        )
                        payload = monitor.run_full_check()
                        text = monitor.format_report(payload)
                        monitor.notify_if_needed(payload)
                        self.send_message(text, reply_markup=self.back_home_keyboard())
                    except Exception as e:
                        logger.error(f"فشل فحص الخدمات من تيليجرام: {e}")
                        self.send_message(
                            f"⚠️ فشل فحص الخدمات:\n{self._safe_html(e)}",
                            reply_markup=self.back_home_keyboard()
                        )

                threading.Thread(target=_run, daemon=True).start()
                return

            elif data == "gui_analyze":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🧠 <b>اختر العملة التي تريد تحليلها:</b>\nأو اضغط إدخال يدوي.",
                    self.analyze_keyboard(),
                )

            elif data == "gui_analyze_manual":
                self.user_state[str(chat_id)] = "awaiting_analyze"
                self.edit_message(
                    chat_id,
                    message_id,
                    "✍️ <b>أرسل رمز العملة الآن:</b>\nمثال: <code>BTC</code> أو <code>ETH</code>",
                    self.main_menu_keyboard(),
                )

            elif data.startswith("analyze_"):
                raw_symbol = data.replace("analyze_", "", 1)
                sym = self.normalize_symbol(raw_symbol)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"⏳ جاري تحليل <b>{self._safe_html(sym)}</b>...\nسيصل التقرير هنا برسالة مستقلة.",
                    self.main_menu_keyboard(),
                )
                threading.Thread(
                    target=self.handle_analyze_request,
                    args=(sym, client, state_manager, ai_engine, ta_engine, news_engine, social_engine),
                    daemon=True,
                ).start()

            elif data == "gui_demo_signal_mode":
                current = state_manager.get_demo_signal_execution_mode()
                label = (
                    "⚡ تنفيذ كل قرار HYBRID BUY/SELL على Demo"
                    if current == "hybrid_decision"
                    else "🔒 الوضع الصارم السابق بعد جميع الفلاتر"
                )
                self.edit_message(
                    chat_id,
                    message_id,
                    "🎯 <b>وضع تنفيذ إشارات Demo</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"الوضع الحالي: <b>{label}</b>\n\n"
                    "⚡ <b>كل BUY/SELL:</b> يتجاوز Quality وRR الأدنى "
                    "وتأكيد 5m، مع بقاء الوقف والمخاطرة وحماية Algo ومنع التكرار.\n\n"
                    "🔒 <b>الصارم:</b> يعيد السلوك السابق، ولا ينفذ إلا "
                    "Proposal اجتاز جميع الفلاتر.",
                    self.demo_signal_mode_keyboard(state_manager),
                )
                return

            elif data.startswith("demo_signal_mode_"):
                new_mode = data.replace("demo_signal_mode_", "", 1)
                old_mode = state_manager.get_demo_signal_execution_mode()
                if not state_manager.set_demo_signal_execution_mode(new_mode):
                    self.edit_message(
                        chat_id,
                        message_id,
                        "❌ تعذر حفظ وضع تنفيذ الإشارات.",
                        self.main_menu_keyboard(),
                    )
                    return
                self._record_config_change(
                    state_manager,
                    "demo_signal_execution_mode",
                    old_mode,
                    new_mode,
                )
                new_label = (
                    "⚡ تنفيذ كل قرار HYBRID BUY/SELL على Demo"
                    if new_mode == "hybrid_decision"
                    else "🔒 الوضع الصارم السابق"
                )
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ تم تفعيل: <b>{new_label}</b>\n"
                    "سيُطبّق من قرار الإشارة التالي.",
                    self.main_menu_keyboard(),
                )
                logger.warning(
                    "[DemoSignalMode] تغير الوضع عبر Telegram: "
                    f"{old_mode} -> {new_mode}"
                )
                return

            elif data == "gui_setmax":
                current_max = state_manager.get_max_open_trades()
                self.edit_message(
                    chat_id,
                    message_id,
                    f"⚙️ <b>الحد الأقصى الحالي للصفقات:</b> <code>{current_max}</code>\n\nاختر رقم من الأزرار أو إدخال يدوي:",
                    self.setmax_keyboard(),
                )

            elif data == "gui_setmax_manual":
                self.user_state[str(chat_id)] = "awaiting_setmax"
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔢 <b>أرسل الرقم الجديد فقط:</b>\nمثال: <code>5</code>",
                    self.main_menu_keyboard(),
                )

            elif data.startswith("setmax_"):
                new_max = int(data.replace("setmax_", "", 1))
                old = state_manager.get_max_open_trades()
                if not state_manager.set_max_open_trades(new_max):
                    self.edit_message(
                        chat_id, message_id,
                        f"❌ الحد المسموح هو 1—{Config.HARD_MAX_OPEN_TRADES}.",
                        self.main_menu_keyboard(),
                    )
                    return
                self._record_config_change(state_manager, "max_open_trades", old, new_max)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ تم تغيير الحد الأقصى للصفقات المفتوحة إلى: <b>{new_max}</b> صفقات.",
                    self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير الحد الأقصى للصفقات المفتوحة إلى {new_max} عبر GUI تيليجرام.")

            elif data == "gui_leverage":
                current_lev = state_manager.get_leverage()
                self.edit_message(
                    chat_id,
                    message_id,
                    f"⚙️ <b>الرافعة الحالية:</b> <code>{current_lev}x</code>\n\nاختر رقم من الأزرار أو إدخال يدوي:",
                    self.leverage_keyboard(),
                )

            elif data == "gui_setlev_manual":
                self.user_state[str(chat_id)] = "awaiting_setlev"
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔢 <b>أرسل رقم الرافعة فقط:</b>\nمثال: <code>10</code>",
                    self.main_menu_keyboard(),
                )

            elif data.startswith("setlev_"):
                new_lev = int(data.replace("setlev_", "", 1))
                min_lev = int(getattr(Config, "MIN_LEVERAGE", 1))
                max_lev = int(getattr(Config, "MAX_LEVERAGE", 20))
                if not (min_lev <= new_lev <= max_lev):
                    self.edit_message(
                        chat_id, message_id,
                        f"❌ الرافعة يجب أن تكون بين {min_lev} و{max_lev}x.",
                        self.main_menu_keyboard(),
                    )
                    return
                old = state_manager.get_leverage()
                if not state_manager.set_leverage(new_lev):
                    self.edit_message(
                        chat_id, message_id, "❌ تعذر حفظ الرافعة بأمان.",
                        self.main_menu_keyboard(),
                    )
                    return
                self._record_config_change(state_manager, "leverage", old, new_lev)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ تم تغيير الرافعة إلى: <b>{new_lev}x</b>.",
                    self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير الرافعة إلى {new_lev}x عبر GUI تيليجرام.")

            elif data == "gui_ref_balance":
                current_ref = state_manager.get("reference_balance", getattr(Config, "REFERENCE_BALANCE", 60.0))
                self.edit_message(
                    chat_id,
                    message_id,
                    f"💰 <b>محفظة محاكاة التقارير الحالية:</b> <code>${current_ref:.2f} USDT</code>\n\n"
                    "هذه القيمة للعرض النسبي فقط ولا تحد حجم الصفقة. "
                    "حجم المخاطرة يعتمد على Equity الفعلي في Binance.\n\n"
                    f"اختر قيمة المحاكاة أو إدخال يدوي:",
                    self.ref_balance_keyboard(state_manager),
                )

            elif data == "gui_setref_manual":
                self.user_state[str(chat_id)] = "awaiting_setref"
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔢 <b>أرسل القيمة المرجعية الجديدة بالدولار:</b>\nمثال: <code>75.50</code>",
                    self.main_menu_keyboard(),
                )

            elif data.startswith("setref_"):
                new_ref = float(data.replace("setref_", "", 1))
                state_manager.set("reference_balance", new_ref)
                Config.REFERENCE_BALANCE = new_ref
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ تم تغيير المحفظة المرجعية إلى: <b>${new_ref:.2f} USDT</b>.",
                    self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير المحفظة المرجعية إلى {new_ref} عبر GUI تيليجرام.")

            elif data == "gui_risk":
                current_risk = state_manager.get_risk_pct()
                self.edit_message(
                    chat_id,
                    message_id,
                    f"🛡️ <b>مخاطرة الوقف الأساسية:</b> <code>{current_risk:.2f}%</code>\n"
                    f"💵 <b>هامش الدخول المستهدف:</b> <code>{Config.ENTRY_MARGIN_PERCENT:.2f}%</code>\n"
                    f"🚨 <b>سقف المخاطرة الصلب:</b> "
                    f"<code>{Config.HARD_MAX_RISK_PER_TRADE_PERCENT:.2f}%</code>\n\n"
                    "هذه القيم مستقلة: تغيير مخاطرة الوقف لا يغيّر هامش الدخول.\n\n"
                    f"اختر النسبة المطلوبة:",
                    self.risk_keyboard(state_manager),
                )

            elif data.startswith("setrisk_"):
                new_risk = float(data.replace("setrisk_", "", 1))
                if not (
                    Config.MIN_RISK_PER_TRADE_PERCENT
                    <= new_risk
                    <= Config.HARD_MAX_RISK_PER_TRADE_PERCENT
                ):
                    self.edit_message(
                        chat_id, message_id, "❌ نسبة المخاطرة خارج الحد الصلب.",
                        self.main_menu_keyboard(),
                    )
                    return
                old = state_manager.get_risk_pct()
                if not state_manager.set_risk_pct(new_risk):
                    self.edit_message(
                        chat_id, message_id, "❌ تعذر حفظ نسبة المخاطرة بأمان.",
                        self.main_menu_keyboard(),
                    )
                    return
                self._record_config_change(state_manager, "risk_percent", old, new_risk)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ تم تغيير مخاطرة الوقف الأساسية إلى: <b>{new_risk}%</b>.\n"
                    f"هامش الدخول المستهدف ما زال <b>{Config.ENTRY_MARGIN_PERCENT}%</b>.",
                    self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير نسبة المخاطرة إلى {new_risk}% عبر GUI تيليجرام.")

            # ── 🎛️ مركز الفلاتر ──────────────────────────────────────
            elif data == "gui_filters_center":
                # الشاشة الرئيسية لمركز الفلاتر
                profile = get_filter_profile(state_manager)
                sentiment_mode = state_manager.get("sentiment_mode", "full")
                if not getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False):
                    sentiment_mode = "ta_only"
                ema_mode_val   = state_manager.get("ema_mode", "dual")
                sentiment_labels = {"full": "🧠 كامل (شرط)", "weight": "📉 ثانوي", "ta_only": "📊 معطّل"}
                ema_labels     = {"dual": "📊 ثنائي (50/200)", "triple": "📈 ثلاثي (50/100/200)"}
                text = (
                    f"🎛️ <b>مركز الفلاتر والشروط</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🔰 <b>البروفايل الحالي:</b> {profile['label']}\n"
                    f"📈 رصد المرشحات: <code>{profile.get('opportunity_pct', '?')}</code>\n"
                    f"🧠 المشاعر: <code>{sentiment_labels.get(sentiment_mode, sentiment_mode)}</code>\n"
                    f"📈 المتوسطات: <code>{ema_labels.get(ema_mode_val, ema_mode_val)}</code>\n\n"
                    f"اختر ما تريد تعديله:"
                )
                self.edit_message(chat_id, message_id, text, self.filters_center_keyboard())
                self.answer_callback(callback_id, "🎛️ مركز الفلاتر")
                return

            elif data in ("gui_filter_profile", "gui_filter_profile_list"):
                # عرض قائمة البروفايلات الست
                status_text = format_filter_profile_status(state_manager)
                self.edit_message(
                    chat_id, message_id,
                    status_text + "\n\nاضغط على أي بروفايل لعرض تفاصيله ومقارنته:",
                    self.filter_profile_list_keyboard(state_manager),
                )
                self.answer_callback(callback_id, "🔰 اختر بروفايل")
                return

            elif data.startswith("filterprofile_preview_"):
                # عرض تفاصيل البروفايل مع زرين (تطبيق / رجوع)
                profile_name = data.replace("filterprofile_preview_", "", 1)
                detail_text  = format_profile_detail(profile_name)
                self.edit_message(
                    chat_id, message_id,
                    detail_text,
                    self.filter_profile_confirm_keyboard(profile_name),
                )
                self.answer_callback(callback_id, "📋 عرض التفاصيل")
                return

            elif data.startswith("filterprofile_confirm_"):
                # تطبيق البروفايل بعد تأكيد المستخدم
                profile_name = data.replace("filterprofile_confirm_", "", 1)
                old = get_filter_profile(state_manager).get("key")
                profile = set_filter_profile(state_manager, profile_name)
                self._record_config_change(state_manager, "filter_profile", old, profile_name)

                conditions_summary = []
                for c in profile.get("conditions_display", []):
                    status = "✅" if c["active"] else "❌"
                    conditions_summary.append(f"  {status} {c['term']} — {c['explain']}")

                self.edit_message(
                    chat_id, message_id,
                    f"✅ <b>تم تفعيل البروفايل:</b> {profile['label']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    + "\n".join(conditions_summary) +
                    f"\n\n📊 رصد المرشحات التقريبي: <code>{profile.get('opportunity_pct', '?')}</code>\n"
                    f"📆 سيطبّق من دورة المسح القادمة.",
                    self.main_menu_keyboard(),
                )
                logger.info(f"تم تغيير وضع الفلاتر من تيليجرام إلى: {profile_name}")
                self.answer_callback(callback_id, f"✅ {profile['label']} — جاهز")
                return

            # (للتوافق مع القيم القديمة strict/medium/relaxed إذا وُجدت في ال state)
            elif data.startswith("filterprofile_"):
                profile_name = data.replace("filterprofile_", "", 1)
                old = get_filter_profile(state_manager).get("key")
                profile = set_filter_profile(state_manager, profile_name)
                self._record_config_change(state_manager, "filter_profile", old, profile_name)
                self.edit_message(
                    chat_id, message_id,
                    f"✅ <b>تم تغيير وضع الفلاتر إلى:</b> {profile['label']}",
                    self.main_menu_keyboard(),
                )
                self.answer_callback(callback_id, f"✅ {profile['label']}")
                return

            elif data == "gui_sentiment_mode":
                current_mode = state_manager.get("sentiment_mode", "full")
                automation_enabled = getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False)
                if not automation_enabled:
                    current_mode = "ta_only"
                mode_labels = {
                    "full":    "🧠 كامل — المشاعر شرط لاتخاذ القرار",
                    "weight":  "📉 ثانوي — يعدّل حجم الصفقة فقط",
                    "ta_only": "📊 معطّل — يعمل على المؤشرات الفنية فقط",
                }
                current_label = mode_labels.get(current_mode, current_mode)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"🧠 <b>وضع تأثير المشاعر (Sentiment)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"الوضع الحالي: <code>{current_label}</code>\n"
                    f"حالة التكامل الآلي: <code>{'مفعّل' if automation_enabled else 'معطّل افتراضياً لعدم ثبوت فائدته'}</code>\n\n"
                    f"<b>شرح الأوضاع:</b>\n"
                    f"• 🧠 <b>كامل:</b> المشاعر شريك في القرار — لن يدخل صفقة إلا إذا اتفق الفني مع المشاعر. الأكثر دقة لكن الأقل نشاطاً.\n\n"
                    f"• 📉 <b>ثانوي:</b> يدخل الصفقة بناءً على الفني، لكن إذا كانت المشاعر سلبية يقلّل حجم الصفقة تلقائياً (مثلاً 70% بدلاً من 100%). أفضل توازن.\n\n"
                    f"• 📊 <b>معطّل:</b> يتجاهل المشاعر كلياً ويعمل على المؤشرات الفنية (RSI, EMA, MACD) فقط. الأكثر نشاطاً والأسرع في الدخول.\n\n"
                    f"اختر الوضع المناسب:",
                    self.sentiment_mode_keyboard(),
                )
                self.answer_callback(callback_id, "اختر وضع المشاعر")
                return

            elif data.startswith("sentimentmode_"):
                new_mode = data.replace("sentimentmode_", "", 1)
                valid_modes = {"full", "weight", "ta_only"}
                if new_mode not in valid_modes:
                    self.answer_callback(callback_id, "وضع غير صالح")
                    return
                if (
                    new_mode != "ta_only"
                    and not getattr(Config, "AUTOMATED_SENTIMENT_ENABLED", False)
                ):
                    self.answer_callback(
                        callback_id,
                        "المشاعر الآلية معطلة من البيئة لأنها غير مثبتة الفائدة.",
                        show_alert=True,
                    )
                    return
                old = state_manager.get("sentiment_mode", "ta_only")
                state_manager.set("sentiment_mode", new_mode)
                self._record_config_change(state_manager, "sentiment_mode", old, new_mode)
                mode_descriptions = {
                    "full":    ("🧠 كامل (المشاعر شرط)", "البوت سيشترط توافق المشاعر مع الفني قبل الدخول. دقة عالية وصفقات أقل."),
                    "weight":  ("📉 ثانوي (يعدّل الحجم)", "البوت يدخل بناءً على الفني، لكن المشاعر السلبية تقلّل حجم الصفقة. توازن ممتاز."),
                    "ta_only": ("📊 معطّل (فني فقط)", "البوت يتجاهل المشاعر ويعمل بالمؤشرات الفنية فقط. أكثر نشاطاً."),
                }
                label, desc = mode_descriptions[new_mode]
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ <b>تم تفعيل وضع المشاعر:</b> {label}\n\n"
                    f"{desc}\n\n"
                    f"🧠 <b>ما يعني هذا:</b>\n"
                    f"• كامل: البوت لن يدخل صفقة إلا إذا اتفق التحليل الفني مع المشاعر.\n"
                    f"• ثانوي: يدخل بالفني، والمشاعر السلبية تقلّل حجم الصفقة فقط.\n"
                    f"• معطّل: يتجاهل المشاعر كلياً — أكثر نشاطاً وأسرع دخولاً.\n\n"
                    f"📆 سيطبّق من دورة المسح القادمة.",
                    self.filters_center_keyboard(),
                )
                logger.info(f"تم تغيير وضع المشاعر إلى: {new_mode}")
                self.answer_callback(callback_id, f"✅ وضع المشاعر: {label}")
                return

            elif data == "gui_ema_mode":
                current_mode = state_manager.get("ema_mode", "dual")
                mode_labels = {
                    "dual":   "📊 ثنائي (EMA 50, EMA 200) — الافتراضي",
                    "triple": "📈 ثلاثي (EMA 50, EMA 100, EMA 200)",
                }
                current_label = mode_labels.get(current_mode, current_mode)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"📈 <b>وضع تأكيد الاتجاه عبر المتوسطات الأسية (EMA Mode)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"الوضع الحالي: <code>{current_label}</code>\n\n"
                    f"<b>شرح الأوضاع:</b>\n"
                    f"• 📊 <b>ثنائي (EMA 50 / 200):</b> يعتمد على المتوسطين 50 و 200 فقط لتحديد صعود أو هبوط السوق. يوفر صفقات أكثر ونشاط أعلى.\n\n"
                    f"• 📈 <b>ثلاثي (EMA 50 / 100 / 200):</b> يشترط الترتيب المثالي للمتوسطات الثلاثة (50 > 100 > 200 للشراء، و 50 < 100 < 200 للبيع). يقلل الإشارات الخاطئة بشكل كبير ويضمن دخولاً آمناً مع الترند القوي، لكن يقلل عدد الصفقات.\n\n"
                    f"اختر الوضع المناسب لتطبيقه:",
                    self.ema_mode_keyboard(),
                )
                self.answer_callback(callback_id, "اختر وضع المتوسطات الأسية")
                return

            elif data.startswith("emamode_"):
                new_mode = data.replace("emamode_", "", 1)
                valid_modes = {"dual", "triple"}
                if new_mode not in valid_modes:
                    self.answer_callback(callback_id, "وضع غير صالح")
                    return
                old = state_manager.get("ema_mode", "dual")
                state_manager.set("ema_mode", new_mode)
                self._record_config_change(state_manager, "ema_mode", old, new_mode)
                mode_descriptions = {
                    "dual":   ("📊 ثنائي (EMA 50/200)", "البوت سيعمل بمتوسطين فقط لتحديد الترند. نشاط أكبر وصفقات أكثر."),
                    "triple": ("📈 ثلاثي (EMA 50/100/200)", "البوت سيشترط ترتيب المتوسطات الثلاثة بالكامل. أمان أعلى وصفقات أقل."),
                }
                label, desc = mode_descriptions[new_mode]
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ <b>تم تغيير وضع المتوسطات الأسية (EMA) إلى:</b>\n\n"
                    f"<b>{label}</b>\n\n"
                    f"{desc}\n\n"
                    f"📈 <b>ما يعني هذا:</b>\n"
                    f"• ثنائي (EMA 50/200): يعتمد على متوسطين — شرط السعر فوق EMA50 وEMA50 فوق EMA200 للشراء.\n"
                    f"• ثلاثي (50/100/200): يشترط ترتيب مثالي 50>100>200 للصعود — أكثر تحفظاً وصفقات أقل.\n\n"
                    f"📆 سيطبّق في دورات المسح القادمة.",
                    self.filters_center_keyboard(),
                )
                logger.info(f"تم تغيير وضع المتوسطات من تيليجرام إلى: {new_mode}")
                self.answer_callback(callback_id, f"✅ وضع المتوسطات: {label}")
                return

            elif data == "gui_scan_interval":
                current_sec = int(state_manager.get("scan_interval_seconds", 600))
                self.edit_message(
                    chat_id,
                    message_id,
                    f"⏱️ <b>فترة المسح الحالية:</b> {current_sec // 60} دقيقة\n\nاختر فترة المسح الجديدة:",
                    self.scan_interval_keyboard()
                )

            elif data == "gui_setscan_manual":
                self.user_state[str(chat_id)] = "awaiting_setscan"
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔢 <b>أرسل فترة المسح بالدقائق فقط:</b>\nمثال: <code>20</code>",
                    self.main_menu_keyboard()
                )

            elif data.startswith("setscan_"):
                new_interval_sec = int(data.replace("setscan_", "", 1))
                old = state_manager.get("scan_interval_seconds", 600)
                state_manager.set("scan_interval_seconds", new_interval_sec)
                self._record_config_change(state_manager, "scan_interval_seconds", old, new_interval_sec)
                self.edit_message(
                    chat_id,
                    message_id,
                    f"✅ تم تعيين فترة المسح بنجاح إلى {new_interval_sec // 60} دقيقة.",
                    self.main_menu_keyboard()
                )
                logger.info(f"تم تغيير فترة المسح إلى {new_interval_sec} ثانية من تيليجرام.")

            elif data == "force_scan_now":
                state_manager.set("force_scan_now", True)
                self.edit_message(
                    chat_id,
                    message_id,
                    "⚡ تم إرسال أمر المسح الفوري بنجاح! سيبدأ البوت بالبحث عن فرص فوراً.",
                    self.main_menu_keyboard()
                )
                logger.info("تم تفعيل المسح الفوري من تيليجرام.")

            elif data == "reset_kill_switch":
                was_active = state_manager.get("kill_switch_active", False)
                state_manager.set("kill_switch_active", False)
                self._record_config_change(state_manager, "kill_switch_active", was_active, False)
                if was_active:
                    msg = "🟢 <b>Kill Switch تم إلغاؤه.</b>\nالبوت سيعود لفتح صفقات جديدة في الدورة القادمة."
                else:
                    msg = "ℹ️ Kill Switch لم يكن نشطاً أصلاً."
                self.edit_message(chat_id, message_id, msg, self.main_menu_keyboard())
                logger.info("تم إلغاء Kill Switch يدوياً من تيليجرام.")

            elif data == "gui_restart_confirm":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔄 <b>تأكيد إعادة التشغيل</b>\n\nهل تريد حفظ الحالة وإعادة تشغيل البوت الآن؟",
                    self.confirm_restart_keyboard(),
                )

            elif data == "confirm_restart":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🔄 <b>تم التأكيد.</b>\nجاري حفظ الحالة وإعادة التشغيل...",
                    None,
                )
                self.restart_bot(state_manager, session=session, offset=offset)

            elif data == "gui_stop_confirm":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🛑 <b>تأكيد إيقاف الطوارئ</b>\n\nهذا الأمر سيوقف البوت فوراً بعد حفظ الحالة.",
                    self.confirm_stop_keyboard(),
                )

            elif data == "confirm_stop":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🛑 <b>تم التأكيد.</b>\nجاري حفظ الحالة وإيقاف البوت فوراً...",
                    None,
                )
                self.stop_bot(state_manager, session=session, offset=offset)

            elif data == "gui_reset_confirm":
                self.edit_message(
                    chat_id,
                    message_id,
                    "🗑️ <b>تأكيد مسح بيانات البوت</b>\n\n"
                    "⚠️ سيتم حذف:\n"
                    "  • سجل حالة البوت (bot_state.json)\n"
                    "  • مجلد سجلات الصفقات (trade_journal/)\n"
                    "  • ملف الصفقات CSV\n\n"
                    "✅ لن يتأثر: الكود، API Keys، الإعدادات.\n\n"
                    "<b>⛔ لن يعمل إذا كانت هناك صفقات مفتوحة.</b>\n\n"
                    "هل أنت متأكد؟",
                    self.confirm_reset_keyboard(),
                )

            elif data == "confirm_reset_data":
                self.edit_message(
                    chat_id, message_id,
                    "⏳ <b>جاري مسح البيانات...</b>",
                    None,
                )
                threading.Thread(
                    target=self.handle_reset_bot_data,
                    args=(client, state_manager, chat_id, message_id),
                    daemon=True,
                ).start()


            elif data == "gui_protections":
                from datetime import datetime as _dt
                sl_lim = state_manager.get_sl_guard_limit()
                dd_pct = state_manager.get_max_drawdown_pct()

                lp_count = state_manager.get_low_profit_loss_count()
                frozen = state_manager.get_frozen_pairs()
                frozen_text = ""
                if frozen:
                    for pair, until_str in frozen.items():
                        try:
                            until_dt = _dt.fromisoformat(until_str)
                            frozen_text += f"\n  ❄️ {pair} حتى {until_dt.strftime('%H:%M')}"
                        except Exception:
                            frozen_text += f"\n  ❄️ {pair}"
                else:
                    frozen_text = "\n  لا توجد عملات مجمّدة"
                self.edit_message(
                    chat_id, message_id,
                    f"🛡️ <b>أنظمة الحماية</b>\n──────────────\n"
                    f"• حارس وقف الخسارة: {'✅' if state_manager.get_sl_guard_enabled() else '❌'} | حد: {sl_lim} خسائر\n"
                    f"• حارس الخسارة المحققة المتحركة: {'✅' if state_manager.get_max_drawdown_enabled() else '❌'} | حد: {dd_pct}%\n"
                    f"• تجميد أزواج الخسارة: {'✅' if state_manager.get_low_profit_enabled() else '❌'} | حد: {lp_count} خسائر"
                    f"{frozen_text}\n"
                    "• وقف خسارة باينانس: إلزامي ✅\n"
                    f"──────────────\nاختر ما تريد تعديله:",
                    self.protections_menu_keyboard(state_manager),
                )

            elif data == "gui_blacklist_menu":
                self.edit_message(
                    chat_id, message_id,
                    "🚫 <b>إدارة القائمة السوداء (Blacklist)</b>\n\nالعملات المضافة هنا لن يتداول عليها البوت أبداً.\nماذا تريد أن تفعل؟",
                    self.blacklist_menu_keyboard(),
                )

            elif data == "gui_blacklist_add":
                self.user_state[str(chat_id)] = "awaiting_blacklist_gui"
                self.edit_message(
                    chat_id, message_id,
                    "✍️ <b>إضافة عملة للحظر:</b>\n\nأرسل اسم العملة الآن في رسالة نصية (مثال: <code>BTC/USDT</code> أو <code>BTC</code>):",
                    self.blacklist_menu_keyboard(),
                )

            elif data == "gui_blacklist_remove":
                self.user_state[str(chat_id)] = "awaiting_whitelist_gui"
                self.edit_message(
                    chat_id, message_id,
                    "✍️ <b>إزالة عملة من الحظر:</b>\n\nأرسل اسم العملة الآن لفك الحظر (مثال: <code>BTC/USDT</code>):",
                    self.blacklist_menu_keyboard(),
                )

            elif data == "gui_blacklist_show":
                bl = state_manager.get_blacklisted_pairs()
                if bl:
                    bl_text = "\n".join([f"• <code>{pair}</code>" for pair in bl])
                    msg = f"📋 <b>العملات المحظورة حالياً ({len(bl)}):</b>\n\n{bl_text}"
                else:
                    msg = "📋 <b>القائمة السوداء فارغة حالياً.</b>"
                
                self.edit_message(
                    chat_id, message_id,
                    msg,
                    self.blacklist_menu_keyboard(),
                )

            elif data == "prot_slguard_toggle":
                old = state_manager.get_sl_guard_enabled()
                new_val = not old
                state_manager.set_sl_guard_enabled(new_val)
                self._record_config_change(state_manager, "sl_guard_enabled", old, new_val)
                status = "مفعّل ✅" if new_val else "معطّل ❌"
                self.edit_message(chat_id, message_id,
                    f"🛡️ StoplossGuard الآن: <b>{status}</b>",
                    self.protections_menu_keyboard(state_manager))

            elif data.startswith("prot_slguard_limit_"):
                limit = int(data.replace("prot_slguard_limit_", ""))
                old = state_manager.get_sl_guard_limit()
                state_manager.set_sl_guard_limit(limit)
                self._record_config_change(state_manager, "sl_guard_trade_limit", old, limit)
                self.edit_message(chat_id, message_id,
                    f"✅ تم تعيين حد StoplossGuard إلى <b>{limit}</b> خسائر.",
                    self.protections_menu_keyboard(state_manager))

            elif data == "prot_slguard_unpause":
                old = state_manager.get_sl_guard_pause()
                state_manager.clear_sl_guard_pause()
                self._record_config_change(state_manager, "sl_guard_pause_until", old, None)
                self.edit_message(chat_id, message_id,
                    "✅ تم إلغاء إيقاف StoplossGuard. البوت سيستأنف التداول.",
                    self.protections_menu_keyboard(state_manager))

            elif data == "prot_dd_toggle":
                old = state_manager.get_max_drawdown_enabled()
                new_val = not old
                state_manager.set_max_drawdown_enabled(new_val)
                self._record_config_change(state_manager, "max_drawdown_enabled", old, new_val)
                status = "مفعّل ✅" if new_val else "معطّل ❌"
                self.edit_message(chat_id, message_id,
                    f"📉 حارس الخسارة المحققة المتحركة الآن: <b>{status}</b>",
                    self.protections_menu_keyboard(state_manager))

            elif data.startswith("prot_dd_pct_"):
                pct = float(data.replace("prot_dd_pct_", ""))
                old = state_manager.get_max_drawdown_pct()
                state_manager.set_max_drawdown_pct(pct)
                self._record_config_change(state_manager, "max_drawdown_percent", old, pct)
                self.edit_message(chat_id, message_id,
                    f"✅ تم تعيين حد حارس الخسارة المحققة إلى <b>{pct:.0f}%</b>.",
                    self.protections_menu_keyboard(state_manager))

            elif data == "prot_dd_unpause":
                old = state_manager.get_max_drawdown_pause()
                state_manager.clear_max_drawdown_pause()
                self._record_config_change(state_manager, "max_drawdown_pause_until", old, None)
                self.edit_message(chat_id, message_id,
                    "✅ تم إلغاء إيقاف حارس الخسارة المحققة. البوت سيستأنف التداول.",
                    self.protections_menu_keyboard(state_manager))

            elif data == "prot_lp_toggle":
                old = state_manager.get_low_profit_enabled()
                new_val = not old
                state_manager.set_low_profit_enabled(new_val)
                self._record_config_change(state_manager, "low_profit_pairs_enabled", old, new_val)
                status = "مفعّل ✅" if new_val else "معطّل ❌"
                self.edit_message(chat_id, message_id,
                    f"❄️ LowProfitPairs الآن: <b>{status}</b>",
                    self.protections_menu_keyboard(state_manager))

            elif data.startswith("prot_lp_count_"):
                count = int(data.replace("prot_lp_count_", ""))
                old = state_manager.get_low_profit_loss_count()
                state_manager.set_low_profit_loss_count(count)
                self._record_config_change(state_manager, "low_profit_loss_count", old, count)
                self.edit_message(chat_id, message_id,
                    f"✅ تم تعيين حد LowProfitPairs إلى <b>{count}</b> خسائر متتالية.",
                    self.protections_menu_keyboard(state_manager))

            elif data == "prot_lp_unfreeze_all":
                state_manager.unfreeze_all_pairs()
                self.edit_message(chat_id, message_id,
                    "✅ تم رفع التجميد عن جميع العملات.",
                    self.protections_menu_keyboard(state_manager))

            elif data == "prot_sl_exchange_toggle":
                self.edit_message(chat_id, message_id,
                    "🔒 <b>SL Binance إلزامي دائماً</b>\n"
                    "لا يمكن تعطيله لأن فتح مركز بلا وقف مؤكد غير مسموح.",
                    self.protections_menu_keyboard(state_manager))

            else:
                self.edit_message(
                    chat_id,
                    message_id,
                    "⚠️ زر غير معروف. عد إلى القائمة الرئيسية.",
                    self.main_menu_keyboard(),
                )

        except Exception as e:
            logger.error(f"خطأ في معالجة زر تيليجرام: {e}")
            self.send_message("⚠️ حدث خطأ أثناء تنفيذ الأمر من الواجهة.", reply_markup=self.main_menu_keyboard())

    # ==========================================================
    # Actions
    # ==========================================================
    def normalize_symbol(self, symbol: str):
        """
        تحويل إدخال المستخدم إلى صيغة تداول موحدة.
        BTC      -> BTC/USDT
        BTCUSDT  -> BTC/USDT
        BTC/USDT -> BTC/USDT
        """
        sym = (symbol or "").strip().upper()
        sym = sym.replace(" ", "")

        if not sym:
            return "BTC/USDT"

        if "/" in sym:
            return sym

        if sym.endswith("USDT"):
            return sym.replace("USDT", "/USDT")

        return f"{sym}/USDT"

    def run_health_report(self, client, state_manager):
        try:
            from utils.health_monitor import HealthMonitor
            health = HealthMonitor(client, state_manager, self)
            health.send_health_report()
        except Exception as e:
            logger.error(f"فشل إرسال تقرير الصحة: {e}")
            self.send_message("⚠️ تعذر توليد تقرير صحة النظام حالياً.", reply_markup=self.main_menu_keyboard())

    def stop_bot(self, state_manager, session=None, offset=None):
        logger.critical("تم إيقاف البوت يدوياً عبر تيليجرام.")
        saved = state_manager.save_state()

        if saved:
            self.send_message("🛑 <b>أمر طوارئ:</b> تم حفظ البيانات. جاري إيقاف البوت فوراً!")
        else:
            self.send_message("⚠️ <b>تحذير:</b> فشل حفظ البيانات! جاري الإيقاف مع احتمال فقدان بيانات.")

        try:
            if session and offset:
                session.get(f"{self.base_url}/getUpdates", params={"offset": offset}, timeout=5)
        except Exception:
            pass

        os._exit(0)

    def restart_bot(self, state_manager, session=None, offset=None):
        logger.critical("تم طلب إعادة تشغيل البوت عبر تيليجرام.")
        state_manager.save_state()
        self.send_message("🔄 <b>أمر إعادة تشغيل:</b> تم حفظ البيانات. جاري إعادة تشغيل البوت الآن...")

        try:
            if session and offset:
                session.get(f"{self.base_url}/getUpdates", params={"offset": offset}, timeout=5)
        except Exception:
            pass

        os.execv(sys.executable, ["python"] + sys.argv)

    # ==========================================================
    # AI Analysis
    # ==========================================================
    def handle_analyze_request(self, symbol, client, state_manager, ai_engine, ta_engine, news_engine, social_engine):
        """
        معالجة طلب التحليل الذكي وتوليد التقرير.
        """
        if not ai_engine or not ta_engine:
            self.send_message("⚠️ عذراً، محركات التحليل غير متوفرة حالياً.", reply_markup=self.main_menu_keyboard())
            return

        self.send_message(
            f"⏳ جاري تجميع البيانات والتحليل الذكي لعملة <b>{self._safe_html(symbol)}</b>...\nيرجى الانتظار قليلاً 🤖",
            reply_markup=self.main_menu_keyboard(),
        )

        try:
            # 1. جلب السعر.
            current_price = client.get_current_price(symbol)
            if not current_price:
                self.send_message(
                    f"⚠️ لم أتمكن من العثور على العملة <b>{self._safe_html(symbol)}</b>. تأكد من صحة الرمز.",
                    reply_markup=self.analyze_keyboard(),
                )
                return

            # 2. التحليل الفني.
            ema_mode = state_manager.get("ema_mode", "dual")
            ta_data = ta_engine.analyze(symbol, client, ema_mode=ema_mode)
            if not ta_data:
                ta_data = {}

            # 3. الأخبار والمشاعر.
            coin_name = symbol.split("/")[0]

            news_text = ""
            if news_engine:
                articles = news_engine.fetch_news_for_coin(coin_name)
                news_text = " | ".join([a.get("title", "") for a in articles[:3]]) if articles else "لا توجد أخبار هامة."

            social_text = ""
            if social_engine:
                sentiment = social_engine.get_social_sentiment(coin_name)
                if sentiment:
                    social_text = (
                        f"حالة السوشيال: {sentiment.get('label', 'محايد')} "
                        f"(Galaxy Score: {sentiment.get('galaxy_score', '?')})"
                    )
                else:
                    social_text = "تفاعل ضعيف في السوشيال ميديا."

            # 4. جلب بيانات متعددة الفريمات وتوليد التقرير الذكي.
            try:
                mtf_data = ta_engine.get_multi_timeframe_indicators(symbol, client)
                from core.config import Config
                use_debate = getattr(Config, "AI_DEBATE_MODE", False)
                if use_debate:
                    report = ai_engine.generate_debate_analysis(
                        symbol,
                        current_price,
                        mtf_data,
                    )
                else:
                    report = ai_engine.generate_interactive_analysis_v2(
                        symbol,
                        current_price,
                        mtf_data,
                    )
                logger.info(f"[Analyze] تم توليد تقرير V2 متعدد الفريمات لـ {symbol}")
            except Exception as mtf_err:
                logger.warning(f"[Analyze] فشل V2، تراجع للتحليل الأساسي: {mtf_err}")
                report = ai_engine.generate_interactive_analysis(
                    symbol,
                    current_price,
                    ta_data,
                    news_text,
                    social_text,
                )

            keyboard = {
                "inline_keyboard": [
                    [
                        self._make_btn("📊 حالة البوت", "gui_status", "primary", icon_key="bar_chart"),
                        self._make_btn("🔄 تحليل مرة أخرى", f"analyze_{coin_name}", "primary", icon_key="refresh_restart"),
                    ],
                    [
                        self._make_btn("🧠 تحليل عملة أخرى", "gui_analyze", "primary", icon_key="brain_ai"),
                        self._make_btn("🏠 الرئيسية", "gui_home", "primary", icon_key="home"),
                    ],
                ]
            }

            self.send_message(report, reply_markup=keyboard)

        except Exception as e:
            logger.error(f"خطأ أثناء توليد التقرير التفاعلي: {e}")
            self.send_message(
                f"⚠️ حدث خطأ أثناء تحليل <b>{self._safe_html(symbol)}</b>. الرجاء المحاولة لاحقاً.",
                reply_markup=self.main_menu_keyboard(),
            )
