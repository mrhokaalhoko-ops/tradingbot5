"""
Candle-Boundary Sync — بديل time.sleep() الذكي
يضمن أن البوت يبدأ دورة المسح بعد إغلاق الشمعة بـ 10 ثوانٍ بالضبط.
"""
import time
from core.logger import logger


def seconds_to_next_candle(
    interval_seconds: int,
    buffer_seconds: int = 10,
    now_ts: float = None,
) -> float:
    """
    يحسب الثواني اللازمة للانتظار حتى حد إغلاق الشمعة التالي + buffer.

    مثال مع interval=900 (15m):
      إذا الوقت الآن 14:07:23 → الشمعة القادمة تغلق في 14:15:00
      الانتظار = (14:15:10) - (14:07:23) = 467 ثانية

    لماذا هذا أفضل من time.sleep(900)؟
      - time.sleep(900) يبدأ العد من لحظة انتهاء المسح (متأخر أو متقدم)
      - هذه الدالة تحسب المتبقي حتى إغلاق منضبط على ساعة المنصة
      - يقضي على مشكلة Look-Ahead Bias (قراءة شمعة لم تُغلق بعد)
    """
    interval_seconds = int(interval_seconds)
    buffer_seconds = int(buffer_seconds)
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if buffer_seconds < 0:
        raise ValueError("buffer_seconds cannot be negative")

    now = float(time.time() if now_ts is None else now_ts)
    current_boundary = (int(now) // interval_seconds) * interval_seconds
    wake_at = current_boundary + buffer_seconds

    # إذا تجاوزنا لحظة buffer الخاصة بالشمعة الحالية، ننتظر إغلاق التالية.
    # لا نفرض حداً أدنى اعتباطياً لأنه يكسر محاذاة حد الشمعة قرب الإغلاق.
    if wake_at <= now:
        wake_at = current_boundary + interval_seconds + buffer_seconds

    wait = wake_at - now
    next_dt = time.strftime("%H:%M:%S", time.localtime(wake_at))
    logger.info(
        f"[CandleSync] الشمعة التالية تغلق في {next_dt} | "
        f"الانتظار: {wait:.0f}s ({wait/60:.1f} دقيقة)"
    )
    return wait
