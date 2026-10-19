import time
import pandas as pd
import numpy as np
from core.logger import logger

class TrailingStopManager:
    """
    نظام الستوب المتحرك المتقدم (SMC Hybrid Trailing Stop)
    يعتمد على: R-Multiple + ATR Buffer + Swing Structure
    """
    def __init__(self, state_manager=None):
        self._state_manager = state_manager
        
        # R-based activation
        self.break_even_at_r         = 0.8   # يبدأ الحماية مبكراً عند 0.8R
        self.structure_trailing_at_r = 1.5   # Structure trailing بعد 1.5R
        self.atr_tight_trailing_at_r = 2.5   # Tight trailing بعد 2.5R

        # ATR buffers
        self.structure_atr_buffer = 0.3
        self.atr_trail_multiplier_normal = 2.5
        self.atr_trail_multiplier_tight = 1.5

        # الحد الأدنى لتحديث الستوب لتقليل ضغط API (0.1%)
        self.min_sl_update_pct = 0.001  
        
        # ذاكرة الصفقات المفتوحة
        self._trades: dict = {}
        
        # استعادة الحالة
        if state_manager:
            saved = state_manager.get_trailing_trades()
            if saved:
                self._trades = saved
                logger.info(f"[Trailing] تم استعادة حالة {len(saved)} صفقة متتبعة.")

    def _audit(self, symbol, status, details=None, *, durable=False):
        try:
            from utils.decision_audit import audit_decision_event

            meta = self._state_manager.get_entry_metadata(symbol) if self._state_manager else {}
            audit_decision_event(
                scan_id=(meta or {}).get("scan_id"),
                decision_id=(meta or {}).get("decision_id"),
                setup_id=(meta or {}).get("setup_thesis_id"),
                thesis_id=(meta or {}).get("setup_thesis_id"),
                position_id=(meta or {}).get("trade_id"),
                trade_id=(meta or {}).get("trade_id"),
                symbol=symbol,
                stage="TRAILING_STOP",
                status=status,
                details=details or {},
                durable=durable,
            )
        except Exception as exc:
            logger.debug(f"[Trailing] تعذر تسجيل audit لـ {symbol}: {exc}")

    def register_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        initial_sl: float,
        amount: float = 0.0,
    ):
        """تسجيل صفقة جديدة في نظام التتبع"""
        self._trades[symbol] = {
            "side": side.lower(),
            "entry": float(entry_price),
            "best_price": float(entry_price),
            "current_sl": float(initial_sl),
            "initial_sl": float(initial_sl),
            "amount": float(amount or 0.0),
            "full_amount": float(amount or 0.0),
            "step_trail_active": False,
            "trailing_active": False,
        }

        if self._state_manager:
            self._state_manager.save_trailing_trades(self._trades)

        logger.info(
            f"[Trailing] ✅ تم تسجيل صفقة {symbol} ({side.upper()}) | "
            f"دخول: {entry_price} | SL أولي: {initial_sl} | amount={amount}"
        )

    def restore_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        initial_sl: float,
        current_sl: float,
        amount: float,
        current_price: float,
    ) -> bool:
        """يعيد بناء التتبع بعد restart دون تصفير SL الأصلي أو خفض الحماية."""
        if symbol in self._trades:
            return True
        side = str(side or "").lower()
        values = (entry_price, initial_sl, current_sl, amount, current_price)
        if side not in ("buy", "sell") or any(float(value or 0) <= 0 for value in values):
            return False
        entry_price = float(entry_price)
        current_price = float(current_price)
        best_price = (
            max(entry_price, current_price)
            if side == "buy"
            else min(entry_price, current_price)
        )
        self._trades[symbol] = {
            "side": side,
            "entry": entry_price,
            "best_price": best_price,
            "current_sl": float(current_sl),
            "initial_sl": float(initial_sl),
            "amount": float(amount),
            "full_amount": float(amount),
            "step_trail_active": bool(
                (side == "buy" and float(current_sl) >= entry_price)
                or (side == "sell" and float(current_sl) <= entry_price)
            ),
            "trailing_active": float(current_sl) != float(initial_sl),
            "recovered_after_restart": True,
        }
        if self._state_manager and not self._state_manager.save_trailing_trades(self._trades):
            self._trades.pop(symbol, None)
            return False
        logger.info(
            f"[Trailing] ✅ استُعيد تتبع {symbol} بعد restart | "
            f"current_sl={current_sl} amount={amount}"
        )
        return True

    def _persist_runtime_state(self, symbol: str) -> bool:
        if not self._state_manager:
            return True
        if not self._state_manager.save_trailing_trades(self._trades):
            return False
        trade = self._trades.get(symbol, {})
        meta = self._state_manager.get_entry_metadata(symbol)
        if meta:
            meta["current_sl"] = float(trade.get("current_sl") or 0.0)
            meta["trailing_best_price"] = float(trade.get("best_price") or 0.0)
            meta["trailing_active"] = bool(trade.get("trailing_active", False))
            return bool(self._state_manager.save_entry_metadata(symbol, meta))
        return True

    def mark_step_trail_active(self, symbol: str, new_sl: float, full_amount: float):
        """تحديث Step Trail مع الاحتفاظ بكامل كمية المركز دون إغلاق جزئي."""
        if symbol not in self._trades:
            return

        trade = self._trades[symbol]
        trade["step_trail_active"] = True
        trade["amount"] = float(full_amount or trade.get("amount", 0.0))
        trade["full_amount"] = trade["amount"]
        trade["current_sl"] = float(new_sl)
        trade["trailing_active"] = True

        if self._state_manager:
            self._state_manager.save_trailing_trades(self._trades)

        logger.info(
            f"[Trailing] ✅ تم تفعيل Step Trail لـ {symbol}. "
            f"SL={new_sl}, full_amount={trade['amount']}"
        )

    def unregister_trade(self, symbol: str):
        """إلغاء تتبع الصفقة بعد إغلاقها"""
        if symbol in self._trades:
            del self._trades[symbol]
            if self._state_manager:
                self._state_manager.save_trailing_trades(self._trades)

    def _persist_close_intent(self, symbol: str, reason: str):
        if not self._state_manager:
            return False
        meta = self._state_manager.get_entry_metadata(symbol)
        if not meta:
            return False
        meta["pending_close_reason"] = reason
        meta["pending_close_requested_at"] = time.time()
        return bool(self._state_manager.save_entry_metadata(symbol, meta))

    def calculate_atr(self, df: pd.DataFrame, period=14):
        high = df['high']
        low = df['low']
        close = df['close']
        prev_close = close.shift(1)
        
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-2]  # نأخذ الشمعة المغلقة
        if pd.isna(atr) or atr <= 0:
            logger.warning(f"[Trailing] ATR غير صالح ({atr}), استخدام ATR افتراضي")
            atr = float(tr.dropna().mean()) if not tr.dropna().empty else 0.001
        return atr

    def get_confirmed_swing(self, df: pd.DataFrame, side: str, left=3, right=3):
        """
        يرجع آخر swing low مؤكد للشراء أو swing high مؤكد للبيع.
        """
        if len(df) < left + right + 5:
            return None

        # الشموع المغلقة فقط
        max_i = len(df) - right - 1 
        swings = []

        if side == 'buy':
            lows = df['low'].values
            for i in range(left, max_i):
                center = lows[i]
                if center < np.min(lows[i-left:i]) and center < np.min(lows[i+1:i+1+right]):
                    swings.append(center)
        else:
            highs = df['high'].values
            for i in range(left, max_i):
                center = highs[i]
                if center > np.max(highs[i-left:i]) and center > np.max(highs[i+1:i+1+right]):
                    swings.append(center)

        return swings[-1] if swings else None

    def update(self, symbol: str, current_price: float, client) -> float | None:
        """يُستدعى لفحص وتحديث الستوب المتحرك إذا لزم الأمر."""
        if symbol not in self._trades:
            return None

        # ── تحقق من تجميد المحاولات بعد الفشل المتكرر ───────────────────
        trade_check = self._trades[symbol]
        retry_after = trade_check.get('_sl_retry_after', 0)
        if retry_after and time.time() < retry_after:
            return None

        # تجاهل new_sl الفاشل سابقاً لتجنب الحلقة اللانهائية
        failed_sl = trade_check.get('_failed_sl')

        trade = self._trades[symbol]
        side = trade['side']
        entry = trade['entry']
        current_sl = trade.get('current_sl', trade.get('initial_sl', entry))
        initial_sl = trade.get('initial_sl', current_sl)

        # تحديث أفضل سعر
        if side == 'buy':
            best_price = max(trade.get('best_price', entry), current_price)
        else:
            best_price = min(trade.get('best_price', entry), current_price)
            
        trade['best_price'] = best_price

        # حساب المخاطرة الأساسية
        risk = abs(entry - initial_sl)
        if risk <= 0:
            return None

        # حساب مضاعف العائد (R-Multiple)
        if side == 'buy':
            best_r = (best_price - entry) / risk
        else:
            best_r = (entry - best_price) / risk

        # لا نحرك الستوب قبل 1R
        if best_r < self.break_even_at_r:
            return None
            
        if not trade['trailing_active']:
            trade['trailing_active'] = True
            logger.info(f"[Trailing] 🟡 تفعيل الستوب المتحرك لـ {symbol} | أفضل R وصل له: {best_r:.2f}R")
            self._audit(
                symbol, "ACTIVATED",
                {"best_r": best_r, "current_price": current_price, "current_sl": current_sl},
            )

        # جلب بيانات الشموع
        try:
            bars_df = client.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            if bars_df is None or len(bars_df) < 20:
                return None
        except Exception as e:
            logger.error(f"[Trailing] خطأ في جلب بيانات الشموع للزوج {symbol}: {e}")
            return None

        atr = self.calculate_atr(bars_df)
        candidates = []

        if side == 'buy':
            # 1) Breakeven + Slippage
            candidates.append(entry * 1.001)

            # 2) Structure trailing بعد 1.5R
            if best_r >= self.structure_trailing_at_r:
                last_swing_low = self.get_confirmed_swing(bars_df, 'buy')
                if last_swing_low:
                    candidates.append(last_swing_low - atr * self.structure_atr_buffer)

            # 3) ATR trailing
            multiplier = self.atr_trail_multiplier_tight if best_r >= self.atr_tight_trailing_at_r else self.atr_trail_multiplier_normal
            candidates.append(best_price - atr * multiplier)

            new_sl = max(candidates)
            
            # لا تسمح بتراجع الستوب
            if new_sl <= current_sl:
                return None
                
            # تحديث فقط إذا كان الفرق يستحق
            if (new_sl - current_sl) / current_sl < self.min_sl_update_pct:
                return None
                
        else: # sell
            # 1) Breakeven + Slippage
            candidates.append(entry * 0.999)

            # 2) Structure trailing بعد 1.5R
            if best_r >= self.structure_trailing_at_r:
                last_swing_high = self.get_confirmed_swing(bars_df, 'sell')
                if last_swing_high:
                    candidates.append(last_swing_high + atr * self.structure_atr_buffer)

            # 3) ATR trailing
            multiplier = self.atr_trail_multiplier_tight if best_r >= self.atr_tight_trailing_at_r else self.atr_trail_multiplier_normal
            candidates.append(best_price + atr * multiplier)

            new_sl = min(candidates)
            
            # لا تسمح بتراجع الستوب
            if new_sl >= current_sl:
                return None
                
            # تحديث فقط إذا كان الفرق يستحق
            if (current_sl - new_sl) / current_sl < self.min_sl_update_pct:
                return None

        # تطبيق التحديث على المنصة
        try:
            normalized_sl = client.normalize_price(symbol, new_sl)
            if normalized_sl is None:
                return None
            new_sl = normalized_sl
            position_side = "LONG" if side == "buy" else "SHORT"
            amount = client.get_position_amount(
                symbol,
                position_side=(
                    position_side if getattr(client, "_is_hedge_mode", False) else None
                ),
            )
            if not amount or amount <= 0:
                logger.warning(
                    f"[Trailing] ⚠️ {symbol}: تعذّر جلب كمية المركز من Binance "
                    f"(amount={amount}) — تم تخطي تحديث Trailing Stop لهذه الدورة."
                )
                return None
            # من هنا فصاعداً amount مضمون > 0
            # ── تجاهل new_sl إذا كان نفس القيمة الفاشلة سابقاً ──────
            if failed_sl and abs(new_sl - failed_sl) / max(failed_sl, 0.0001) < 0.002:
                logger.debug(f"[Trailing] تجاهل new_sl={new_sl:.5f} (نفس القيمة الفاشلة سابقاً {failed_sl:.5f})")
                return None

            logger.info(f"[Trailing] محاولة وضع ستوب جديد لـ {symbol} عند {new_sl}...")

            if side == 'buy' and current_price <= new_sl:
                logger.warning(
                    f"[Trailing] 🚨 السعر الحالي ({current_price:.5f}) كسر الستوب المتحرك ({new_sl:.5f}) "
                    f"لـ {symbol}. جاري الإغلاق ماركت لحماية الأرباح!"
                )
                self._persist_close_intent(symbol, "BOT_TRAILING_STOP_BREACH")
                close_ok = client.close_position(symbol, side.lower(), amount)
                self._audit(
                    symbol, "CLOSE_TRIGGERED" if close_ok else "CLOSE_FAILED",
                    {"reason": "BOT_TRAILING_STOP_BREACH", "current_price": current_price,
                     "candidate_sl": new_sl, "amount": amount}, durable=True,
                )
                if close_ok:
                    self.unregister_trade(symbol)
                    return new_sl
                logger.critical(
                    f"[Trailing] فشل تأكيد إغلاق {symbol}; سيبقى تحت المتابعة والحماية."
                )
                return None

            if side == 'sell' and current_price >= new_sl:
                logger.warning(
                    f"[Trailing] 🚨 السعر الحالي ({current_price:.5f}) كسر الستوب المتحرك ({new_sl:.5f}) "
                    f"لـ {symbol}. جاري الإغلاق ماركت لحماية الأرباح!"
                )
                self._persist_close_intent(symbol, "BOT_TRAILING_STOP_BREACH")
                close_ok = client.close_position(symbol, side.lower(), amount)
                self._audit(
                    symbol, "CLOSE_TRIGGERED" if close_ok else "CLOSE_FAILED",
                    {"reason": "BOT_TRAILING_STOP_BREACH", "current_price": current_price,
                     "candidate_sl": new_sl, "amount": amount}, durable=True,
                )
                if close_ok:
                    self.unregister_trade(symbol)
                    return new_sl
                logger.critical(
                    f"[Trailing] فشل تأكيد إغلاق {symbol}; سيبقى تحت المتابعة والحماية."
                )
                return None

            replaced_price = client.replace_sl_order(symbol, side.upper(), amount, new_sl)
            ok_new = bool(replaced_price)
            
            if ok_new:
                new_sl = float(replaced_price)
                trade['current_sl'] = new_sl
                if not self._persist_runtime_state(symbol):
                    logger.critical(
                        f"[Trailing] تم تحديث SL على Binance لـ {symbol} لكن تعذر حفظ حالته محلياً."
                    )
                logger.info(f"[Trailing] 📈 تحديث SL لـ {symbol} | R: {best_r:.2f} | القديم: {current_sl:.5f} → الجديد: {new_sl:.5f}")
                self._audit(
                    symbol, "SL_REPLACED",
                    {"best_r": best_r, "old_sl": current_sl, "new_sl": new_sl,
                     "best_price": best_price, "atr": atr, "amount": amount}, durable=True,
                )
                return new_sl
            else:
                logger.critical(f"[Trailing] تعذر استبدال SL لـ {symbol}؛ الوقف القديم بقي قائماً عند {current_sl}")
                trade['_failed_sl'] = new_sl
                trade['_failed_sl_count'] = trade.get('_failed_sl_count', 0) + 1
                self._audit(
                    symbol, "SL_REPLACE_FAILED",
                    {"best_r": best_r, "old_sl": current_sl, "requested_sl": new_sl,
                     "failure_count": trade['_failed_sl_count']}, durable=True,
                )
                if trade.get('_failed_sl_count', 0) >= 3:
                    trade['_sl_retry_after'] = time.time() + 300
                    trade['_failed_sl_count'] = 0
                    logger.warning(
                        f"[Trailing] 3 فشل متتالي لـ {symbol} — "
                        f"تجميد محاولات Trailing Stop لمدة 5 دقائق"
                    )
                        
            return None
        except Exception as e:
            logger.error(f"[Trailing] فشل وضع أمر SL جديد لـ {symbol}: {e}")

        return None

    def get_trade_info(self, symbol: str) -> dict | None:
        return self._trades.get(symbol)

    def has_trade(self, symbol: str) -> bool:
        return symbol in self._trades

    def get_current_sl(self, symbol: str) -> float | None:
        trade = self._trades.get(symbol)
        return trade.get('current_sl') if trade else None
