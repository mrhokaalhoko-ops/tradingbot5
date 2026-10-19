import pandas as pd
import ta
import numpy as np
from cachetools import TTLCache
from core.logger import logger
from core.config import Config
from strategy.market_context import analyze_market_context_4h
from strategy.setup_context import analyze_setup_context_1h, select_setup_side

# ═══════════════════════════════════════════════════════════════
# دوال التحليل الزمني — مستوحاة من ta.ts
# تقيّم آخر 3-5 شموع بدلاً من الشمعة الحالية فقط
# ═══════════════════════════════════════════════════════════════

def get_trending_direction(lookbacks: list) -> str:
    """
    يقارن آخر قيمة بمتوسط الثلاث قيم التي سبقتها.
    مستوحى من getTrendingDirection في ta.ts
    """
    if len(lookbacks) < 4:
        return "unknown"
    current = lookbacks[-1]
    avg_prev = (lookbacks[-2] + lookbacks[-3] + lookbacks[-4]) / 3
    return "down" if avg_prev > current else "up"


def get_trending_direction_last(lookbacks: list) -> str:
    """
    يقارن آخر شمعتين فقط.
    مستوحى من getTrendingDirectionLastItem في ta.ts
    """
    if len(lookbacks) < 2:
        return "unknown"
    return "down" if lookbacks[-2] > lookbacks[-1] else "up"


def get_macro_trending_direction(lookbacks: list, short_window: int = 3, long_window: int = 10) -> dict:
    """
    نسخة محسّنة تدمج نافذتين: قصيرة (ضجيج لحظي) وطويلة (اتجاه كلي).
    - النافذة القصيرة: حساسة وسريعة الاستجابة للانعكاسات.
    - النافذة الطويلة: تمثل الترند الكلي، أقل تأثراً بالضجيج.
    - الإشارة قوية فقط عندما يتفق الاتجاهان.
    """
    if len(lookbacks) < long_window + 1:
        return {"direction": "unknown", "strength": "weak", "aligned": False}

    current = lookbacks[-1]

    avg_short = sum(lookbacks[-(short_window + 1):-1]) / short_window
    short_dir = "down" if avg_short > current else "up"

    avg_long = sum(lookbacks[-(long_window + 1):-1]) / long_window
    long_dir = "down" if avg_long > current else "up"

    aligned = (short_dir == long_dir)

    return {
        "direction": long_dir,       # الاتجاه الكلي هو المرجع الرئيسي
        "short_term": short_dir,     # الاتجاه اللحظي (مفيد لاصطياد الانعكاسات)
        "strength": "strong" if aligned else "weak",
        "aligned": aligned,
    }




def get_crossed_since(lookbacks: list):
    """
    يكتشف منذ كم شمعة تجاوز المؤشر خط الصفر.
    مستوحى من getCrossedSince في ta.ts
    
    مثال الاستخدام: RSI تجاوز 50 أو MACD تجاوز الصفر منذ كم شمعة؟
    مفيد لتقييم قوة وعمر الإشارة بدلاً من فحص الشمعة الحالية فقط.
    """
    if len(lookbacks) < 2:
        return None
    values = list(reversed(lookbacks))
    current = values[0]
    for i in range(1, len(values) - 1):
        # تحقق من تجاوز الصفر في كلا الاتجاهين
        if (current < 0 and values[i] > 0) or (current >= 0 and values[i] < 0):
            return i
    return None

# ---------------------------------------------------------------
# HTF Trend Cache: نخزن الترند العلوي مؤقتاً لتجنب استدعاءات API متكررة
# ---------------------------------------------------------------
HTF_CACHE_TTL_SECONDS = 900  # 15 دقيقة = عمر الشمعة الـ 1H
_htf_cache = TTLCache(maxsize=512, ttl=HTF_CACHE_TTL_SECONDS)
_context_4h_cache = TTLCache(maxsize=512, ttl=3600)

