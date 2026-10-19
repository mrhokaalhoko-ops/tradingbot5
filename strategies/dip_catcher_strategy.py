"""
Dip Catcher Strategy — Python
ترجمة من dip_catcher.ts
يصيد الانهيارات (Dips) عند تقاطع HMA مع البولينجر باند،
مع فلتر Ichimoku للاتجاه العام.

Long:  HMA(low) يتجاوز BB Lower من أسفل + Ichimoku صاعد
Short: HMA(high) يتجاوز BB Upper من أعلى + Ichimoku هابط
Close: التقاطع العكسي
"""
from strategies.smc_rsi_strategy import StrategyContext


class DipCatcherStrategy:

    def get_description(self) -> str:
        return "Dip Catcher: HMA/BB Crossover + Ichimoku Trend Filter"

    def execute(self, context: StrategyContext) -> dict:
        df         = context.bars_df
        last_signal = context.last_signal

        empty = {"signal": "hold", "reason": "DipCatcher: بيانات غير كافية", "ta_data": {}}

        if df is None or len(df) < 30:
            return empty

        # ── نحتاج على الأقل آخر شمعتين مغلقتين ───────────────────
        # نستخدم [-3] و [-2] (شمعتان مغلقتان) بدلاً من [-1] الحية
        try:
            hma_low_0  = float(df['hma_9_low'].iloc[-2])
            hma_low_1  = float(df['hma_9_low'].iloc[-3])
            hma_high_0 = float(df['hma_9_high'].iloc[-2])
            hma_high_1 = float(df['hma_9_high'].iloc[-3])
            bb_lower_0 = float(df['bb_lower'].iloc[-2])
            bb_lower_1 = float(df['bb_lower'].iloc[-3])
            bb_upper_0 = float(df['bb_upper'].iloc[-2])
            bb_upper_1 = float(df['bb_upper'].iloc[-3])
            hma_0      = float(df['hma_9'].iloc[-2])
            span_b_0   = float(df['ichi_span_b'].iloc[-2])
            is_long_trend = bool(df['ichi_bullish'].iloc[-2])
        except (KeyError, IndexError):
            return empty

        ta_data = {
            "hma_low":   round(hma_low_0,  4),
            "hma_high":  round(hma_high_0, 4),
            "bb_lower":  round(bb_lower_0, 4),
            "bb_upper":  round(bb_upper_0, 4),
            "ichi_span_b": round(span_b_0, 4),
            "ichi_trend": "bullish" if is_long_trend else "bearish",
        }

        is_flat = last_signal is None or last_signal == "close"

        # ── تقاطع HMA(low) فوق BB Lower (إشارة شراء) ───────────────
        low_cross_up = hma_low_0 > bb_lower_0 and hma_low_1 < bb_lower_1

        # ── تقاطع HMA(high) تحت BB Upper (إشارة بيع/إغلاق) ─────────
        high_cross_down = hma_high_0 < bb_upper_0 and hma_high_1 > bb_upper_1

        if low_cross_up:
            if is_flat and is_long_trend:
                return {
                    "signal": "buy",
                    "reason": (
                        f"DipCatcher Long: HMA(low) تجاوز BB Lower صعوداً "
                        f"| Ichimoku صاعد (SpanB={span_b_0:.4f})"
                    ),
                    "ta_data": ta_data,
                }
            elif not is_flat:
                return {
                    "signal": "close",
                    "reason": "DipCatcher: تقاطع HMA/BB Lower → إغلاق مركز قائم",
                    "ta_data": ta_data,
                }

        if high_cross_down:
            if is_flat and not is_long_trend:
                return {
                    "signal": "sell",
                    "reason": (
                        f"DipCatcher Short: HMA(high) تجاوز BB Upper هبوطاً "
                        f"| Ichimoku هابط (SpanB={span_b_0:.4f})"
                    ),
                    "ta_data": ta_data,
                }
            elif not is_flat:
                return {
                    "signal": "close",
                    "reason": "DipCatcher: تقاطع HMA/BB Upper → إغلاق مركز قائم",
                    "ta_data": ta_data,
                }

        return {
            "signal": "hold",
            "reason": "DipCatcher: لا يوجد تقاطع HMA/BB",
            "ta_data": ta_data,
        }
