"""
استراتيجية اصطياد القيعان الكبرى (Macro Bottom Fishing Strategy)
تستهدف قنص صفقات الشراء (LONG) من القيعان التاريخية والتشبعات البيعية الكبرى على الفريمات العالية (4H / 1D).
"""
import pandas as pd
import ta
from core.logger import logger
from core.config import Config
from strategies.smc_rsi_strategy import StrategyContext

class BottomFishingStrategy:
    def get_description(self) -> str:
        return "Macro Bottom Fishing & Trend Rider Strategy"

    def execute(self, context: StrategyContext) -> dict:
        """
        تقوم بفحص تشبعات الفريمات الكبيرة وتوليد إشارة شراء قاع ممتد.
        """
        # التحقق من تفعيل الاستراتيجية
        if not getattr(Config, 'BOTTOM_FISHING_ENABLED', True):
            return {"signal": "hold", "reason": "استراتيجية صيد القيعان معطلة في الإعدادات"}

        client = context.client
        if not client:
            return {"signal": "hold", "reason": "Exchange client غير متوفر في context"}

        symbol = context.symbol
        htf = getattr(Config, 'BOTTOM_FISHING_HTF', '4h')
        threshold = getattr(Config, 'BOTTOM_FISHING_RSI_THRESHOLD', 30.0)

        try:
            # جلب الشموع للفريم الكبير
            bars_htf = client.fetch_ohlcv(symbol, timeframe=htf, limit=50)
            if bars_htf is None or len(bars_htf) < 20:
                return {"signal": "hold", "reason": f"بيانات غير كافية للفريم الكبير {htf}"}

            # حساب الـ RSI
            close_series = pd.to_numeric(bars_htf['close'], errors='coerce')
            rsi_series = ta.momentum.RSIIndicator(close=close_series, window=14).rsi()

            if rsi_series.empty or pd.isna(rsi_series.iloc[-2]):
                return {"signal": "hold", "reason": f"فشل حساب RSI للفريم {htf}"}

            latest_rsi = float(rsi_series.iloc[-2]) # الشمعة المغلقة الأخيرة

            # التحقق من حدوث تشبع بيعي حاد (قاع)
            if latest_rsi <= threshold:
                # لتجنب السقوط الحر المطلق، نفضل أن نرى علامة ارتداد خفيفة على الفريم القصير (RSI 15m > 35 أو شمعة خضراء)
                rsi_15m = context.get_latest("rsi")
                close_15m = context.price
                
                # نحدد وقف خسارة آمن تحت أدنى قاع للشموع الـ 10 الأخيرة على الفريم الكبير
                low_series = pd.to_numeric(bars_htf['low'], errors='coerce')
                lowest_low = float(low_series.iloc[-10:].min())
                
                # وقف الخسارة تحت أدنى قاع بمسافة ATR للفريم الكبير
                atr_series = ta.volatility.AverageTrueRange(
                    high=pd.to_numeric(bars_htf['high'], errors='coerce'),
                    low=low_series,
                    close=close_series,
                    window=14
                ).average_true_range()
                
                safe_atr = float(atr_series.iloc[-2]) if not atr_series.empty else close_15m * 0.01
                sl_price = lowest_low - (safe_atr * 0.5)

                # التأكد من أن SL منطقي
                if sl_price >= close_15m or sl_price <= 0:
                    sl_price = close_15m * 0.97 # ستوب 3% افتراضي إذا حدث خطأ

                logger.info(f"🔮 [Bottom Fishing] {symbol}: تم توليد إشارة شراء قاع! HTF RSI={latest_rsi:.2f} <= {threshold}")
                
                # نحدد المعطيات الإضافية في ta_data ليستخدمها RiskManager في تحديد الـ SL
                context.ta_data["invalidation_level"] = sl_price
                context.ta_data["bottom_fishing_active"] = True
                context.ta_data["bottom_fishing_rsi"] = latest_rsi

                return {
                    "signal": "buy",
                    "reason": f"Macro Bottom Fishing (HTF {htf} RSI={latest_rsi:.1f} <= {threshold})",
                    "ta_data": context.ta_data
                }

        except Exception as e:
            logger.warning(f"⚠️ [Bottom Fishing] خطأ أثناء معالجة {symbol}: {e}")

        return {"signal": "hold", "reason": "لا يوجد تشبع بيعي في الفريم الماكرو"}