class TAEngine:
    def __init__(self):
        pass

    def add_indicators(self, df: pd.DataFrame, filter_profile: dict = None):
        """
        يضيف مؤشرات التحليل الفني ومفاهيم المال الذكي (SMC) المتقدمة.
        """
        if df is None or df.empty:
            logger.warning("لا توجد بيانات كافية لحساب المؤشرات.")
            return df

        # ── حماية: عملات جديدة جداً قد تُرجع شموعاً أقل من الحد الأدنى اللازم لحساب
        # RSI/ADX (window=14) بأمان دون رفع استثناء — يمنع تكرار ERROR لكل دورة
        # (تأكيد من اللوج: "index 14 is out of bounds for axis 0 with size 12" × 636 لـ G/USDT)
        MIN_ROWS_FOR_INDICATORS = 30
        if len(df) < MIN_ROWS_FOR_INDICATORS:
            logger.debug(
                f"[add_indicators] عدد الشموع ({len(df)}) أقل من الحد الأدنى "
                f"({MIN_ROWS_FOR_INDICATORS}) لحساب المؤشرات بأمان — تخطي الحساب."
            )
            return df

        try:
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
                
            # --- المؤشرات الكلاسيكية الأساسية ---
            df['rsi'] = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi()
            macd = ta.trend.MACD(close=df['close'])
            df['macd'] = macd.macd()
            df['macd_signal'] = macd.macd_signal()
            df['ema_50'] = ta.trend.EMAIndicator(close=df['close'], window=50).ema_indicator()
            df['ema_100'] = ta.trend.EMAIndicator(close=df['close'], window=100).ema_indicator()
            df['ema_200'] = ta.trend.EMAIndicator(close=df['close'], window=200).ema_indicator()
            df['atr'] = ta.volatility.AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
            df['adx'] = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14).adx().clip(upper=99.0)

            # مقارنة الشمعة الحالية بماضٍ متاح عند بدايتها فقط. shift(1)
            # يمنع الشمعة نفسها من تخفيض RVOL الخاص بها داخل المقام.
            df['volume_avg_20'] = df['volume'].shift(1).rolling(20).mean()
            df['volume_median_20'] = df['volume'].shift(1).rolling(20).median()
            df['rvol_20'] = (
                df['volume'] / df['volume_avg_20'].replace(0, np.nan)
            )
            
            # --- مؤشرات إضافية للاستراتيجيات المتقدمة ---
            
            # OBV (On-Balance Volume) — لاستراتيجية OBV Pump/Dump
            df['obv'] = ta.volume.OnBalanceVolumeIndicator(
                close=df['close'], volume=df['volume']
            ).on_balance_volume()

            # HMA (Hull Moving Average) — لاستراتيجية Dip Catcher
            # HMA = WMA(2*WMA(n/2) − WMA(n), sqrt(n))
            def _hma(series: pd.Series, period: int) -> pd.Series:
                half   = max(1, period // 2)
                sqrtp  = max(1, int(round(period ** 0.5)))
                wma_h  = series.ewm(span=half,   adjust=False).mean()   # تقريب WMA بـ EWM
                wma_f  = series.ewm(span=period, adjust=False).mean()
                diff   = 2 * wma_h - wma_f
                return diff.ewm(span=sqrtp, adjust=False).mean()

            df['hma_9']        = _hma(df['close'], 9)
            df['hma_9_high']   = _hma(df['high'],  9)   # hma_high لـ dip_catcher
            df['hma_9_low']    = _hma(df['low'],   9)   # hma_low لـ dip_catcher

            # Bollinger Bands — مطلوبة لـ Dip Catcher (موجودة إن كانت، هنا نضمنها)
            bb_ind = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
            df['bb_upper'] = bb_ind.bollinger_hband()
            df['bb_lower'] = bb_ind.bollinger_lband()
            df['bb_mid']   = bb_ind.bollinger_mavg()

            # Ichimoku Cloud — لاستراتيجية Dip Catcher (trend filter)
            # conversion=9, base=26, span=52, displacement=26 × multiplier=4
            ich = ta.trend.IchimokuIndicator(
                high=df['high'], low=df['low'],
                window1=9, window2=26, window3=52
            )
            df['ichi_conversion'] = ich.ichimoku_conversion_line()
            df['ichi_base']       = ich.ichimoku_base_line()
            df['ichi_span_a']     = ich.ichimoku_a()
            df['ichi_span_b']     = ich.ichimoku_b()
            # الترند: السعر فوق سحابة B = صاعد، تحتها = هابط
            df['ichi_bullish'] = df['close'] > df['ichi_span_b']
            df['ichi_bearish'] = df['close'] < df['ichi_span_b']
            
            # --- إعدادات مفاهيم المال الذكي (SMC) ---
            # 1. تحليل حجم الشمعة (Displacement)
            df['body'] = (df['close'] - df['open']).abs()
            df['range'] = (df['high'] - df['low']).replace(0, np.nan)
            df['body_avg_20'] = df['body'].rolling(20).mean()
            
            # 2. هيكل السوق (Swing High / Swing Low)
            lookback = 5
            df['prior_swing_high'] = df['high'].shift(1).rolling(lookback).max()
            df['prior_swing_low'] = df['low'].shift(1).rolling(lookback).min()
            
            # ------------------------------------------------------------
            # Liquidity Sweep Detection
            # ------------------------------------------------------------
            liq_lookback = getattr(Config, "LIQUIDITY_SWEEP_LOOKBACK", 10)
            sweep_recent_bars = getattr(Config, "LIQUIDITY_SWEEP_RECENT_BARS", 8)

            # قاع/قمة سابقة قبل الشمعة الحالية
            df["liq_prior_low"] = df["low"].shift(1).rolling(liq_lookback).min()
            df["liq_prior_high"] = df["high"].shift(1).rolling(liq_lookback).max()

            # Sweep للسيولة السفلية: كسر قاع سابق ثم إغلاق فوقه
            df["sell_side_sweep"] = (
                (df["low"] < df["liq_prior_low"]) &
                (df["close"] > df["liq_prior_low"])
            )

            # Sweep للسيولة العلوية: كسر قمة سابقة ثم إغلاق تحتها
            df["buy_side_sweep"] = (
                (df["high"] > df["liq_prior_high"]) &
                (df["close"] < df["liq_prior_high"])
            )

            # نطلب أن يكون السويب قريبًا من شمعة الـ displacement وليس قديمًا جدًا
            df["recent_sell_side_sweep"] = (
                df["sell_side_sweep"]
                .shift(1)
                .rolling(sweep_recent_bars)
                .max()
                .fillna(0)
                .astype(bool)
            )

            df["recent_buy_side_sweep"] = (
                df["buy_side_sweep"]
                .shift(1)
                .rolling(sweep_recent_bars)
                .max()
                .fillna(0)
                .astype(bool)
            )

            # 3. شروط الزخم والاندفاع (Displacement)
            df['strong_body'] = df['body'] > df['body_avg_20'] * 1.5
            df['close_near_high'] = ((df['close'] - df['low']) / df['range']) > 0.7
            df['close_near_low'] = ((df['high'] - df['close']) / df['range']) > 0.7
            
            # 4. تحديد أنواع الشموع السابقة
            prev_bearish = df['close'].shift(1) < df['open'].shift(1)
            prev_bullish = df['close'].shift(1) > df['open'].shift(1)
            
            # 5. كسر الهيكل (Break of Structure - BOS)
            bullish_bos = df['close'] > df['prior_swing_high']
            bearish_bos = df['close'] < df['prior_swing_low']
            
            # 6. كتل الأوامر (Order Blocks)
            profile = filter_profile or {}
            require_sweep = bool(profile.get(
                "require_sweep",
                getattr(Config, "REQUIRE_LIQUIDITY_SWEEP_FOR_OB", True),
            ))
            require_bos = bool(profile.get("require_bos", True))
            bullish_bos_gate = bullish_bos if require_bos else pd.Series(True, index=df.index)
            bearish_bos_gate = bearish_bos if require_bos else pd.Series(True, index=df.index)

            base_bullish_ob_cond = (
                prev_bearish &
                (df["close"] > df["open"]) &
                df["strong_body"] &
                df["close_near_high"] &
                bullish_bos_gate
            )

            base_bearish_ob_cond = (
                prev_bullish &
                (df["close"] < df["open"]) &
                df["strong_body"] &
                df["close_near_low"] &
                bearish_bos_gate
            )

            if require_sweep:
                bullish_ob_cond = base_bullish_ob_cond & df["recent_sell_side_sweep"]
                bearish_ob_cond = base_bearish_ob_cond & df["recent_buy_side_sweep"]
            else:
                bullish_ob_cond = base_bullish_ob_cond
                bearish_ob_cond = base_bearish_ob_cond
            
            # 7. تخزين مناطق الـ Order Blocks (Zones)
            df['ob_bullish_low'] = np.nan
            df['ob_bullish_high'] = np.nan
            df['ob_bearish_low'] = np.nan
            df['ob_bearish_high'] = np.nan
            
            df.loc[bullish_ob_cond, 'ob_bullish_low'] = df['low'].shift(1)
            df.loc[bullish_ob_cond, 'ob_bullish_high'] = df['open'].shift(1)
            
            df.loc[bearish_ob_cond, 'ob_bearish_low'] = df['open'].shift(1)
            df.loc[bearish_ob_cond, 'ob_bearish_high'] = df['high'].shift(1)
            
            # التعبئة للأمام
            df['ob_bullish_low'] = df['ob_bullish_low'].ffill()
            df['ob_bullish_high'] = df['ob_bullish_high'].ffill()
            df['ob_bearish_low'] = df['ob_bearish_low'].ffill()
            df['ob_bearish_high'] = df['ob_bearish_high'].ffill()
            
            # Mid levels
            df['ob_bullish_mid'] = (df['ob_bullish_low'] + df['ob_bullish_high']) / 2
            df['ob_bearish_mid'] = (df['ob_bearish_low'] + df['ob_bearish_high']) / 2
            
            # 8. الفجوات السعرية (Fair Value Gaps - FVG)
            df['fvg_bullish_low'] = np.nan
            df['fvg_bullish_high'] = np.nan
            fvg_bullish_cond = df['low'] > df['high'].shift(2)
            df['fvg_bullish_created'] = fvg_bullish_cond.fillna(False)
            df.loc[fvg_bullish_cond, 'fvg_bullish_low'] = df['high'].shift(2)
            df.loc[fvg_bullish_cond, 'fvg_bullish_high'] = df['low']
            
            df['fvg_bearish_low'] = np.nan
            df['fvg_bearish_high'] = np.nan
            fvg_bearish_cond = df['high'] < df['low'].shift(2)
            df['fvg_bearish_created'] = fvg_bearish_cond.fillna(False)
            df.loc[fvg_bearish_cond, 'fvg_bearish_low'] = df['high']
            df.loc[fvg_bearish_cond, 'fvg_bearish_high'] = df['low'].shift(2)
            
            df['fvg_bullish_low'] = df['fvg_bullish_low'].ffill()
            df['fvg_bullish_high'] = df['fvg_bullish_high'].ffill()
            df['fvg_bearish_low'] = df['fvg_bearish_low'].ffill()
            df['fvg_bearish_high'] = df['fvg_bearish_high'].ffill()

            # --- OB Age & Mitigation Tracking ---
            # ✅ إصلاح: نتتبع عمر الـ OB ونضع علامة Mitigated
            # عندما يُغلق السعر داخل منطقة الـ OB (أو خلفها) تُعتبر مستهلكة ولا تُطلق إشارة جديدة
            df['bullish_ob_created'] = bullish_ob_cond
            df['bearish_ob_created'] = bearish_ob_cond

            ages_bullish = []
            ages_bearish = []
            mitigated_bullish = []
            mitigated_bearish = []
            last_bull_idx = None
            last_bear_idx = None
            bull_mitigated = False
            bear_mitigated = False

            for i in range(len(df)):
                close_i = df['close'].iloc[i]

                # Bullish OB
                if df['bullish_ob_created'].iloc[i]:
                    last_bull_idx = i
                    bull_mitigated = False  # OB جديد = غير مستهلك
                    ages_bullish.append(0)
                elif last_bull_idx is not None:
                    ages_bullish.append(i - last_bull_idx)
                    # إذا أغلق السعر تحت أدنى نقطة في منطقة الـ OB → مستهلك
                    ob_low = df['ob_bullish_low'].iloc[i]
                    if pd.notna(ob_low) and close_i < ob_low:
                        bull_mitigated = True
                else:
                    ages_bullish.append(np.nan)
                mitigated_bullish.append(bull_mitigated)

                # Bearish OB
                if df['bearish_ob_created'].iloc[i]:
                    last_bear_idx = i
                    bear_mitigated = False  # OB جديد = غير مستهلك
                    ages_bearish.append(0)
                elif last_bear_idx is not None:
                    ages_bearish.append(i - last_bear_idx)
                    # إذا أغلق السعر فوق أعلى نقطة في منطقة الـ OB → مستهلك
                    ob_high = df['ob_bearish_high'].iloc[i]
                    if pd.notna(ob_high) and close_i > ob_high:
                        bear_mitigated = True
                else:
                    ages_bearish.append(np.nan)
                mitigated_bearish.append(bear_mitigated)

            df['ob_bullish_age'] = ages_bullish
            df['ob_bearish_age'] = ages_bearish
            df['ob_bullish_mitigated'] = mitigated_bullish
            df['ob_bearish_mitigated'] = mitigated_bearish

            # ─────────────────────────────────────────────────────
            # Alpha Factors الكمية (للسكالبينج 15m/1h)
            # تُضاف هنا ولا توقف البوت إذا فشلت
            # ─────────────────────────────────────────────────────
            try:
                # Alpha 1: Volume-Price Divergence (VPD)
                # الهدف: اكتشاف ضعف الحركة السعرية (ارتفاع السعر مع هبوط الحجم = إشارة انعكاس)
                # القيمة السالبة = ضعف صعودي (خطر انعكاس هبوطي)
                # القيمة الموجبة = ضعف هبوطي (فرصة انعكاس صعودي)
                delta_close  = df['close'].diff(1)
                delta_volume = df['volume'].diff(1)
                df['alpha_vpd'] = np.sign(delta_volume) * (-1 * delta_close)

                # Alpha 2: Geometric Mean Deviation (GMD)
                # الهدف: كشف السيولة المخفية — يقيس انحراف السعر عن متوسطه الهندسي
                # إذا السعر بعيد جداً تحت المتوسط الهندسي → منطقة شراء محتملة
                # ماخوذة من Alpha#41 في ورقة Kakushadze
                # تتطلب: high, low, close
                geo_mean = np.sqrt(df['high'] * df['low'])
                close_ma = df['close'].rolling(20).mean()
                df['alpha_gmd'] = (geo_mean - close_ma) / close_ma.replace(0, np.nan)

                # Alpha 3: Volatility-Adjusted Momentum (VAM)
                # الهدف: زخم مُعدَّل بالتذبذب — يكشف قيعان السكالبينج
                # يجمع بين سرعة الحركة (momentum) وحجمها النسبي (ATR)
                # قيمة سالبة كبيرة = ضغط بيع مبالغ → فرصة ارتداد
                momentum_5 = df['close'].diff(5)
                atr_series = df['atr'] if 'atr' in df.columns else (df['high'] - df['low'])
                df['alpha_vam'] = momentum_5 / atr_series.replace(0, np.nan)

                logger.debug("[TAEngine] ✅ Alpha Factors حُسبت: VPD, GMD, VAM")
            except Exception as alpha_err:
                logger.debug(f"[TAEngine] Alpha Factors فشلت (غير حرج): {alpha_err}")
                df['alpha_vpd'] = 0.0
                df['alpha_gmd'] = 0.0
                df['alpha_vam'] = 0.0

            logger.debug("تم حساب SMC بشكل متقدم (OB Zones, Displacement, FVG, BOS, Age, Mitigation).")
            return df
            
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات الفنية: {e}")
            return df

    def extract_market_levels(self, df: pd.DataFrame, max_age: int = 100) -> dict:
        """Extract recent, still-actionable levels from closed candles only.

        Forward-filled columns are useful for the signal engine but unsafe as a
        target map because they hide when a zone was created and whether price
        has already traded through it.  This method reconstructs multiple OB,
        FVG and confirmed swing levels with age/mitigation metadata.
        """
        empty = {
            "bullish_obs": [], "bearish_obs": [],
            "bullish_fvgs": [], "bearish_fvgs": [],
            "swing_highs": [], "swing_lows": [],
        }
        if df is None or len(df) < 8:
            return empty

        closed = df.iloc[:-1].copy()
        if closed.empty:
            return empty
        max_age = max(10, int(max_age or 100))
        start = max(0, len(closed) - max_age)

        def _number(value):
            try:
                value = float(value)
                return value if np.isfinite(value) else 0.0
            except (TypeError, ValueError):
                return 0.0

        def _created_at(row, index):
            value = row.get("timestamp", row.get("time", index))
            return str(value)

        levels = {key: [] for key in empty}
        for position in range(start, len(closed)):
            row = closed.iloc[position]
            later = closed.iloc[position + 1:]
            age = len(closed) - 1 - position

            if bool(row.get("bullish_ob_created", False)):
                low = _number(row.get("ob_bullish_low"))
                high = _number(row.get("ob_bullish_high"))
                mitigated = bool(low > 0 and not later.empty and (later["close"] < low).any())
                if 0 < low <= high and not mitigated:
                    levels["bullish_obs"].append({
                        "low": low, "high": high, "age": age,
                        "created_at": _created_at(row, position), "type": "bullish_ob",
                    })

            if bool(row.get("bearish_ob_created", False)):
                low = _number(row.get("ob_bearish_low"))
                high = _number(row.get("ob_bearish_high"))
                mitigated = bool(high > 0 and not later.empty and (later["close"] > high).any())
                if 0 < low <= high and not mitigated:
                    levels["bearish_obs"].append({
                        "low": low, "high": high, "age": age,
                        "created_at": _created_at(row, position), "type": "bearish_ob",
                    })

            if bool(row.get("fvg_bullish_created", False)):
                low = _number(row.get("fvg_bullish_low"))
                high = _number(row.get("fvg_bullish_high"))
                # First later touch consumes the near edge for target purposes.
                touched = bool(high > 0 and not later.empty and (later["low"] <= high).any())
                if 0 < low < high and not touched:
                    levels["bullish_fvgs"].append({
                        "low": low, "high": high, "age": age,
                        "created_at": _created_at(row, position), "type": "bullish_fvg",
                    })

            if bool(row.get("fvg_bearish_created", False)):
                low = _number(row.get("fvg_bearish_low"))
                high = _number(row.get("fvg_bearish_high"))
                touched = bool(low > 0 and not later.empty and (later["high"] >= low).any())
                if 0 < low < high and not touched:
                    levels["bearish_fvgs"].append({
                        "low": low, "high": high, "age": age,
                        "created_at": _created_at(row, position), "type": "bearish_fvg",
                    })

        # A pivot is actionable only after two candles on its right have closed.
        # That makes these liquidity levels look-ahead-free at decision time.
        for position in range(max(2, start), len(closed) - 2):
            window = closed.iloc[position - 2:position + 3]
            row = closed.iloc[position]
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            age = len(closed) - 1 - position
            later_after_confirmation = closed.iloc[position + 3:]
            high_consumed = bool(
                not later_after_confirmation.empty
                and (later_after_confirmation["high"] >= high).any()
            )
            low_consumed = bool(
                not later_after_confirmation.empty
                and (later_after_confirmation["low"] <= low).any()
            )
            if high > 0 and high == _number(window["high"].max()) and not high_consumed:
                levels["swing_highs"].append({
                    "price": high, "age": age,
                    "created_at": _created_at(row, position), "type": "swing_high",
                })
            if low > 0 and low == _number(window["low"].min()) and not low_consumed:
                levels["swing_lows"].append({
                    "price": low, "age": age,
                    "created_at": _created_at(row, position), "type": "swing_low",
                })

        # Most recent first; cap each family to keep audit payloads compact.
        for key in levels:
            levels[key] = sorted(levels[key], key=lambda item: item.get("age", 999))[:12]
        return levels
            
    def get_htf_trend(self, symbol: str, client, ema_mode: str = "dual") -> str:
        """
        ✅ جديد: فلتر الترند العلوي (Higher TimeFrame Confirmation)
        يجلب بيانات 1H ويقيّم الاتجاه بناءً على EMA50 و EMA200 (أو EMA50, EMA100, EMA200).
        يُستخدم كفيتو: لا ندخل LONG على 15m إذا كان 1H هابطاً والعكس.
        النتيجة مخزّنة في cache لمدة 15 دقيقة لتجنب استدعاءات API متكررة.
        """
        import time
        now = time.time()
        cached = _htf_cache.get(symbol)
        if cached and (now - cached["ts"]) < HTF_CACHE_TTL_SECONDS:
            return cached["trend"]
        try:
            bars_1h = client.fetch_ohlcv(symbol, timeframe='1h', limit=500)
            if bars_1h is None or len(bars_1h) < 210:
                logger.warning(f"[HTF] بيانات 1H غير كافية لـ {symbol}; الحالة UNKNOWN وليست neutral.")
                return "unknown"
            df_htf = bars_1h.copy()
            df_htf['ema_50']  = ta.trend.EMAIndicator(close=df_htf['close'], window=50).ema_indicator()
            if ema_mode == "triple":
                df_htf['ema_100'] = ta.trend.EMAIndicator(close=df_htf['close'], window=100).ema_indicator()
            df_htf['ema_200'] = ta.trend.EMAIndicator(close=df_htf['close'], window=200).ema_indicator()
            last_htf = df_htf.iloc[-2]
            ema50  = last_htf.get('ema_50',  0)
            ema200 = last_htf.get('ema_200', 0)
            close  = last_htf.get('close',   0)
            if pd.isna(ema50) or pd.isna(ema200) or ema50 <= 0 or ema200 <= 0 or close <= 0:
                logger.warning(f"[HTF] EMA 1H غير صالحة لـ {symbol}; الحالة UNKNOWN.")
                return "unknown"
            
            if ema_mode == "triple":
                ema100 = last_htf.get('ema_100', 0)
                if ema50 > ema100 > ema200 and close > ema50:
                    trend = "bullish"
                elif ema50 < ema100 < ema200 and close < ema50:
                    trend = "bearish"
                else:
                    trend = "neutral"
                logger.debug(f"[HTF 1H] {symbol} (Triple): EMA50={ema50:.4f} | EMA100={ema100:.4f} | EMA200={ema200:.4f} --> Trend={trend.upper()}")
            else:
                if ema50 > ema200 and close > ema50:
                    trend = "bullish"
                elif ema50 < ema200 and close < ema50:
                    trend = "bearish"
                else:
                    trend = "neutral"
                logger.debug(f"[HTF 1H] {symbol} (Dual): EMA50={ema50:.4f} | EMA200={ema200:.4f} --> Trend={trend.upper()}")
                
            _htf_cache[symbol] = {"trend": trend, "ts": now}
            _htf_cache[symbol]["setup_context_1h"] = analyze_setup_context_1h(df_htf)
            return trend
        except Exception as e:
            logger.warning(f"[HTF] فشل جلب بيانات 1H لـ {symbol}: {e}")
            return "unknown"

    def get_setup_context_1h(self, symbol: str, client, side: str, ema_mode: str = "dual") -> dict:
        cached = _htf_cache.get(symbol)
        if not cached or "setup_context_1h" not in cached:
            self.get_htf_trend(symbol, client, ema_mode=ema_mode)
            cached = _htf_cache.get(symbol)
        context = (cached or {}).get("setup_context_1h")
        if not context:
            context = analyze_setup_context_1h(None)
        return select_setup_side(context, side)

    def get_market_context_4h(self, symbol: str, client) -> dict:
        """Fetch and cache a read-only 4H context for candidate explanation.

        This context is deliberately not an execution veto until prospective
        shadow/replay evidence demonstrates that it improves outcomes.
        """
        cached = _context_4h_cache.get(symbol)
        if cached:
            return dict(cached)
        try:
            bars = client.fetch_ohlcv(symbol, timeframe="4h", limit=260)
            context = analyze_market_context_4h(bars)
        except Exception as exc:
            logger.warning(f"[Context 4H] فشل جلب/تحليل {symbol}: {exc}")
            context = analyze_market_context_4h(None)
            context["reason"] = f"FOUR_HOUR_CONTEXT_ERROR:{type(exc).__name__}"
        _context_4h_cache[symbol] = dict(context)
        return context

    def get_multi_timeframe_indicators(self, symbol: str, client) -> dict:
        """
        يجلب مؤشرات فنية لـ 4 فريمات (15m, 1h, 4h, 1d) ويعيدها كـ dict.
        يُمرَّر الناتج مباشرةً لـ ai_engine.generate_interactive_analysis_v2()
        """
        result = {}
        timeframes = {
            "15m": {"tf": "15m", "limit": 250},
            "1h":  {"tf": "1h",  "limit": 500},
            "4h":  {"tf": "4h",  "limit": 300},
            "1d":  {"tf": "1d",  "limit": 200},
        }

        for key, cfg in timeframes.items():
            try:
                bars = client.fetch_ohlcv(symbol, timeframe=cfg["tf"], limit=cfg["limit"])
                if bars is None or len(bars) < 50:
                    logger.warning(f"[MTF] بيانات {key} غير كافية لـ {symbol}")
                    result[key] = {}
                    continue

                df = self.add_indicators(bars)
                if df is None or df.empty:
                    result[key] = {}
                    continue

                row = df.iloc[-2]  # آخر شمعة مغلقة (منهجية Lookahead-free)

                # ── حساب MACD Histogram ──
                macd_val  = float(row.get("macd",        0) or 0)
                macd_sig  = float(row.get("macd_signal", 0) or 0)
                macd_hist = round(macd_val - macd_sig, 8)

                # ── Supertrend بسيط من EMA ──
                ema50  = float(row.get("ema_50",  0) or 0)
                ema200 = float(row.get("ema_200", 0) or 0)
                close  = float(row.get("close",   0) or 0)
                supertrend_dir = "صاعد 📈" if (ema50 > ema200 and close > ema50) else (
                                 "هابط 📉" if (ema50 < ema200 and close < ema50) else "جانبي ↔️")

                # ── مناطق الدعم والمقاومة من Order Blocks (موجودة في add_indicators) ──
                support1    = float(row.get("ob_bullish_high", 0) or 0)
                support2    = float(row.get("ob_bullish_low",  0) or 0)
                resistance1 = float(row.get("ob_bearish_low",  0) or 0)
                resistance2 = float(row.get("ob_bearish_high", 0) or 0)

                # تنظيف: أخرج صفر إذا لا توجد OB لهذا الفريم
                def _clean(v): return round(v, 6) if v > 0 else "N/A"

                result[key] = {
                    "rsi":            round(float(row.get("rsi", 0) or 0), 2),
                    "ema_50":         round(ema50,  6),
                    "ema_200":        round(ema200, 6),
                    "atr":            round(float(row.get("atr", 0) or 0), 6),
                    "adx":            round(float(row.get("adx", 0) or 0), 2),
                    "macd_hist":      macd_hist,
                    "volume":         round(float(row.get("volume", 0) or 0), 2),
                    "supertrend_dir": supertrend_dir,
                    "support1":       _clean(support1),
                    "support2":       _clean(support2),
                    "resistance1":    _clean(resistance1),
                    "resistance2":    _clean(resistance2),
                    "alpha_vpd":      round(float(row.get("alpha_vpd", 0) or 0), 6),
                    "alpha_gmd":      round(float(row.get("alpha_gmd", 0) or 0), 6),
                    "alpha_vam":      round(float(row.get("alpha_vam", 0) or 0), 4),
                }
                logger.debug(
                    f"[MTF] {symbol}/{key}: RSI={result[key]['rsi']} | "
                    f"ADX={result[key]['adx']} | Dir={supertrend_dir}"
                )

            except Exception as e:
                logger.error(f"[MTF] فشل جلب {key} لـ {symbol}: {e}")
                result[key] = {}

        return result

    def validate_entry_quality(
        self,
        bars_df: pd.DataFrame,
        side: str,
        current_price=None,
        filter_profile: dict = None,
        market_regime: dict = None,
        audit_details: dict = None,
    ):
        """
        فلتر جودة الدخول:
        - يمنع LONG في أعلى 30% من الشمعة الحالية.
        - يمنع SHORT في أدنى 30% من الشمعة الحالية.
        - يمنع السوق الجانبي إذا ATR أقل من min_atr_pct الخاصة بالبروفايل الحالي.
        - يمنع الدخول بعد شمعة سابقة ذات حركة كبيرة حتى يحدث تصحيح.
        """
        audit = audit_details if isinstance(audit_details, dict) else {}
        audit.clear()
        audit.update({"side": str(side or "").lower(), "gates": [], "values": {}, "thresholds": {}})

        def _result(passed, reason, gate):
            audit["passed"] = bool(passed)
            audit["reason"] = str(reason)
            audit["terminal_gate"] = str(gate)
            audit["gates"].append({"gate": str(gate), "passed": bool(passed)})
            return passed, reason

        try:
            side = str(side or "").lower()
            if side not in ("buy", "sell"):
                return _result(True, "Not an entry side", "ENTRY_SIDE")

            if bars_df is None or len(bars_df) < 4:
                audit["values"]["candle_count"] = 0 if bars_df is None else len(bars_df)
                return _result(False, "EntryQuality: بيانات الشموع غير كافية", "DATA_SUFFICIENCY")

            def _f(value, default=0.0):
                try:
                    value = float(value)
                    return value if np.isfinite(value) else default
                except Exception:
                    return default

            # مرجع الجودة هو آخر شمعة مغلقة؛ الشمعة الحية تعطي غالباً 0%/100% زائفاً.
            last_closed = bars_df.iloc[-2]
            prev_closed = bars_df.iloc[-3]
            reference_candle = last_closed

            price = _f(current_price, _f(last_closed.get("close", 0)))
            audit["values"]["current_price"] = price
            if price <= 0:
                return _result(False, "EntryQuality: السعر الحالي غير صالح", "VALID_PRICE")

            # 1) Candle Body / Extreme Zone Rule
            raw_high = reference_candle.get("high", None)
            raw_low = reference_candle.get("low", None)
            try:
                high = float(raw_high)
                low = float(raw_low)
            except (TypeError, ValueError):
                high = low = float("nan")
            audit["values"].update({
                "reference_candle_high": high if np.isfinite(high) else None,
                "reference_candle_low": low if np.isfinite(low) else None,
            })
            if (
                not np.isfinite(high)
                or not np.isfinite(low)
                or high <= 0
                or low <= 0
                or high < low
            ):
                return _result(
                    False,
                    "EntryQuality: بيانات High/Low لشمعة المرجع غير صالحة",
                    "REFERENCE_CANDLE_DATA",
                )

            atr = _f(last_closed.get("atr", 0))
            candle_range = high - low
            audit["values"]["reference_candle_range"] = candle_range
            # الحد الأدنى للنطاق: 0.05% من السعر — يتجنب الرفض الزائف في Testnet
            # حيث تكون الشموع الحية flat (High=Low=Price) بسبب بيانات غير كاملة
            min_candle_range = price * 0.0005

            # A position is defined only inside the closed candle. Extending the
            # range with the live price pinned every breakout to 100% (or 0%).
            # An outside price is deliberately deferred to the entry-location
            # and executable-price guards; it is not permission to chase it.
            if candle_range > min_candle_range and low <= price <= high:
                pos = (price - low) / candle_range
                pos = max(0.0, min(1.0, pos))
                extreme_zone = float(getattr(Config, "ENTRY_EXTREME_ZONE_PCT", 0.30))
                audit["values"]["closed_candle_position"] = pos
                audit["thresholds"]["extreme_zone_pct"] = extreme_zone

                if side == "buy" and pos > (1.0 - extreme_zone):
                    return _result(False, f"EntryQuality: رفض LONG لأن السعر الحالي ({price:.4f}) في أعلى {extreme_zone:.0%} من الشمعة الحالية (النسبة: {pos:.1%})", "EXTREME_ZONE")

                if side == "sell" and pos < extreme_zone:
                    return _result(False, f"EntryQuality: رفض SHORT لأن السعر الحالي ({price:.4f}) في أدنى {extreme_zone:.0%} من الشمعة الحالية (النسبة: {pos:.1%})", "EXTREME_ZONE")
                audit["gates"].append({
                    "gate": "EXTREME_ZONE",
                    "passed": True,
                    "evaluated": True,
                    "status": "CHECKED_INSIDE_REFERENCE_CANDLE",
                })
            elif candle_range <= min_candle_range:
                audit["gates"].append({
                    "gate": "EXTREME_ZONE",
                    "passed": True,
                    "evaluated": False,
                    "status": "SKIPPED_NARROW_REFERENCE_RANGE",
                })
            else:
                relation = "ABOVE_HIGH" if price > high else "BELOW_LOW"
                outside_distance = price - high if price > high else low - price
                audit["values"].update({
                    "reference_candle_relation": relation,
                    "outside_reference_distance": outside_distance,
                    "outside_reference_distance_pct": outside_distance / (
                        high if price > high else low
                    ),
                    "outside_reference_distance_atr": (
                        outside_distance / atr if atr > 0 else None
                    ),
                })
                audit["gates"].append({
                    "gate": "EXTREME_ZONE",
                    "passed": True,
                    "evaluated": False,
                    "status": "DEFERRED_TO_ENTRY_LOCATION_GUARDS",
                })

            # 2) Sideways / Low ATR Filter — يقرأ الحد من البروفايل إن وجد، وإلا من Config
            _fp = filter_profile or {}
            min_atr_pct = float(_fp.get("min_atr_pct") or getattr(Config, "MIN_ATR_PCT_OF_PRICE", 0.008))
            atr_pct = atr / price if price > 0 else 0.0
            audit["values"].update({"atr": atr, "atr_pct": atr_pct})
            audit["thresholds"]["min_atr_pct"] = min_atr_pct

            if atr_pct < min_atr_pct:
                return _result(False, f"EntryQuality: سوق جانبي ATR={atr_pct:.3%} أقل من الحد [{_fp.get('label','Config')}={min_atr_pct:.3%}]", "MIN_ATR")
            audit["gates"].append({"gate": "MIN_ATR", "passed": True})

            # 3) Big Previous Candle Filter (Require Retracement)
            # لا نحظر displacement متوافقاً مع benchmark regime قوي. سجل 48h
            # أظهر أن الحظر المطلق كان يزيل momentum continuation، بينما بوابة
            # 5m الموحدة تتولى منع المطاردة الفعلية عند مكان الدخول.
            if bool(getattr(Config, "REQUIRE_RETRACEMENT_AFTER_BIG_MOVE", True)):
                prev_open  = _f(prev_closed.get("open",  0))
                prev_high  = _f(prev_closed.get("high",  0))
                prev_low   = _f(prev_closed.get("low",   0))
                prev_close = _f(prev_closed.get("close", 0))

                if prev_open > 0:
                    prev_move_pct = abs(prev_high - prev_low) / prev_open
                    threshold = float(getattr(Config, "BIG_MOVE_PCT_THRESHOLD", 0.015))
                    audit["values"]["previous_candle_move_pct"] = prev_move_pct
                    audit["thresholds"]["big_move_pct"] = threshold

                    if prev_move_pct > threshold:
                        last_open  = _f(last_closed.get("open",  0))
                        last_close = _f(last_closed.get("close", 0))
                        prev_direction = 1 if prev_close > prev_open else -1
                        last_direction = 1 if last_close > last_open else -1

                        regime_label = str(
                            (market_regime or {}).get("label") or "UNKNOWN"
                        ).upper()
                        regime_aligned = (
                            side == "buy" and regime_label == "STRONG_RISK_ON"
                        ) or (
                            side == "sell" and regime_label == "STRONG_RISK_OFF"
                        )

                        if last_direction == prev_direction and not regime_aligned:
                            return _result(False, f"EntryQuality: الشمعة السابقة حركتها {prev_move_pct:.2%} > {threshold:.2%} ولم يحدث تصحيح بعد", "BIG_MOVE_RETRACEMENT")
            audit["gates"].append({"gate": "BIG_MOVE_RETRACEMENT", "passed": True})

            # 4) منع مطاردة حركة يومية ممتدة. الشرط لا يعتمد على تصنيف العملة
            # (meme/mainstream)، ويحتاج اجتماع: تغير كبير + عدة ATR + وجود السعر
            # في طرف نطاق الفترة. بهذا لا نرفض ترنداً طبيعياً لمجرد نسبة واحدة.
            lookback = max(
                48, int(getattr(Config, "ENTRY_EXTENSION_LOOKBACK_BARS", 96))
            )
            closed_window = bars_df.iloc[:-1].tail(lookback)
            if len(closed_window) >= 48 and atr > 0:
                window_open = _f(closed_window.iloc[0].get("open", 0))
                window_high = max(_f(closed_window["high"].max()), price)
                window_low = min(_f(closed_window["low"].min()), price)
                if window_open > 0 and window_high > window_low:
                    directional_move = (price - window_open) / window_open
                    move_atr = abs(price - window_open) / atr
                    range_position = (price - window_low) / (window_high - window_low)
                    max_move = float(
                        getattr(Config, "ENTRY_MAX_DIRECTIONAL_MOVE_PCT", 0.12)
                    )
                    max_atr = float(
                        getattr(Config, "ENTRY_MAX_DIRECTIONAL_ATR", 8.0)
                    )
                    range_zone = float(
                        getattr(Config, "ENTRY_EXTENSION_RANGE_ZONE", 0.75)
                    )
                    audit["values"].update({
                        "directional_move": directional_move,
                        "move_atr": move_atr,
                        "range_position": range_position,
                    })
                    audit["thresholds"].update({
                        "max_directional_move_pct": max_move,
                        "max_directional_atr": max_atr,
                        "extension_range_zone": range_zone,
                    })

                    if (
                        side == "buy"
                        and directional_move >= max_move
                        and move_atr >= max_atr
                        and range_position >= range_zone
                    ):
                        return _result(False, (
                            "EntryQuality: رفض LONG بعد امتداد "
                            f"{directional_move:.1%} ({move_atr:.1f} ATR) "
                            f"والسعر في أعلى النطاق ({range_position:.0%})"
                        ), "DIRECTIONAL_EXTENSION")
                    if (
                        side == "sell"
                        and directional_move <= -max_move
                        and move_atr >= max_atr
                        and range_position <= (1.0 - range_zone)
                    ):
                        return _result(False, (
                            "EntryQuality: رفض SHORT بعد امتداد "
                            f"{directional_move:.1%} ({move_atr:.1f} ATR) "
                            f"والسعر في أدنى النطاق ({range_position:.0%})"
                        ), "DIRECTIONAL_EXTENSION")

            return _result(True, "EntryQuality: OK", "ALL_GATES")

        except Exception as e:
            logger.warning(f"[EntryQuality] فشل فلتر جودة الدخول: {e}")
            audit["error_type"] = type(e).__name__
            return _result(False, f"EntryQuality: خطأ أثناء الفحص {e}", "FILTER_EXCEPTION")

    def evaluate_trend(self, df: pd.DataFrame, symbol="Unknown", details: dict = None, htf_trend: str = "neutral", ema_mode: str = "dual", filter_profile: dict = None):

        """
        تقييم مناطق الدخول الاستراتيجية بناءً على القواعد الاحترافية.
        htf_trend: الترند من الإطار الزمني الأعلى (1H) — يُستخدم كفيتو.
        """
        if df is None or len(df) < 50:
            if details is not None: details.update({'score': 0, 'reason': 'بيانات غير كافية (أقل من 50 شمعة)'})
            return "neutral"

        # ─── حارس: إذا كانت EMA200 = 0 فالبيانات غير كافية للقرار (Testnet أو شموع ناقصة) ───
        _check_latest = df.iloc[-2]
        _ema200_check = float(_check_latest.get('ema_200', 0) or 0)
        _adx_check    = float(_check_latest.get('adx', 0) or 0)
        if pd.isna(_ema200_check) or _ema200_check == 0.0:
            logger.debug(f"[evaluate_trend] EMA200=0 — بيانات غير كافية (أقل من 200 شمعة أو Testnet). قرار: neutral.")
            if details is not None:
                details.update({'score': 0, 'reason': 'EMA200=0: بيانات غير كافية لتحديد الاتجاه'})
            return "neutral"
            
        # نستخدم الشمعة المغلقة فقط!
        latest = df.iloc[-2]

        # ── التحليل الزمني للمؤشرات (آخر 5 شموع) ─────────────────
        rsi_slice  = df['rsi'].dropna().iloc[-6:-1].tolist()   # آخر 5 شموع مغلقة
        macd_slice = df['macd'].dropna().iloc[-6:-1].tolist()

        rsi_trend    = get_trending_direction(rsi_slice)      # اتجاه RSI العام
        macd_cross   = get_crossed_since(macd_slice)          # منذ كم شمعة تجاوز MACD الصفر
        rsi_cross_50 = get_crossed_since([v - 50 for v in rsi_slice])  # منذ كم شمعة RSI تجاوز 50

        
        close = latest.get('close', 0)
        atr = latest.get('atr', 0)
        ema_200 = latest.get('ema_200', 0)
        ema_100 = latest.get('ema_100', 0)
        ema_50 = latest.get('ema_50', 0)
        
        # OB Zones
        ob_bullish_low = latest.get('ob_bullish_low', 0)
        ob_bullish_high = latest.get('ob_bullish_high', 0)
        ob_bullish_mid = latest.get('ob_bullish_mid', 0)
        
        ob_bearish_low = latest.get('ob_bearish_low', 0)
        ob_bearish_high = latest.get('ob_bearish_high', 0)
        ob_bearish_mid = latest.get('ob_bearish_mid', 0)
        
        ob_bullish_age = latest.get('ob_bullish_age', 999)
        ob_bearish_age = latest.get('ob_bearish_age', 999)

        # ── max_ob_age ديناميكي من البروفايل (بدلاً من القيمة الثابتة 80) ──
        _fp = filter_profile or {}
        max_ob_age = int(_fp.get("max_ob_age", 80))
        # فلاغ: هل OB شرط أصلاً؟ (في بعض البروفايلات لا)
        require_ob = bool(_fp.get("require_ob", True))
        # فلاغ: هل Fallback RSI/MACD مفعّل؟
        enable_fallback = bool(_fp.get("enable_fallback", getattr(Config, "ENABLE_FALLBACK_STRATEGY", False)))
        allow_countertrend_reversal = bool(_fp.get("allow_countertrend_reversal", False))

        # Invalidation Check (لمنع الدخول في مناطق مكسورة أو مستهلكة بشدة أو قديمة جداً)
        # إذا السعر الحالي أغلق تحت المنطقة، نعتبرها مكسورة ولا نتداول عليها
        # --- OB Mitigation Check ---
        ob_bullish_mitigated = bool(latest.get('ob_bullish_mitigated', False))
        ob_bearish_mitigated = bool(latest.get('ob_bearish_mitigated', False))

        # ✅ صحة OB: موجودة + في نطاق العمر + غير مستهلكة
        valid_bullish_ob = (
            (ob_bullish_low > 0)
            and (close >= ob_bullish_low * 0.995)
            and (ob_bullish_age <= max_ob_age)
            and not ob_bullish_mitigated
        )
        valid_bearish_ob = (
            (ob_bearish_high > 0)
            and (close <= ob_bearish_high * 1.005)
            and (ob_bearish_age <= max_ob_age)
            and not ob_bearish_mitigated
        )

        score = 0
        reason = "انتظار وصول السعر لمنطقة OB نشطة وحديثة وغير مستهلكة"
        trend_result = "neutral"
        # إصلاح: تعريف افتراضي لمنع UnboundLocalError وSL=سعر الدخول
        optimal_entry = close
        invalidation_level = close

        # 1. تقييم الاتجاه العام (15m)
        if ema_mode == "triple":
            is_uptrend_15m = (ema_50 > ema_100 > ema_200) if (ema_50 > 0 and ema_100 > 0 and ema_200 > 0) else True
            is_downtrend_15m = (ema_50 < ema_100 < ema_200) if (ema_50 > 0 and ema_100 > 0 and ema_200 > 0) else True
        else:
            is_uptrend_15m = (ema_50 > ema_200) if (ema_50 > 0 and ema_200 > 0) else True
            is_downtrend_15m = (ema_50 < ema_200) if (ema_50 > 0 and ema_200 > 0) else True

        # فشل بيانات HTF لا يتحول إلى neutral مسموح. أما التعارض الحقيقي فيمكن
        # أن يولد *مرشح انعكاس* فقط للبروفايلات التي تسمح به؛ بوابة 5m الموحدة
        # ستطلب عندها sweep + volume + rejection معاً قبل أي تنفيذ.
        htf_known = htf_trend in ("bullish", "bearish", "neutral")
        htf_allows_buy = htf_known and (
            htf_trend != "bearish" or allow_countertrend_reversal
        )
        htf_allows_sell = htf_known and (
            htf_trend != "bullish" or allow_countertrend_reversal
        )

        # 2. الشراء (LONG)
        if is_uptrend_15m and valid_bullish_ob and htf_allows_buy:
            distance_to_ob = abs(close - ob_bullish_high)
            # توسيع نافذة الدخول: 3.5% بدل 2% لمتابعة OB القريب
            if distance_to_ob <= min(close * 0.035, atr * 1.5) or (close <= ob_bullish_high and close >= ob_bullish_low):
                score = 3
                trend_result = "buy"
                htf_note = f" | HTF={htf_trend.upper()}" if htf_trend != "neutral" else ""
                cross_info = f" | MACD عبر الصفر منذ {macd_cross} شموع" if macd_cross else ""
                rsi_info   = f" | RSI فوق 50 منذ {rsi_cross_50} شموع" if rsi_cross_50 else ""
                reason = f"إشارة قنص (Smart Money LONG) - السعر عند OB صاعد{htf_note}{cross_info}{rsi_info}"
                optimal_entry = ob_bullish_mid
                invalidation_level = ob_bullish_low

        # --- مسار ثانوي: RSI+MACD+BOS بدون OB محدد (LONG) ---
        # enable_fallback يأتي من البروفايل الحالي ديناميكياً
        if score == 0 and is_uptrend_15m and htf_allows_buy and enable_fallback:
            rsi_val  = latest.get('rsi', 50)
            macd_val = latest.get('macd', 0)
            macd_sig = latest.get('macd_signal', 0)
            adx_val  = latest.get('adx', 0)
            if (
                50 < rsi_val < 70             # RSI فوق 50 وليس في منطقة التشبع
                and macd_val > macd_sig       # MACD فوق خط الإشارة
                and adx_val >= 25             # ترند قوي بما يكفي
            ):
                score = 2
                trend_result = "buy"
                reason = f"[RSI+MACD Fallback] LONG مؤكد | ADX={adx_val:.0f} | RSI={rsi_val:.0f}"
                optimal_entry = close
                invalidation_level = close * 0.985  # 1.5% ستوب احتياطي

        # 3. البيع (SHORT) — إصلاح: if مستقل بدلاً من elif لضمان التقييم دائماً
        if score == 0 and is_downtrend_15m and valid_bearish_ob and htf_allows_sell:
            distance_to_ob = abs(close - ob_bearish_low)
            # توسيع نافذة الدخول: 3.5% بدل 2%
            if distance_to_ob <= min(close * 0.035, atr * 1.5) or (close >= ob_bearish_low and close <= ob_bearish_high):
                score = 3
                trend_result = "sell"
                htf_note = f" | HTF={htf_trend.upper()}" if htf_trend != "neutral" else ""
                cross_info = f" | MACD عبر الصفر منذ {macd_cross} شموع" if macd_cross else ""
                reason = f"إشارة قنص (Smart Money SHORT) - السعر عند OB هابط{htf_note}{cross_info}"
                optimal_entry = ob_bearish_mid
                invalidation_level = ob_bearish_high

        # --- مسار ثانوي: RSI+MACD Fallback للبيع ---
        if score == 0 and is_downtrend_15m and htf_allows_sell and enable_fallback:
            rsi_val  = latest.get('rsi', 50)
            macd_val = latest.get('macd', 0)
            macd_sig = latest.get('macd_signal', 0)
            adx_val  = latest.get('adx', 0)
            if (
                30 < rsi_val < 50             # RSI تحت 50 وليس في منطقة التشبع
                and macd_val < macd_sig       # MACD تحت خط الإشارة
                and adx_val >= 25
            ):
                score = 2
                trend_result = "sell"
                reason = f"[RSI+MACD Fallback] SHORT مؤكد | ADX={adx_val:.0f} | RSI={rsi_val:.0f}"
                optimal_entry = close
                invalidation_level = close * 1.015  # 1.5% ستوب احتياطي

        # سبب الرفض بسبب HTF (للتشخيص)
        if score == 0 and not htf_known:
            reason = f"رُفض القرار لأن بيانات HTF 1H غير معروفة ({htf_trend})"
        elif score == 0 and is_uptrend_15m and valid_bullish_ob and not htf_allows_buy:
            reason = f"رُفض LONG بسبب HTF هابط (1H={htf_trend.upper()}) رغم وجود OB صاعد على 15m"
        elif score == 0 and is_downtrend_15m and valid_bearish_ob and not htf_allows_sell:
            reason = f"رُفض SHORT بسبب HTF صاعد (1H={htf_trend.upper()}) رغم وجود OB هابط على 15m"
                    
        if details is not None:
            market_levels = self.extract_market_levels(df, max_age=max_ob_age)
            relevant_obs = (
                market_levels.get("bullish_obs", [])
                if trend_result == "buy"
                else market_levels.get("bearish_obs", [])
                if trend_result == "sell"
                else []
            )
            if trend_result == "buy":
                current_low = float(ob_bullish_low) if pd.notna(ob_bullish_low) else 0.0
                current_high = float(ob_bullish_high) if pd.notna(ob_bullish_high) else 0.0
            elif trend_result == "sell":
                current_low = float(ob_bearish_low) if pd.notna(ob_bearish_low) else 0.0
                current_high = float(ob_bearish_high) if pd.notna(ob_bearish_high) else 0.0
            else:
                current_low = current_high = 0.0
            setup_anchor = min(
                relevant_obs,
                key=lambda item: (
                    abs(float(item.get("low", 0)) - current_low)
                    + abs(float(item.get("high", 0)) - current_high)
                ),
                default=None,
            )
            countertrend = (
                (trend_result == "buy" and htf_trend == "bearish")
                or (trend_result == "sell" and htf_trend == "bullish")
            )
            details.update({
                'score': score,
                'close': close,
                'atr': atr,
                'reason': reason,
                'support': ob_bullish_high if pd.notna(ob_bullish_high) else 0,
                'resistance': ob_bearish_low if pd.notna(ob_bearish_low) else 0,
                'optimal_entry': locals().get('optimal_entry', close),
                'invalidation_level': locals().get('invalidation_level', close),
                'ob_mid': locals().get('optimal_entry', close),
                'market_levels': market_levels,
                'ema_200': ema_200,
                'ema_50': ema_50,
                'adx': latest.get('adx', 0),
                'rsi': latest.get('rsi', 0),
                'volume': latest.get('volume', 0),
                'volume_avg_20': latest.get('volume_avg_20', 0),
                'volume_median_20': latest.get('volume_median_20', 0),
                'rvol_20': latest.get('rvol_20', 0),
                'htf_trend': htf_trend,                                           # ✅ جديد
                'ob_bullish_mitigated': ob_bullish_mitigated,                     # ✅ جديد
                'ob_bearish_mitigated': ob_bearish_mitigated,                     # ✅ جديد
                "sell_side_sweep": bool(latest.get("sell_side_sweep", False)),
                "buy_side_sweep": bool(latest.get("buy_side_sweep", False)),
                "recent_sell_side_sweep": bool(latest.get("recent_sell_side_sweep", False)),
                "recent_buy_side_sweep": bool(latest.get("recent_buy_side_sweep", False)),
                "ob_low": current_low,
                "ob_high": current_high,
                "setup_anchor": setup_anchor,
                "countertrend": bool(countertrend),
                "htf_data_known": bool(htf_known),
                "local_trend_aligned": bool(
                    (trend_result == "buy" and is_uptrend_15m)
                    or (trend_result == "sell" and is_downtrend_15m)
                ),
            })
            
        return trend_result

    def analyze(self, symbol: str, client, ema_mode: str = "dual", filter_profile: dict = None) -> dict:
        try:
            bars = client.fetch_ohlcv(symbol, timeframe='15m', limit=300)  # زيادة من 150 لدقة أفضل لـ EMA200
            if bars is None or len(bars) < 50:
                return {}
            df = self.add_indicators(bars, filter_profile=filter_profile)
            details = {}
            # جلب الترند العلوي (1H) أولاً ثم تمريره لـ evaluate_trend
            htf_trend = self.get_htf_trend(symbol, client, ema_mode=ema_mode)
            trend = self.evaluate_trend(df, symbol=symbol, details=details, htf_trend=htf_trend, ema_mode=ema_mode, filter_profile=filter_profile)
            latest = df.iloc[-2]
            market_levels = details.get("market_levels") or self.extract_market_levels(
                df, max_age=int((filter_profile or {}).get("max_ob_age", 100))
            )
            
            return {
                "trend": trend,
                "score": details.get("score", 0),
                "reason": details.get("reason", ""),
                "price": latest.get("close", 0),
                "rsi": latest.get("rsi", 0),
                "macd": latest.get("macd", 0),
                "ema_200": latest.get("ema_200", 0),
                "adx": latest.get("adx", 0),
                "support": details.get("support", 0),
                "resistance": details.get("resistance", 0),
                "atr": latest.get("atr", 0),
                "market_levels": market_levels
            }
        except Exception as e:
            logger.error(f"خطأ في دالة analyze للزوج {symbol}: {e}")
            return {}
