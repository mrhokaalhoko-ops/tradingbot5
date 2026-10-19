from core.logger import logger

class HybridStrategy:
    """
    استراتيجية الفيتو المتقدمة (Advanced Veto Architecture)
    لا يحق للذكاء الاصطناعي بدء صفقة إذا كان التحليل الفني محايداً.
    دوره فقط تقليل المخاطرة أو المنع (Veto) في حال وجود أخبار قوية.
    """
    def __init__(self):
        self.veto_threshold         = 0.70          # الحد الذي يمنع الصفقة تماماً
        self.risk_reduce_threshold  = 0.55          # الحد الذي يقلل حجم الصفقة
        self.risk_multiplier_full   = 0.5           # معامل المخاطرة للوضع الكامل عند التحذير
        self.risk_multiplier_weight = 0.7           # معامل المخاطرة للوضع الثانوي عند التحذير

    def decide(self, ta_signal: str, sentiment: dict, ta_data: dict = None, sentiment_mode: str = "full"):
        # احفظ المصدر الأصلي قبل التطبيع (لأن _normalize_sentiment لا تُمرر source)
        raw_sentiment = sentiment if isinstance(sentiment, dict) else {}
        sentiment = self._normalize_sentiment(sentiment)
        # أعد حقن source الأصلي في sentiment المُطبَّع لاستخدامه في فحوصات لاحقة
        sentiment["source"] = raw_sentiment.get("source", "")
        
        label = sentiment["label"]
        score = sentiment["score"]

        decision = {
            "action": "hold",
            "risk_multiplier": 1.0,
            "reason": ""
        }

        # 0. التحقق من صحة صيغة وضع المشاعر
        VALID_MODES = {"full", "ta_only", "weight"}
        if sentiment_mode not in VALID_MODES:
            logger.warning(f"[HybridStrategy] وضع غير معروف: '{sentiment_mode}'. سيتم استخدام 'full'.")
            sentiment_mode = "full"

        # 1. فلتر الفيتو الفني (لا بيانات = لا صفقة)
        if not ta_data:
            decision["reason"] = "بيانات فنية مفقودة، تم إلغاء التنفيذ كإجراء وقائي."
            return decision

        # 2. الفيتو الذكي (لا SMC = لا دخول)
        if ta_signal not in ["buy", "sell"]:
            decision["reason"] = "لم يتم تأكيد الدخول من محرك SMC. (الذكاء الاصطناعي ممنوع من بدء الصفقات)."
            return decision

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # وضع معطّل: يتجاهل المشاعر ويعمل بالفني فقط
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if sentiment_mode == "ta_only":
            decision["action"] = ta_signal
            decision["reason"] = f"[فني فقط] إشارة SMC: {ta_signal.upper()}. المشاعر معطّلة."
            return decision

        # الوضع الكامل يفشل بأمان عند غياب بيانات خاصة وموثوقة للعملة.
        unreliable_sources = {
            "",
            "lunarcrush_429_cooldown",
            "lunarcrush_429",
            "lunarcrush_rate_limited_proactive",
            "no_cmc_key",
            "cmc_error",
            "cmc_exception",
            "cmc_fear_greed",
            "fallback",
        }
        if (
            sentiment_mode == "full"
            and sentiment.get("source", "") in unreliable_sources
        ):
            decision["action"] = "hold"
            decision["risk_multiplier"] = 0.0
            decision["reason"] = (
                "رفض وقائي: لا توجد بيانات مشاعر موثوقة خاصة بالعملة "
                f"(source={sentiment.get('source') or 'missing'}) — "
                "الوضع full يشترط بيانات حقيقية."
            )
            return decision

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # وضع ثانوي: المشاعر تعدّل الحجم فقط ولا تمنع الدخول
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if sentiment_mode == "weight":
            decision["action"] = ta_signal
            if ta_signal == "buy" and label == "negative" and score >= self.risk_reduce_threshold:
                decision["risk_multiplier"] = self.risk_multiplier_weight
                decision["reason"] = f"[ثانوي] شراء مسموح، خُفّض الحجم {int((1 - self.risk_multiplier_weight) * 100)}% بسبب مشاعر سلبية (score={score:.2f})."
            elif ta_signal == "sell" and label == "positive" and score >= self.risk_reduce_threshold:
                decision["risk_multiplier"] = self.risk_multiplier_weight
                decision["reason"] = f"[ثانوي] بيع مسموح، خُفّض الحجم {int((1 - self.risk_multiplier_weight) * 100)}% بسبب مشاعر إيجابية (score={score:.2f})."
            else:
                decision["reason"] = f"[ثانوي] {ta_signal.upper()} مؤكد. المشاعر: {label} (score={score:.2f})."
            return decision

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # وضع كامل (الافتراضي): المشاعر شرط في القرار
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 3. معالجة الشراء (LONG)
        if ta_signal == "buy":
            # فيتو إضافي متماثل: مشاعر إيجابية جداً = احتمال قمة (FOMO)
            if label == "positive" and score >= self.veto_threshold:
                decision["action"] = "hold"
                decision["risk_multiplier"] = 0.0
                decision["reason"] = f"تم نقض الشراء (Veto): مشاعر إيجابية مفرطة (score={score:.2f}) — خطر FOMO / قمة."
                return decision

            if label == "negative" and score >= self.veto_threshold:
                decision["action"] = "hold"
                decision["risk_multiplier"] = 0.0
                decision["reason"] = "تم نقض الشراء (Veto): أخبار سلبية قوية جداً."
                return decision

            if label == "negative" and score >= self.risk_reduce_threshold:
                decision["action"] = "buy"
                decision["risk_multiplier"] = self.risk_multiplier_full
                decision["reason"] = f"شراء مسموح، لكن تم خفض حجم الصفقة {int((1 - self.risk_multiplier_full) * 100)}% بسبب أخبار سلبية."
                return decision

            decision["action"] = "buy"
            if label == "negative":
                decision["reason"] = f"شراء مؤكد: مشاعر سلبية ضعيفة جداً (score={score:.2f}) لا تكفي للفيتو."
            elif label == "neutral":
                decision["reason"] = "شراء مؤكد: مشاعر محايدة من مصدر موثوق."
            else:
                decision["reason"] = f"شراء مؤكد: مشاعر إيجابية (score={score:.2f})."
            return decision


        # 4. معالجة البيع (SHORT)
        if ta_signal == "sell":
            # فيتو إضافي متماثل: مشاعر سلبية جداً = احتمال قاع (Panic)
            if label == "negative" and score >= self.veto_threshold:
                decision["action"] = "hold"
                decision["risk_multiplier"] = 0.0
                decision["reason"] = f"تم نقض البيع (Veto): مشاعر سلبية مفرطة (score={score:.2f}) — خطر Panic Selling / قاع."
                return decision

            if label == "positive" and score >= self.veto_threshold:
                decision["action"] = "hold"
                decision["risk_multiplier"] = 0.0
                decision["reason"] = "تم نقض البيع (Veto): أخبار إيجابية قوية جداً."
                return decision

            if label == "positive" and score >= self.risk_reduce_threshold:
                decision["action"] = "sell"
                decision["risk_multiplier"] = self.risk_multiplier_full
                decision["reason"] = f"بيع مسموح، لكن تم خفض حجم الصفقة {int((1 - self.risk_multiplier_full) * 100)}% بسبب أخبار إيجابية."
                return decision

            decision["action"] = "sell"
            if label == "positive":
                decision["reason"] = f"بيع مؤكد: مشاعر إيجابية ضعيفة جداً (score={score:.2f}) لا تكفي للفيتو."
            elif label == "neutral":
                decision["reason"] = "بيع مؤكد: مشاعر محايدة من مصدر موثوق."
            else:
                decision["reason"] = f"بيع مؤكد: مشاعر سلبية (score={score:.2f})."
            return decision


        return decision

    def _normalize_sentiment(self, sentiment):
        """تأكيد صحة كائن المشاعر ومطابقته للمعايير"""
        if not sentiment:
            return {"label": "neutral", "score": 0.0}

        label = str(sentiment.get("label", "neutral")).lower().strip()
        
        # دعم galaxy_score من LunarCrush (0-100) و score من Groq (0.0-1.0)
        raw_score = sentiment.get("score") or sentiment.get("galaxy_score") or 0.0
        score = float(raw_score or 0.0)
        
        # تطبيع: إذا كان أكبر من 1 نفترض أنه مقياس 0-100
        if score > 1.0:
            score = score / 100.0

        score = max(0.0, min(score, 1.0))

        if label not in ["positive", "negative", "neutral"]:
            label = "neutral"

        return {
            "label": label,
            "score": score
        }
