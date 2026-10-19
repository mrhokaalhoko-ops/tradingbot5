"""
OBV Pump/Dump Strategy — Python
ترجمة من obv_pump_dump.ts
يكتشف تدفق الحجم الاستثنائي (Pump) مقارنةً بالمتوسط التاريخي،
مع فلتر EMA200 للتأكد من الاتجاه الصاعد.
Long Only — لا يعطي إشارة بيع.
"""
import time
from strategies.smc_rsi_strategy import StrategyContext


class OBVPumpDumpStrategy:

    def __init__(
        self,
        trigger_multiplier: float = 2.0,
        trigger_time_windows: int = 3,
        ema_length: int = 200,
    ):
        self.trigger_multiplier    = trigger_multiplier
        self.trigger_time_windows  = trigger_time_windows
        self.ema_length            = ema_length

    def get_description(self) -> str:
        return f"OBV Pump/Dump Detection (multiplier={self.trigger_multiplier}x, EMA{self.ema_length})"

    def execute(self, context: StrategyContext, state_manager=None) -> dict:
        df    = context.bars_df
        price = context.price

        empty = {"signal": "hold", "reason": "OBV: بيانات غير كافية", "ta_data": {}}

        if df is None or len(df) < 25:
            return empty

        # ── جلب مصفوفات OBV و EMA ───────────────────────────────────
        obv_series = df['obv'].dropna()
        ema_series = df['ema_200'].dropna()

        if len(obv_series) <= 20 or len(ema_series) == 0:
            return empty

        ema   = float(ema_series.iloc[-1])
        n     = self.trigger_time_windows
        obvArr = obv_series.tolist()

        ta_data = {
            "obv_current": round(obvArr[-1]),
            "ema_200":     round(ema, 4),
        }

        # ── فلتر الاتجاه: Long Only عندما السعر فوق EMA200 ──────────
        if price <= ema:
            return {
                "signal": "hold",
                "reason": f"OBV: السعر ({price:.4f}) تحت EMA200 ({ema:.4f}) — Downtrend",
                "ta_data": ta_data,
            }

        # ── حساب متوسط OBV الحالي (آخر n شمعة) ─────────────────────
        current_avg = sum(obvArr[-n:]) / n

        # ── حساب أعلى متوسط OBV تاريخي (نفس النافذة قبل آخر n شمعة) ──
        end_idx   = len(obvArr) - n
        start_idx = max(0, end_idx - 20)
        before    = obvArr[start_idx:end_idx]

        if not before:
            return empty

        sorted_before = sorted(before, reverse=True)
        top_n         = sorted_before[:n]
        highest_avg   = sum(top_n) / len(top_n)

        ta_data["current_avg"]  = round(current_avg)
        ta_data["highest_avg"]  = round(highest_avg)

        if highest_avg == 0 or current_avg <= highest_avg:
            return {
                "signal": "hold",
                "reason": f"OBV: لم يتجاوز الحجم الحالي الذروة التاريخية",
                "ta_data": ta_data,
            }

        difference = abs(current_avg / highest_avg)
        ta_data["obv_ratio"] = round(difference, 2)

        if difference >= self.trigger_multiplier:
            # ── فحص freshness: هل زخم OBV يتسارع أم يتباطأ؟ ──────────────────
            lookback = 5
            if len(obvArr) > lookback * 2 + 1:
                recent_delta = obvArr[-1] - obvArr[-(lookback + 1)]
                older_delta  = obvArr[-(lookback + 1)] - obvArr[-(lookback * 2 + 1)]
                # نتجاهل الفحص إذا كان older_delta صفراً أو قريباً جداً (تجنب القسمة على صفر)
                if abs(older_delta) > 0:
                    acceleration = recent_delta / abs(older_delta)
                    ta_data["obv_acceleration"] = round(acceleration, 2)
                    # إذا كان الزخم يتباطأ (التسارع أقل من 1.2x) → الـ spike قديم وليس طازجاً
                    if acceleration < 1.2:
                        return {
                            "signal": "hold",
                            "reason": (
                                f"OBV: الزخم يتباطأ — acceleration={acceleration:.2f}x "
                                f"(recent_delta={recent_delta:.0f}, older_delta={older_delta:.0f})"
                            ),
                            "ta_data": ta_data,
                        }

            # ── cooldown: منع إشارة جديدة قبل مرور 30 دقيقة ────────────────
            if state_manager is not None:
                last_signal_time = state_manager.get_obv_signal_time(context.symbol)
                elapsed_minutes  = (time.time() - last_signal_time) / 60
                if elapsed_minutes < 30:
                    return {
                        "signal": "hold",
                        "reason": f"OBV: cooldown نشط — مرّ {elapsed_minutes:.1f} دقيقة من أصل 30",
                        "ta_data": ta_data,
                    }
                # سجّل وقت الإشارة الجديدة
                state_manager.set_obv_signal_time(context.symbol, time.time())

            return {
                "signal": "buy",
                "reason": (
                    f"OBV Pump مكتشف: الحجم الحالي {difference:.2f}x "
                    f"أعلى من الذروة التاريخية | EMA200={ema:.4f}"
                ),
                "ta_data": ta_data,
            }

        return {
            "signal": "hold",
            "reason": f"OBV: النسبة {difference:.2f}x أقل من الحد ({self.trigger_multiplier}x)",
            "ta_data": ta_data,
        }
