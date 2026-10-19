# engines/ai_engine.py
# ─────────────────────────────────────────────────────────────────
# محرك الذكاء الاصطناعي — نظام Fallback رباعي
# Gemini (Vision مدعوم) → Groq → DeepSeek → Qwen
# ─────────────────────────────────────────────────────────────────

from core.config import Config
from core.logger import logger
import json
import base64
import re
import os
import requests
from utils.ai_cache import AIAnalysisCache

_ai_cache = AIAnalysisCache(ttl_seconds=300)


_FORBIDDEN_EXECUTION_OUTPUT = re.compile(
    r"(?im)(?:\bTP\d*\b|\bSL\b|وقف\s+الخسارة|سعر\s+الدخول|نقطة\s+الدخول|"
    r"الرافعة|leverage|\b(?:LONG|SHORT)\b|(?:الدخول|الهدف)\s*:|"
    r"(?:القرار|التوصية)\s*:?\s*(?:LONG|SHORT))"
)


def _sanitize_analysis_narrative(result: str | None) -> str | None:
    """Fail closed when an LLM manufactures an executable trade plan."""

    text = str(result or "").strip()
    if not text:
        return None
    if _FORBIDDEN_EXECUTION_OUTPUT.search(text):
        logger.warning(
            "[AIEngine] تم رفض صياغة AI لأنها تضمنت خطة تنفيذ غير مصادق عليها."
        )
        return None
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    return (
        "🧠 <b>قراءة تفسيرية فقط — لا تملك صلاحية تنفيذ</b>\n"
        + text
        + "\n\n⚠️ لا تظهر مستويات الدخول والوقف والهدف إلا بعد اعتماد Risk Engine."
    )

# ── تحميل المكتبات المتاحة (لا يوقف البوت إذا غابت واحدة) ──────
try:
    from google import genai as _genai_new
    from google.genai import types as _genai_types
    _GEMINI_AVAILABLE = True
    _GEMINI_NEW_SDK = True
except ImportError:
    try:
        import google.generativeai as _genai_new
        _genai_types = None
        _GEMINI_AVAILABLE = True
        _GEMINI_NEW_SDK = False
        logger.warning("[AIEngine] تحذير: تستخدم الإصدار القديم google.generativeai — يُنصح بالتحديث: pip install google-genai")
    except ImportError:
        _genai_new = None
        _genai_types = None
        _GEMINI_AVAILABLE = False
        _GEMINI_NEW_SDK = False
        logger.warning("[AIEngine] مكتبة google-genai غير مثبتّة. شغّل: pip install google-genai")

try:
    from groq import Groq as _GroqClient
    _GROQ_AVAILABLE = True
except ImportError:
    # ── 2. تهيئة Gemini مع تعدد المفاتيح ───────────
    _GroqClient = None
    _GROQ_AVAILABLE = False
    logger.warning("[AIEngine] مكتبة groq غير مثبّتة.")


