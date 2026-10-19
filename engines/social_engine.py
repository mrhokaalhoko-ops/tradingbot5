import requests
import re
from cachetools import TTLCache
from core.config import Config
from core.logger import logger

class SocialEngine:
    """
    محرك تحليل المشاعر الاجتماعية عبر LunarCrush API
    يوفر بيانات دقيقة عن مدى حديث الناس عن العملة + نسبة المشاعر الإيجابية/السلبية
    مثالي للعملات الصغيرة والميم التي لا تغطيها وسائل الإعلام التقليدية

    التحسينات:
    - تدوير تلقائي بين مفاتيح LunarCrush عند 429 (key rotation)
    - احتياطي تلقائي بـ CoinMarketCap Fear & Greed عند تعطل كل المفاتيح
    """

    def __init__(self):
        # ── جمع كل مفاتيح LunarCrush المتاحة ──────────────────────────
        keys_raw = getattr(Config, "LUNARCRUSH_API_KEYS", [])
        single = getattr(Config, "LUNARCRUSH_API_KEY", "")
        if single and single not in keys_raw:
            keys_raw = [single] + list(keys_raw)
        self.api_keys = [k for k in keys_raw if k]  # قائمة نظيفة
        self.api_key  = self.api_keys[0] if self.api_keys else ""  # للتوافق

        self.base_url = "https://lunarcrush.com/api4/public"

        self.cache_ttl            = getattr(Config, "LUNARCRUSH_CACHE_TTL_SECONDS", 3600)
        self.cache = TTLCache(maxsize=512, ttl=self.cache_ttl)
        self._max_requests_per_min = getattr(Config, "LUNARCRUSH_MAX_RPM", 8)

        # cooldown منفصل لكل مفتاح: {key: rate_limited_until_timestamp}
        self._key_cooldowns: dict = {}
        # 402 غالباً خاص بالعملة/الخطة وليس حظراً عاماً للمفتاح. لذلك نعزل
        # التهدئة على مستوى (المفتاح، العملة) حتى لا نعطل عملات أخرى سليمة.
        self._symbol_cooldowns: dict = {}
        # عداد الطلبات منفصل لكل مفتاح: {key: (count, window_start)}
        self._key_counters: dict  = {}

        # CMC fallback
        self._cmc_key = getattr(Config, "COINMARKETCAP_API_KEY", "")
        self._cmc_fg_cache: dict = {}          # cache لـ Fear & Greed
        self._cmc_fg_ttl = 900                 # 15 دقيقة

    # ─────────────────────────────────────────────────────────────────────
    # الدالة الرئيسية
    # ─────────────────────────────────────────────────────────────────────
    def get_social_sentiment(self, coin_symbol: str):
        """
        جلب مشاعر السوق الاجتماعية لعملة معينة.
        يُعيد قاموساً يحتوي على:
        - label: 'positive', 'negative', 'neutral'
        - score: قوة الإشارة (0.0 - 1.0)
        - galaxy_score: نقاط LunarCrush الكلية (0-100)
        - social_volume: حجم التفاعل الاجتماعي
        - social_dominance: نسبة الهيمنة الاجتماعية
        """
        if not self.api_keys:
            logger.debug("مفاتيح LunarCrush غير متوفرة.")
            return self._cmc_fallback()

        # تحويل الرمز للشكل الصحيح (BTC/USDT -> BTC)
        symbol = self._normalize_social_symbol(coin_symbol)

        import time
        now = time.time()

        # فحص الكاش أولاً
        cached = self.cache.get(symbol)
        if cached and now - cached["time"] < self.cache_ttl:
            return cached["data"]

        # محاولة كل مفتاح متاح بالترتيب
        for key in self.api_keys:
            result = self._try_key(key, symbol, now)
            if result is not None:
                # نجح المفتاح → احفظ الكاش وأعد
                self.cache[symbol] = {"time": now, "data": result}
                return result

        # كل المفاتيح في cooldown → احتياطي CMC
        logger.warning("[LunarCrush] ⚠️ كل المفاتيح في cooldown — الاحتياطي: CoinMarketCap Fear & Greed")
        return self._cmc_fallback()

    @staticmethod
    def _normalize_social_symbol(coin_symbol: str) -> str:
        """تحويل رمز عقد Binance إلى أصل العملة الذي تفهمه خدمات المشاعر."""
        symbol = str(coin_symbol or "").upper().split(":")[0].replace("/", "").strip()
        symbol = re.sub(r"(?:USDT|USDC|BUSD)$", "", symbol)
        # عقود Binance ذات المضاعف تمثل الأصل نفسه اجتماعياً (1000PEPE -> PEPE).
        # نقيّد التطبيع بالقائمة المعروفة كي لا نفسد رمزاً يبدأ بأرقام فعلاً.
        multiplier_contracts = {
            "1000BONK", "1000CAT", "1000CHEEMS", "1000FLOKI", "1000LUNC",
            "1000PEPE", "1000RATS", "1000SATS", "1000SHIB", "1000WHY",
            "1000X", "1000XEC",
        }
        if symbol in multiplier_contracts:
            symbol = symbol[4:]
        return symbol

    # ─────────────────────────────────────────────────────────────────────
    # محاولة مفتاح واحد
    # ─────────────────────────────────────────────────────────────────────
    def _try_key(self, key: str, symbol: str, now: float):
        """
        يحاول استخدام مفتاح واحد لجلب البيانات.
        يُعيد dict عند النجاح، أو None إذا كان المفتاح في cooldown أو فشل.
        """
        import time

        cooldown = getattr(Config, "LUNARCRUSH_429_COOLDOWN_SECONDS", 600)
        symbol_cooldown = getattr(Config, "LUNARCRUSH_402_COOLDOWN_SECONDS", 3600)

        # هل المفتاح في cooldown؟
        limited_until = self._key_cooldowns.get(key, 0)
        if limited_until > now:
            remaining = int(limited_until - now)
            logger.debug(f"[LunarCrush] مفتاح API في cooldown عام لـ {remaining}s")
            return None

        pair_limited_until = self._symbol_cooldowns.get((key, symbol), 0)
        if pair_limited_until > now:
            remaining = int(pair_limited_until - now)
            logger.debug(f"[LunarCrush] {symbol} في cooldown خاص بالرمز لـ {remaining}s")
            return None
        if pair_limited_until:
            self._symbol_cooldowns.pop((key, symbol), None)
        if limited_until:
            self._key_cooldowns.pop(key, None)

        # Rate limit استباقي لهذا المفتاح
        count, window_start = self._key_counters.get(key, (0, now))
        if now - window_start > 60:
            count = 0
            window_start = now
        if count >= self._max_requests_per_min:
            logger.debug("[LunarCrush] Rate limit استباقي لمفتاح API")
            # حاول إعادة كاش قديم
            cached = self.cache.get(symbol)
            if cached:
                return cached["data"]
            return None

        self._key_counters[key] = (count + 1, window_start)

        url = f"{self.base_url}/coins/{symbol}/v1"
        headers = {"Authorization": f"Bearer {key}"}

        try:
            logger.debug(f"[LunarCrush] جاري جلب مشاعر {symbol}")
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After") if response.headers else None
                try:
                    cooldown_seconds = max(cooldown, int(float(retry_after))) if retry_after else cooldown
                except (TypeError, ValueError):
                    cooldown_seconds = cooldown
                self._key_cooldowns[key] = time.time() + cooldown_seconds
                # أعد العداد للحد الأقصى فوراً لهذا المفتاح
                self._key_counters[key] = (self._max_requests_per_min, window_start)
                logger.warning(
                    f"[LunarCrush] HTTP {response.status_code} (Rate Limit) — "
                    f"cooldown عام {cooldown_seconds}s — سأجرب المفتاح التالي."
                )
                return None

            if response.status_code == 402:
                self._symbol_cooldowns[(key, symbol)] = time.time() + symbol_cooldown
                logger.warning(
                    f"[LunarCrush] HTTP {response.status_code} للرمز {symbol} — "
                    f"cooldown خاص بالرمز {symbol_cooldown}s دون تعطيل المفتاح لبقية العملات."
                )
                return None

            if response.status_code == 404:
                logger.debug(f"[LunarCrush] {symbol} غير موجود في LunarCrush.")
                return None

            response.raise_for_status()
            data = response.json().get("data", {})

            if not data:
                return None

            # استخراج مؤشرات المشاعر
            def _number(value, default):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return float(default)

            galaxy_score  = _number(data.get("galaxy_score"), 50)
            social_volume = _number(data.get("social_volume"), 0)
            social_dom    = _number(data.get("social_dominance"), 0)
            sentiment_pct = max(0.0, min(_number(data.get("sentiment"), 50), 100.0))
            alt_rank      = _number(data.get("alt_rank"), 500)

            if social_volume == 0 and sentiment_pct == 50.0:
                logger.warning(
                    f"[LunarCrush] {symbol}: استجابة placeholder (Sentiment=50%, Volume=0) — "
                    "ستُعامل كبيانات غير متاحة."
                )
                return None

            logger.info(
                f"[LunarCrush] {symbol}: Galaxy={galaxy_score} | "
                f"Sentiment={sentiment_pct}% | Volume={social_volume:,} | AltRank=#{alt_rank}"
            )

            # اتجاه المشاعر يؤخذ من حقل sentiment نفسه. Galaxy Score وAltRank
            # مقاييس نشاط/جودة اجتماعية وليسا دليلاً على إيجابية أو سلبية الاتجاه.
            directional_confidence = abs(sentiment_pct - 50.0) / 50.0
            if sentiment_pct >= 60:
                label = "positive"
                score = round(directional_confidence, 2)
            elif sentiment_pct <= 40:
                label = "negative"
                score = round(directional_confidence, 2)
            else:
                label = "neutral"
                score = 0.0

            return {
                "label": label,
                "score": min(score, 1.0),
                "galaxy_score": galaxy_score,
                "social_volume": social_volume,
                "social_dominance": social_dom,
                "raw_sentiment_pct": sentiment_pct,
                "alt_rank": alt_rank,
                "source": "lunarcrush",
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"[LunarCrush] فشل الاتصال لـ {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"[LunarCrush] خطأ غير متوقع لـ {symbol}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────
    # احتياطي CoinMarketCap Fear & Greed
    # ─────────────────────────────────────────────────────────────────────
    def _cmc_fallback(self):
        """
        يجلب Fear & Greed Index من CoinMarketCap كمشاعر عامة للسوق
        عند تعطل كل مفاتيح LunarCrush.
        """
        import time
        now = time.time()

        # كاش لـ 15 دقيقة
        cached = self._cmc_fg_cache.get("fg")
        if cached and now - cached["time"] < self._cmc_fg_ttl:
            logger.debug("[CMC Fallback] إعادة Fear & Greed من الكاش.")
            return cached["data"]

        if not self._cmc_key:
            logger.debug("[CMC Fallback] لا يوجد COINMARKETCAP_API_KEY — إعادة neutral.")
            return self._neutral_result("no_cmc_key")

        try:
            r = requests.get(
                "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest",
                headers={"X-CMC_PRO_API_KEY": self._cmc_key},
                timeout=8,
            )
            if r.status_code != 200:
                logger.warning(f"[CMC Fallback] خطأ {r.status_code} من CMC Fear & Greed.")
                return self._neutral_result("cmc_error")

            fng = r.json().get("data", {})
            value = int(fng.get("value", 50))
            classification = fng.get("value_classification", "Neutral")

            # تحويل 0-100 لـ label/score
            if value >= 75:
                label, score = "positive", round(value / 100, 2)
            elif value >= 55:
                label, score = "positive", round(value / 120, 2)
            elif value <= 25:
                label, score = "negative", round((100 - value) / 100, 2)
            elif value <= 40:
                label, score = "negative", round((100 - value) / 140, 2)
            else:
                label, score = "neutral", 0.5

            result = {
                "label": label,
                "score": min(score, 1.0),
                "galaxy_score": value,          # نستخدم FNG كـ galaxy_score
                "social_volume": 0,
                "social_dominance": 0,
                "raw_sentiment_pct": value,
                "source": "cmc_fear_greed",
                "fear_greed_value": value,
                "fear_greed_label": classification,
            }

            logger.info(
                f"[CMC Fallback] Fear & Greed = {value} ({classification}) → "
                f"Sentiment: {label.upper()} (score={score:.2f})"
            )
            self._cmc_fg_cache["fg"] = {"time": now, "data": result}
            return result

        except Exception as e:
            logger.error(f"[CMC Fallback] فشل جلب Fear & Greed: {e}")
            return self._neutral_result("cmc_exception")

    def _neutral_result(self, source: str = "fallback"):
        return {
            "label": "neutral",
            "score": 0.5,
            "galaxy_score": 50,
            "social_volume": 0,
            "social_dominance": 0,
            "source": source,
        }