class AIEngine:
    """
    محرك الذكاء الاصطناعي مع Fallback تلقائي (4 مزودين):
    1. Google Gemini  → الأساسي (يدعم Vision أيضاً)
    2. Groq (Llama)  → Fallback ثاني (سريع جداً)
    3. DeepSeek      → Fallback ثالث (قوي في التفكير المنطقي)
    4. Qwen          → Fallback رابع (متعدد اللغات)
    """

    def __init__(self):
        self.provider = getattr(Config, "AI_ENGINE_PROVIDER", "gemini").lower()

        # ── 1. تهيئة Gemini مع تعدد المفاتيح (الأساسي) ───────────
        self._gemini_keys: list[str] = []
        self._gemini_key_idx: int = 0
        self.gemini_client = None
        self.gemini_model_id = getattr(Config, "GEMINI_MODEL", "gemini-2.0-flash")

        raw_gemini = getattr(Config, "GEMINI_API_KEYS", [])
        if isinstance(raw_gemini, list):
            self._gemini_keys = [k for k in raw_gemini if k and len(k) > 10]
        elif isinstance(raw_gemini, str) and raw_gemini:
            self._gemini_keys = [raw_gemini]

        if self._gemini_keys and _GEMINI_AVAILABLE:
            try:
                if _GEMINI_NEW_SDK:
                    # SDK الجديد: google.genai — يستخدم genai.Client()
                    self._gemini_client_obj = _genai_new.Client(api_key=self._gemini_keys[0])
                    self.gemini_client = self._gemini_client_obj  # حفظ للاتساق مع بقية الكود
                else:
                    # SDK القديم: google.generativeai — للتوافقية
                    _genai_new.configure(api_key=self._gemini_keys[0])
                    self.gemini_client = _genai_new.GenerativeModel(self.gemini_model_id)
                logger.info(f"[AIEngine] ✅ Gemini جاهز ({'SDK جديد' if _GEMINI_NEW_SDK else 'SDK قديم'}) — {len(self._gemini_keys)} مفتاح — موديل: {self.gemini_model_id}")
            except Exception as e:
                logger.warning(f"[AIEngine] ⚠️ Gemini فشل في التهيئة: {e}")
        else:
            logger.warning("[AIEngine] ⚠️ Gemini غير متاح — لا مفاتيح أو المكتبة مفقودة")


        # ── 2. تهيئة Groq (Fallback نهائي) ──────────────────────────
        self.groq_client = None
        self.groq_model_id = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        
        # دعم مفاتيح Groq المتعددة إن وجدت
        self._groq_keys: list[str] = getattr(Config, "GROQ_API_KEYS", [])
        self._groq_key_idx: int = 0
        
        if self._groq_keys and _GROQ_AVAILABLE:
            self.groq_client = _GroqClient(api_key=self._groq_keys[0])
            logger.info(f"[AIEngine] ✅ Groq جاهز — {len(self._groq_keys)} مفتاح (Fallback نهائي)")
        else:
            logger.warning("[AIEngine] ⚠️ Groq غير متاح")

        # ── 3. تهيئة DeepSeek (Fallback #3) ──────────────────────────
        self.deepseek_model_id = getattr(Config, "DEEPSEEK_MODEL", "deepseek-chat")
        self.deepseek_url = "https://api.deepseek.com/v1/chat/completions"
        raw_deepseek = getattr(Config, "DEEPSEEK_API_KEYS", [])
        if isinstance(raw_deepseek, str):
            raw_deepseek = [k.strip() for k in raw_deepseek.split(",") if k.strip()]
        self._deepseek_keys: list[str] = [k for k in raw_deepseek if k and len(k) > 10]
        self._deepseek_key_idx: int = 0
        if self._deepseek_keys:
            logger.info(f"[AIEngine] ✅ DeepSeek جاهز — {len(self._deepseek_keys)} مفتاح")
        else:
            logger.warning("[AIEngine] ⚠️ DeepSeek غير متاح — لا مفاتيح")

        # ── 4. تهيئة Qwen (Fallback #4) ──────────────────────────────
        self.qwen_model_id = getattr(Config, "QWEN_MODEL", "qwen-plus")
        self.qwen_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
        raw_qwen = getattr(Config, "QWEN_API_KEYS", [])
        if isinstance(raw_qwen, str):
            raw_qwen = [k.strip() for k in raw_qwen.split(",") if k.strip()]
        self._qwen_keys: list[str] = [k for k in raw_qwen if k and len(k) > 10]
        self._qwen_key_idx: int = 0
        if self._qwen_keys:
            logger.info(f"[AIEngine] ✅ Qwen جاهز — {len(self._qwen_keys)} مفتاح")
        else:
            logger.warning("[AIEngine] ⚠️ Qwen غير متاح — لا مفاتيح")

        # GLM was removed

        # ── متغيرات التوافق مع الكود القديم ──────────────────────────
        if self.provider == "gemini" and self.gemini_client:
            self.client   = self.gemini_client
            self.model_id = self.gemini_model_id
        elif self.groq_client:
            self.client   = self.groq_client
            self.model_id = self.groq_model_id
        else:
            self.client   = None
            self.model_id = None
            logger.error("[AIEngine] ❌ لا يوجد أي مزود AI متاح!")

    # ──────────────────────────────────────────────────────────────
    # دالة مساعدة عامة: استدعاء أي مزود متوافق مع OpenAI Chat API
    # ──────────────────────────────────────────────────────────────
    def _call_openai_style(
        self,
        provider_label: str,
        api_keys: list,
        current_idx: int,
        url: str,
        model_id: str,
        messages: list,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ):
        """
        نداء عام عبر requests لأي مزود متوافق مع OpenAI Chat Completions
        (DeepSeek / Qwen / GLM). يدور على كل المفاتيح قبل الفشل النهائي.
        يُرجع (result_text أو None, المؤشر الجديد بعد آخر محاولة).
        """
        if not api_keys:
            return None, current_idx
        total_keys = len(api_keys)
        idx = current_idx
        for _ in range(total_keys):
            key = api_keys[idx]
            try:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = {
                    "model": model_id,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                result_text = data["choices"][0]["message"]["content"]
                logger.info(f"[AIEngine/{provider_label}] ✅ نجح (مفتاح {idx + 1}/{total_keys})")
                return result_text, idx
            except Exception as e:
                logger.warning(f"[AIEngine/{provider_label}] مفتاح {idx + 1} فشل: {str(e)[:100]}")
                idx = (idx + 1) % total_keys
        return None, idx

    # ──────────────────────────────────────────────────────────────
    # دالة الاستدعاء الموحّدة مع Fallback تلقائي وتدوير المفاتيح
    # ──────────────────────────────────────────────────────────────
    def _call_text(
        self,
        messages: list,
        temperature: float = 0.2,
        max_tokens: int = 1000,
        json_mode: bool = False,
    ) -> str | None:
        """
        يستدعي الموديل بترتيب: Gemini (كل المفاتيح) → Groq (كل المفاتيح).
        """
        # ── المحاولة 1: Gemini (تدوير جميع المفاتيح) ────────────────
        if self.gemini_client and _GEMINI_AVAILABLE:
            total_keys = len(self._gemini_keys)
            for attempt in range(total_keys):
                try:
                    full_prompt = "\n\n".join(
                        f"[{m.get('role', 'user').upper()}]:\n{m.get('content', '')}"
                        for m in messages
                    )
                    gen_cfg = {"temperature": temperature, "max_output_tokens": max_tokens}
                    if _GEMINI_NEW_SDK:
                        try:
                            # لدعم المفاتيح الجديدة (Auth Keys) نستخدم Interactions API
                            interaction = self._gemini_client_obj.interactions.create(
                                model=self.gemini_model_id,
                                input=full_prompt
                            )
                            result_text = interaction.output_text
                        except Exception as _int_exc:
                            # كاحتياطي إذا كان المفتاح قديماً ويدعم generate_content فقط
                            resp = self._gemini_client_obj.models.generate_content(
                                model=self.gemini_model_id,
                                contents=full_prompt,
                                config=_genai_types.GenerateContentConfig(
                                    temperature=temperature,
                                    max_output_tokens=max_tokens,
                                ) if _genai_types else None,
                            )
                            result_text = resp.text
                    else:
                        resp = self.gemini_client.generate_content(full_prompt, generation_config=gen_cfg)
                        result_text = resp.text
                    logger.info(f"[AIEngine/Gemini] ✅ نجح (مفتاح {self._gemini_key_idx + 1}/{total_keys})")
                    return result_text
                except Exception as e:
                    err_msg = str(e)[:100]
                    logger.warning(f"[AIEngine/Gemini] مفتاح {self._gemini_key_idx + 1} فشل: {err_msg}")
                    if total_keys > 1:
                        self._gemini_key_idx = (self._gemini_key_idx + 1) % total_keys
                        if _GEMINI_NEW_SDK:
                            self._gemini_client_obj = _genai_new.Client(api_key=self._gemini_keys[self._gemini_key_idx])
                        else:
                            _genai_new.configure(api_key=self._gemini_keys[self._gemini_key_idx])
                            self.gemini_client = _genai_new.GenerativeModel(self.gemini_model_id)
                        logger.info(f"[AIEngine] التبديل لمفتاح Gemini رقم {self._gemini_key_idx + 1}")

        # ── المحاولة 2: Groq (تدوير جميع المفاتيح) ──────────────────
        if self.groq_client and _GROQ_AVAILABLE:
            total_keys = len(self._groq_keys)
            for attempt in range(total_keys):
                try:
                    kwargs = dict(
                        model=self.groq_model_id,
                        messages=messages,
                        temperature=temperature,
                    )
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    resp = self.groq_client.chat.completions.create(**kwargs)
                    logger.info(f"[AIEngine/Groq] ✅ نجح (مفتاح {self._groq_key_idx + 1}/{total_keys}) كـ Fallback")
                    return resp.choices[0].message.content
                except Exception as e:
                    err_msg = str(e)[:100]
                    logger.warning(f"[AIEngine/Groq] مفتاح {self._groq_key_idx + 1} فشل: {err_msg}")
                    if total_keys > 1:
                        self._groq_key_idx = (self._groq_key_idx + 1) % total_keys
                        self.groq_client = _GroqClient(api_key=self._groq_keys[self._groq_key_idx])
                        logger.info(f"[AIEngine] التبديل لمفتاح Groq رقم {self._groq_key_idx + 1}")

        # ── المحاولة 3: DeepSeek ─────────────────────────────────────
        result, self._deepseek_key_idx = self._call_openai_style(
            "DeepSeek", self._deepseek_keys, self._deepseek_key_idx,
            self.deepseek_url, self.deepseek_model_id,
            messages, temperature, max_tokens, json_mode,
        )
        if result:
            return result

        # ── المحاولة 4: Qwen ─────────────────────────────────────────
        result, self._qwen_key_idx = self._call_openai_style(
            "Qwen", self._qwen_keys, self._qwen_key_idx,
            self.qwen_url, self.qwen_model_id,
            messages, temperature, max_tokens, json_mode,
        )
        if result:
            return result

        # (تمت إزالة GLM)

        logger.error("[AIEngine] ❌ جميع المزودات والمفاتيح فشلت!")
        return None

    # ──────────────────────────────────────────────────────────────
    # 1. تحليل المشاعر
    # ──────────────────────────────────────────────────────────────
    def analyze_sentiment(self, text: str) -> dict | None:
        """تحليل مشاعر النصوص الإخبارية."""
        if not self.client and not self.gemini_client and not self.groq_client:
            logger.warning("[AIEngine] لا يوجد مزود AI — تخطي تحليل المشاعر.")
            return None

        prompt = f"""Analyze the following financial/crypto news and determine its sentiment.
Respond ONLY with valid JSON with exactly two keys:
- "label": "positive", "negative", or "neutral"
- "score": confidence float 0.00-1.00

News: "{text}"

JSON:"""
        try:
            result = self._call_text(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=100,
                json_mode=True,
            )
            if not result:
                return None
            data = json.loads(result)
            return {
                "label": str(data.get("label", "neutral")).lower(),
                "score": float(data.get("score", 0.0)),
            }
        except Exception as e:
            logger.error(f"[AIEngine] خطأ في تحليل المشاعر: {e}")
        return None

    # ──────────────────────────────────────────────────────────────
    # 2. التحليل الأساسي (V1 — فريم واحد — للتوافق مع الكود القديم)
    # ──────────────────────────────────────────────────────────────
    def generate_interactive_analysis(
        self, symbol, current_price, ta_data, news_text, social_text
    ) -> str:
        """التحليل الأساسي بفريم زمني واحد (15m). يُستخدم كـ Fallback إذا فشل V2."""
        prompt = f"""You are an analytical crypto market assistant.
Explain {symbol} in a SHORT Arabic report using only the supplied values.

Data:
- Price: ${current_price}
- RSI(14): {ta_data.get('rsi_14', ta_data.get('rsi', 'N/A'))}
- EMA50: {ta_data.get('ema_50', 'N/A')}
- EMA200: {ta_data.get('ema_200', 'N/A')}
- ATR: {ta_data.get('atr', 'N/A')}
- ADX: {ta_data.get('adx', 'N/A')}
- MACD Hist: {ta_data.get('macd_hist', 'N/A')}
- Support: {ta_data.get('support', 'N/A')}
- Resistance: {ta_data.get('resistance', 'N/A')}
- News: "{news_text}"
- Social: "{social_text}"

Format (Arabic only, emojis, mobile-friendly):
🪙 **العملة:** {symbol}
💰 **السعر اللحظي:** {current_price} $
📊 **السياق الفني:** (ما الذي تؤيده المؤشرات وما الذي يعارضه)
📰 **البيانات الاختيارية:** (اذكر صراحة ما هو ناقص أو غير متاح)
🧭 **الحالة:** مراقبة / انتظار تأكيد / بيانات غير كافية
⏭️ **ما الذي نراقبه تالياً:** وصف حدث فقط دون اختراع مستوى سعري

Hard rules:
- trade_allowed=false: this request has no Risk Engine-approved trade plan.
- Never output LONG, SHORT, entry, TP, SL, leverage, confidence percentage, or new numbers.
- Never invent support/resistance or fill missing values.
- Explain uncertainty and contradictions. Arabic only. No code blocks."""

        result = self._call_text(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=700,
        )
        safe = _sanitize_analysis_narrative(result)
        return safe or (
            "⚠️ تعذر اعتماد تحليل الذكاء الاصطناعي بأمان؛ لم تُعرض أي خطة غير "
            "مصادق عليها من محرك المخاطر."
        )

    # ──────────────────────────────────────────────────────────────
    # 3. التحليل الاحترافي متعدد الفريمات (V2 — الجديد)
    # ──────────────────────────────────────────────────────────────
    def generate_interactive_analysis_v2(
        self, symbol: str, current_price: float, mtf_data: dict
    ) -> str:
        """
        تحليل احترافي متعدد الفريمات (15m, 1h, 4h, 1d).
        mtf_data: يأتي من ta_engine.get_multi_timeframe_indicators()
        يعمل مع OpenAI / Gemini / Groq تلقائياً.
        يستخدم الكاش لتفادي الاستدعاءات المتكررة.
        """
        cached = _ai_cache.get(symbol)
        if cached and "result" in cached:
            return cached["result"]

        def _fmt(d: dict) -> str:
            if not d:
                return "لا توجد بيانات"
            return (
                f"RSI={d.get('rsi','N/A')} | EMA50={d.get('ema_50','N/A')} | "
                f"EMA200={d.get('ema_200','N/A')} | ATR={d.get('atr','N/A')} | "
                f"MACD_hist={d.get('macd_hist','N/A')} | ADX={d.get('adx','N/A')} | "
                f"Volume={d.get('volume','N/A')} | Trend={d.get('supertrend_dir','N/A')} | "
                f"Support1={d.get('support1','N/A')} | Support2={d.get('support2','N/A')} | "
                f"Resist1={d.get('resistance1','N/A')} | Resist2={d.get('resistance2','N/A')} | "
                f"VPD={d.get('alpha_vpd','N/A')} | GMD={d.get('alpha_gmd','N/A')} | VAM={d.get('alpha_vam','N/A')}"
            )

        system_msg = (
            "أنت مساعد تحليلي يشرح بيانات رقمية حتمية ولا يصدر أوامر تداول. "
            "تحلل السوق من أعلى إطار زمني لأدناه: "
            "1) الاتجاه العام (1D → 4H) "
            "2) توافق أو تعارض الحركة الأقصر (1H → 15m) "
            "3) جودة البيانات والمستويات الموجودة فقط. "
            "ممنوع اختراع أرقام أو إعطاء LONG/SHORT أو دخول أو TP أو SL أو رافعة. "
            "trade_allowed=false لأن Risk Engine لم يعتمد خطة. "
            "الرد بالعربية فقط، احتمالي ومباشر وبدون حشو."
        )

        user_msg = f"""
حلل عملة {symbol} — السعر الحالي: {current_price} $

📅 اليومي (1D): {_fmt(mtf_data.get('1d', {}))}
🕓 4 ساعات (4H): {_fmt(mtf_data.get('4h', {}))}
🕐 ساعة (1H): {_fmt(mtf_data.get('1h', {}))}
⏱ 15 دقيقة (15m): {_fmt(mtf_data.get('15m', {}))}

أنتج التقرير بهذا التنسيق الصارم (استخدم HTML tags مثل <b> بدلاً من **):

🪙 <b>{symbol}</b> | 💰 {current_price} $

📈 <b>الاتجاه العام:</b> (من 1D و4H)
📊 <b>الحركة اللحظية:</b> (من 1H و15m)

✅ <b>العوامل المؤيدة:</b> (من القيم الموجودة فقط)
⚠️ <b>العوامل المعارضة أو الناقصة:</b>
🧭 <b>الحالة:</b> مراقبة / انتظار تأكيد / بيانات غير كافية
⏭️ <b>ما الذي نراقبه تالياً:</b> حدث وصفي دون اختراع مستوى

⚠️ قراءة تفسيرية؛ لا توجد خطة دخول مصادق عليها.
"""
        result = self._call_text(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=1000,
        )
        safe = _sanitize_analysis_narrative(result)
        if safe:
            _ai_cache.set(symbol, {"result": safe})
            return safe
        return (
            "⚠️ تعذر اعتماد التحليل من جميع المزودات بأمان؛ لم تُعرض أي "
            "مستويات غير مصادق عليها."
        )

    # ──────────────────────────────────────────────────────────────
    # 3c. نقد توقيت الصفقة بعد الإغلاق (AI Trade Critique)
    # يحلل الشموع الفعلية حول لحظة الدخول ويقيّم: مبكر / متأخر / سليم
    # ──────────────────────────────────────────────────────────────
    def generate_trade_critique(self, symbol: str, entry_meta: dict, exit_details: dict, candles_df) -> str:
        try:
            if candles_df is None or candles_df.empty:
                return ""

            side        = str(entry_meta.get("side", "buy")).upper()
            entry_price = entry_meta.get("entry_price", 0)
            sl          = entry_meta.get("sl", 0)
            tp          = entry_meta.get("tp", 0)
            exit_price  = exit_details.get("exit_price", 0)
            pnl_pct     = exit_details.get("pnl_pct", 0)

            window = candles_df.head(60)  # سقف 60 شمعة لضبط حجم الطلب
            candle_lines = [
                f"{r.get('utc_time','')}: O={r.get('open',0):.4f} H={r.get('high',0):.4f} "
                f"L={r.get('low',0):.4f} C={r.get('close',0):.4f}"
                for _, r in window.iterrows()
            ]
            candles_text = "\n".join(candle_lines)

            strategy_name = entry_meta.get("strategy_source", "unknown")

            prompt = f"""أنت خبير تحليل فني متخصص في نقد توقيت صفقات الفيوتشر بعد إغلاقها.
العملة: {symbol} | الاتجاه: {side} | الاستراتيجية: {strategy_name}
سعر الدخول: {entry_price} | SL: {sl} | TP: {tp}
سعر الخروج: {exit_price} | النتيجة: {pnl_pct:+.2f}%

سلسلة الشموع الفعلية حول لحظة الدخول (قبل وبعد):
{candles_text}

قيّم بدقة (مع مراعاة طبيعة الاستراتيجية المستخدمة):
1) هل نقطة الدخول سليمة أم دخل البوت متأخراً بعد جزء كبير من الحركة؟
2) هل كان يلزم انتظار تأكيد إضافي قبل الدخول؟
3) هل كانت هناك نقطة أفضل قبل أو بعد الدخول الفعلي بقليل؟

الرد بالعربية، 4 أسطر كحد أقصى، مباشر، استخدم <b> بدلاً من **."""

            result = self._call_text(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=350,
            )
            if not result:
                return ""

            result = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', result)
            return f"🧠 <b>نقد الذكاء الاصطناعي — {symbol}</b>\n{result.strip()}\n\n⚠️ تحليل آلي وليس نصيحة مالية."
        except Exception as e:
            logger.warning(f"[AIEngine/Critique] فشل توليد نقد الصفقة: {e}")
            return ""

    # ──────────────────────────────────────────────────────────────
    # 3b. لجنة الذكاء الاصطناعي — AI Debate (للتحليل اليدوي فقط)
    # Bull + Bear موازيان → Judge يحكم
    # ⚠️ لا تستخدم هذه الدالة في run_bot_iteration (بطيئة جداً)
    # استخدمها فقط عند طلب /analyze من تيليجرام
    # ──────────────────────────────────────────────────────────────
    def generate_debate_analysis(
        self, symbol: str, current_price: float, mtf_data: dict
    ) -> str:
        """
        تحليل بأسلوب لجنة النقاش: Bull vs Bear → Judge.
        يعمل بالتوازي (threading) لتقليل وقت الانتظار.
        مناسب فقط للتحليل اليدوي من تيليجرام — لا للمسح الآلي.
        """
        import threading

        # ── بناء بيانات السوق المشتركة ────────────────────────────
        def _fmt(d: dict) -> str:
            if not d:
                return "لا توجد بيانات"
            return (
                f"RSI={d.get('rsi','N/A')} | EMA50={d.get('ema_50','N/A')} | "
                f"EMA200={d.get('ema_200','N/A')} | ATR={d.get('atr','N/A')} | "
                f"MACD_hist={d.get('macd_hist','N/A')} | ADX={d.get('adx','N/A')} | "
                f"Trend={d.get('supertrend_dir','N/A')} | "
                f"Support1={d.get('support1','N/A')} | Resist1={d.get('resistance1','N/A')}"
            )

        market_context = f"""
العملة: {symbol} | السعر: {current_price}$
اليومي (1D): {_fmt(mtf_data.get('1d', {}))}
4 ساعات (4H): {_fmt(mtf_data.get('4h', {}))}
ساعة (1H): {_fmt(mtf_data.get('1h', {}))}
15 دقيقة (15m): {_fmt(mtf_data.get('15m', {}))}"""

        # ── Bull Prompt ───────────────────────────────────────────
        bull_prompt = f"""أنت مراجع Bull. استخرج الأدلة الصعودية الموجودة فقط.
{market_context}

قدّم 3 أدلة صعودية محتملة دون توصية شراء ودون اختراع أرقام أو مستويات.
trade_allowed=false. الرد موجز (5 أسطر فقط)."""

        # ── Bear Prompt ───────────────────────────────────────────
        bear_prompt = f"""أنت مراجع Bear. استخرج الأدلة الهبوطية والمخاطر الموجودة فقط.
{market_context}

قدّم 3 أدلة هبوطية محتملة دون توصية بيع ودون اختراع أرقام أو مستويات.
trade_allowed=false. الرد موجز (5 أسطر فقط)."""

        # ── تشغيل Bull و Bear بالتوازي ───────────────────────────
        bull_result = [None]
        bear_result = [None]

        def _run_bull():
            try:
                bull_result[0] = self._call_text(
                    messages=[{"role": "user", "content": bull_prompt}],
                    temperature=0.4,
                    max_tokens=300,
                )
            except Exception as e:
                logger.warning(f"[Debate/Bull] فشل: {e}")
                bull_result[0] = "فشل Bull في التحليل"

        def _run_bear():
            try:
                bear_result[0] = self._call_text(
                    messages=[{"role": "user", "content": bear_prompt}],
                    temperature=0.4,
                    max_tokens=300,
                )
            except Exception as e:
                logger.warning(f"[Debate/Bear] فشل: {e}")
                bear_result[0] = "فشل Bear في التحليل"

        t_bull = threading.Thread(target=_run_bull, daemon=True)
        t_bear = threading.Thread(target=_run_bear, daemon=True)
        t_bull.start()
        t_bear.start()
        t_bull.join(timeout=15)
        t_bear.join(timeout=15)

        bull_text = bull_result[0] or "لا يوجد رد من Bull"
        bear_text = bear_result[0] or "لا يوجد رد من Bear"

        # ── Judge Prompt — يقرأ كلا الرأيين ─────────────────────
        judge_prompt = f"""أنت مراجع محايد يشرح الأدلة ولا يصدر قرار تنفيذ.
قرأت تقريرين متعارضين حول {symbol} (السعر: {current_price}$):

📈 رأي المتفائل (Bull):
{bull_text}

📉 رأي المتشائم (Bear):
{bear_text}

بيانات السوق الفعلية:
{market_context}

أصدر حكمك النهائي بهذا التنسيق الصارم (استخدم HTML tags مثل <b> بدلاً من **):
⚖️ <b>حكم اللجنة:</b> {symbol}

📈 <b>Bull قال:</b> (ملخص سطر واحد)
📉 <b>Bear قال:</b> (ملخص سطر واحد)

🏛️ <b>الخلاصة التحليلية:</b> ميل صعودي / ميل هبوطي / أدلة متعارضة
⚠️ <b>البيانات الناقصة:</b> (اذكرها دون تعويضها)
⏭️ <b>ما الذي نراقبه تالياً:</b> حدث وصفي موجود في السياق دون اختراع مستوى
💡 <b>السبب:</b> (سطر واحد)

ممنوع LONG/SHORT أو دخول أو TP أو SL أو رافعة أو نسبة ثقة رقمية.
trade_allowed=false لأن Risk Engine لم يعتمد خطة."""

        judge_result = self._call_text(
            messages=[{"role": "user", "content": judge_prompt}],
            temperature=0.1,   # Judge يجب أن يكون حازماً لا إبداعياً
            max_tokens=500,
        )

        if not judge_result:
            # Judge فشل — ارجع للتحليل العادي كـ fallback
            logger.warning("[Debate/Judge] فشل Judge — الرجوع للتحليل العادي")
            return self.generate_interactive_analysis_v2(symbol, current_price, mtf_data)

        judge_result = _sanitize_analysis_narrative(judge_result)
        if not judge_result:
            return self.generate_interactive_analysis_v2(symbol, current_price, mtf_data)
        
        full_report = (
            f"🏛️ <b>تقرير لجنة التداول — {symbol}</b>\n\n"
            f"{judge_result.strip()}"
        )
        # نحفظ في الكاش بنفس مفتاح التحليل العادي
        _ai_cache.set(symbol, {"result": full_report})
        logger.info(f"[Debate] ✅ اكتمل تحليل اللجنة لـ {symbol}")
        return full_report

    # ──────────────────────────────────────────────────────────────
    # 4. تحليل صورة الشارت (Vision) — OpenAI أو Gemini
    # ──────────────────────────────────────────────────────────────
    def analyze_chart_vision(self, image_base64: str, symbol: str = "Unknown") -> str:
        """
        تحليل صورة شارت باستخدام Vision.
        - Google Gemini: Vision مدعوم
        - Groq: لا يدعم Vision → يُرجع رسالة خطأ واضحة
        """
        prompt_text = (
            f"أنت محلل تقني خبير. حلّل شارت {symbol} واستخرج:\n"
            "1. مناطق الدعم والمقاومة الواضحة (أرقام تقريبية)\n"
            "2. حالة الترند (صاعد / هابط / جانبي)\n"
            "3. نماذج برايس أكشن واضحة إن وجدت\n"
            "4. توصية (LONG / SHORT / WAIT) مع سبب موجز.\n"
            "الرد بالعربية فقط، مختصر ومنسق بالإيموجي."
        )

        # ── محاولة Gemini Vision ──────────────────────────────────
        if self.gemini_client and _GEMINI_AVAILABLE:
            total_keys = len(self._gemini_keys)
            for attempt in range(total_keys):
                try:
                    img_bytes = base64.b64decode(image_base64)
                    if _GEMINI_NEW_SDK:
                        resp = self._gemini_client_obj.models.generate_content(
                            model=self.gemini_model_id,
                            contents=[
                                prompt_text,
                                _genai_types.Part.from_bytes(data=img_bytes, mime_type="image/png") if _genai_types else prompt_text,
                            ],
                        )
                        result_text = resp.text
                    else:
                        response = self.gemini_client.generate_content([
                            prompt_text,
                            {"mime_type": "image/png", "data": img_bytes},
                        ])
                        result_text = response.text
                    logger.info(f"[Vision/Gemini] ✅ تحليل {symbol} نجح (مفتاح {self._gemini_key_idx + 1})")
                    return result_text.strip()
                except Exception as e:
                    err_msg = str(e)[:80]
                    logger.warning(f"[Vision/Gemini] مفتاح {self._gemini_key_idx + 1} فشل: {err_msg}")
                    if total_keys > 1:
                        self._gemini_key_idx = (self._gemini_key_idx + 1) % total_keys
                        if _GEMINI_NEW_SDK:
                            self._gemini_client_obj = _genai_new.Client(api_key=self._gemini_keys[self._gemini_key_idx])
                        else:
                            _genai_new.configure(api_key=self._gemini_keys[self._gemini_key_idx])
                            self.gemini_client = _genai_new.GenerativeModel(self.gemini_model_id)
                        logger.info(f"[Vision/Gemini] تبديل لمفتاح رقم {self._gemini_key_idx + 1}")

        return "⚠️ تحليل الصور (Vision) مدعوم على Gemini فقط حالياً. Groq/DeepSeek/Qwen لا تدعم الصور في هذا الإصدار."
