from core.logger import logger
from core.config import Config
import math
import threading

def entry_sizing_balances(balance):
    """Read real equity and free margin; missing equity never becomes free cash."""
    try:
        info = balance.get("info") or {}
        usdt = balance.get("USDT") or {}
        equity = next((info[key] for key in ("totalMarginBalance", "totalWalletBalance")
                       if info.get(key) is not None), usdt.get("total"))
        available = info.get("availableBalance")
        if available is None:
            available = usdt.get("free")
        equity, available = float(equity), float(available)
        if not math.isfinite(equity) or not math.isfinite(available) or equity <= 0 or available < 0:
            return 0.0, 0.0
        return equity, available
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0, 0.0


class RiskManager:
    """
    إدارة المخاطر الذكية المستندة إلى السيولة (SMC Smart Risk Manager)
    """
    def __init__(self):
        self.risk_percent = Config.RISK_PER_TRADE_PERCENT / 100.0  # 1%
        self.min_rr = 1.5 # الحد الأدنى لنسبة العائد للمخاطرة
        self.tp_front_run_pct = 0.0015  # الخروج قبل السيولة بـ 0.15% (Front Running)
        # Scanner وMonitor يشتركان في الكائن نفسه. هذه النتائج المؤقتة يجب أن
        # تكون خاصة بكل thread حتى لا يستبدل أحدهما خطة الآخر بين حسابين.
        self._thread_state = threading.local()
        self.last_block_reason = None
        self.last_plan = {}

    @property
    def last_block_reason(self):
        return getattr(self._thread_state, "last_block_reason", None)

    @last_block_reason.setter
    def last_block_reason(self, value):
        self._thread_state.last_block_reason = value

    @property
    def last_plan(self):
        return getattr(self._thread_state, "last_plan", {})

    @last_plan.setter
    def last_plan(self, value):
        self._thread_state.last_plan = dict(value or {})

    def calculate_position_size(
        self,
        balance: float,
        current_price: float,
        custom_risk_percent: float = None,
        atr: float = None,
        sl: float = None,
        leverage: float = None,
        entry_margin_percent: float = None,
        available_balance: float = None,
    ):
        """Size live entries from a margin target, bounded by stop-risk and cash.

        entry_margin_percent is in percentage points (1 means 1% of equity).
        Omitting it preserves the historical risk-based research interface.
        """
        if entry_margin_percent is not None:
            try:
                equity = float(balance)
                price = float(current_price)
                lev = float(leverage if leverage is not None else Config.LEVERAGE)
                margin_pct = float(entry_margin_percent)
                cash = float(available_balance if available_balance is not None else equity)
                risk_pct = float(custom_risk_percent if custom_risk_percent is not None else self.risk_percent)
                stop = float(sl)
                max_notional_pct = float(Config.MAX_NOTIONAL_BALANCE_PCT)
                min_stop_pct = float(Config.MIN_SL_DISTANCE_PCT)
                values = (equity, price, lev, margin_pct, cash, risk_pct, stop,
                          max_notional_pct, min_stop_pct)
                if (not all(math.isfinite(v) and v > 0 for v in values)
                        or margin_pct > 100 or price == stop):
                    return 0.0
                risk_distance = max(abs(price - stop), price * min_stop_pct)
                target_margin = equity * margin_pct / 100.0
                notional = min(target_margin * lev, cash * 0.95 * lev,
                               equity * lev, equity * max_notional_pct,
                               equity * risk_pct / risk_distance * price)
                amount = notional / price
                if not math.isfinite(amount) or amount <= 0:
                    return 0.0
                logger.info(
                    "[Sizing] FIXED_MARGIN equity=%.4f target_margin_pct=%.4f "
                    "target_margin=%.4f planned_margin=%.4f leverage=%.2f notional=%.4f",
                    equity, margin_pct, target_margin, notional / lev, lev, notional,
                )
                return amount
            except (TypeError, ValueError, OverflowError, ZeroDivisionError):
                return 0.0

        if not balance or balance <= 0 or not current_price or current_price <= 0:
            return 0

        risk_pct = custom_risk_percent if custom_risk_percent is not None else self.risk_percent
        # إعداد Telegram/State هو مصدر الرافعة في التشغيل الحي. تبقى Config
        # قيمة افتراضية للاختبارات/الباك تست فقط.
        leverage = float(
            leverage if leverage is not None else getattr(Config, 'LEVERAGE', 5)
        )

        # ① الحد الأقصى المطلق للـ notional كنسبة من الرصيد (30% افتراضياً)
        max_notional_pct = float(getattr(Config, 'MAX_NOTIONAL_BALANCE_PCT', 0.30))
        max_safe_notional = balance * max_notional_pct

        # ─── الخيار الأول: Risk-Based Sizing (إذا تم تمرير الستوب لوز) ───
        if sl and sl > 0:
            sl_distance = abs(current_price - sl)

            # ② الحد الأدنى لمسافة SL = 0.3% من السعر — يحمي من SL ضيق جداً يضخّم الحجم
            min_sl_pct   = float(getattr(Config, 'MIN_SL_DISTANCE_PCT', 0.003))
            min_sl_dist  = current_price * min_sl_pct
            if sl_distance < min_sl_dist and sl_distance > 0:
                logger.warning(
                    f"[RiskManager] ⚠️ مسافة SL ضيقة جداً ({sl_distance:.6f}) "
                    f"< الحد الأدنى ({min_sl_dist:.6f} = {min_sl_pct*100:.1f}% من السعر) "
                    f"→ تعديل تلقائي لمنع تضخم الحجم."
                )
                sl_distance = min_sl_dist

            if sl_distance > 0:
                risk_amount_usd = balance * risk_pct

                # الحجم المحسوب رياضياً
                notional_size = (risk_amount_usd / sl_distance) * current_price

                # ③ تطبيق الحدود القصوى بالترتيب (الأصغر يفوز)
                max_notional_by_leverage = balance * leverage
                final_notional = min(notional_size, max_notional_by_leverage, max_safe_notional)

                amount = final_notional / current_price

                # تحذير إذا تم تقليص الحجم بسبب الحد الأقصى
                was_capped = final_notional < notional_size
                cap_source = ""
                if was_capped:
                    if final_notional == max_safe_notional:
                        cap_source = f"[حد الرصيد {max_notional_pct*100:.0f}%]"
                    else:
                        cap_source = f"[حد الرافعة {leverage}x]"
                    logger.warning(
                        f"[RiskManager] ✂️ تم تقليص الحجم: {notional_size:.2f}$ → {final_notional:.2f}$ {cap_source}"
                    )

                logger.info(
                    f"حساب الكمية (Risk-Based): رصيد={balance:.2f}$, مخاطرة={risk_amount_usd:.2f}$, "
                    f"مسافة_SL={sl_distance:.5f}, حجم_صفقة={final_notional:.2f}$ "
                    f"({final_notional/balance*100:.1f}% من الرصيد), كمية={amount:.6f}"
                )
                return amount

        # ─── الخيار الثاني: Fixed Margin Fallback (سقوط آمن إذا لم يُمرَّر SL) ───
        margin_per_trade = balance * risk_pct
        notional_size    = margin_per_trade * leverage

        # تطبيق الحد الأقصى على الـ Fallback أيضاً
        final_notional = min(notional_size, max_safe_notional)
        amount = final_notional / current_price

        logger.info(
            f"حساب الكمية (Fixed Margin): رصيد={balance:.2f}$, هامش={margin_per_trade:.2f}$, "
            f"رافعة={leverage}x, حجم_صفقة={final_notional:.2f}$ "
            f"({final_notional/balance*100:.1f}% من الرصيد), كمية={amount:.6f}"
        )
        return amount


    def calculate_trade_plan(self, side: str, entry: float, sl: float, market_levels: dict, min_rr: float = None):
        """
        حساب خطة التداول باختيار أفضل مجمع سيولة يعطي RR ممتاز
        """
        min_rr = float(min_rr if min_rr is not None else self.min_rr)
        risk = self._risk(side, entry, sl)
        if risk <= 0:
            logger.warning(f"خطر: الستوب لوز {sl} غير منطقي للدخول {entry} من جهة {side}")
            return {"valid": False, "reason": "Invalid risk", "entry": entry, "sl": sl, "min_rr": min_rr}

        targets = self._collect_liquidity_targets(side, entry, market_levels)
        if not targets:
            logger.warning("لا توجد مجمعات سيولة (أهداف) متاحة أعلى/أسفل السعر الحالي")
            return {
                "valid": False, "reason": "No SMC target found",
                "entry": entry, "sl": sl, "risk": risk, "min_rr": min_rr,
                "evaluated_targets": [],
            }

        # ترتيب الأهداف من الأقرب إلى الأبعد
        targets = sorted(targets, key=lambda x: abs(x["price"] - entry))
        
        scored_targets = []
        for target in targets:
            tp = self._front_run_tp(side, target["price"])
            reward = self._reward(side, entry, tp)
            
            # لا نقبل هدفاً أصبح غير مربح بعد front-run
            if reward <= 0:
                continue

            rr = reward / risk if risk > 0 else 0
            
            target["tp"] = tp
            target["rr"] = rr
            scored_targets.append(target)

        if not scored_targets:
            return {
                "valid": False, "reason": "No actionable SMC target found",
                "entry": entry, "sl": sl, "risk": risk, "min_rr": min_rr,
                "evaluated_targets": [],
            }

        # مساحة الصفقة يحددها أول مستوى معاكس قابل للتداول، لا هدف أبعد نتمنى
        # أن يخترقه السعر. تجاهل المستوى الأقرب هو بالضبط ما صنع RR وهمياً.
        nearest_target = scored_targets[0]
        if nearest_target["rr"] < min_rr:
            reason = (
                f"تم حظر الصفقة: أول سيولة مقابلة تعطي RR غير كافٍ "
                f"({nearest_target['rr']:.2f}R < {min_rr:.2f}R). "
                f"النوع: {nearest_target['type']}"
            )
            logger.warning(reason)
            self.last_block_reason = reason  # ← يُخزَّن للمستدعي
            return {
                "valid": False,
                "reason": "First opposing liquidity is too close. Poor RR.",
                "blocking_target": nearest_target,
                "rr": nearest_target["rr"],
                "entry": entry, "sl": sl, "risk": risk, "min_rr": min_rr,
                "evaluated_targets": scored_targets[:20],
            }

        selected = nearest_target
        logger.info(f"🎯 تم اختيار هدف سيولة SMC! الدخول: {entry}, الوقف: {sl}, الهدف: {selected['tp']}, RR: {selected['rr']:.2f}, نوع الهدف: {selected['type']}")

        return {
            "valid": True,
            "entry": entry,
            "sl": sl,
            "tp": selected["tp"],
            "rr": round(selected["rr"], 2),
            "target_type": selected["type"],
            "target_raw_price": selected["price"],
            "risk": risk,
            "min_rr": min_rr,
            "nearest_opposing_target": nearest_target,
            "evaluated_targets": scored_targets[:20],
        }

    def _risk(self, side, entry, sl):
        return entry - sl if side == "buy" else sl - entry

    def _reward(self, side, entry, tp):
        return tp - entry if side == "buy" else entry - tp

    def _front_run_tp(self, side, raw_target_price):
        """نخرج قبل الهدف المؤسسي بمسافة طفيفة لضمان التنفيذ"""
        return raw_target_price * (1 - self.tp_front_run_pct) if side == "buy" else raw_target_price * (1 + self.tp_front_run_pct)

    def _collect_liquidity_targets(self, side, entry, levels):
        targets = []
        if not levels:
            return targets

        if side == "buy":
            for fvg in levels.get("bearish_fvgs", []):
                if fvg["low"] > entry and fvg["low"] > 0:
                    targets.append({"type": "bearish_fvg", "price": fvg["low"], "quality": 1.1})
            for ob in levels.get("bearish_obs", []):
                if ob["low"] > entry and ob["low"] > 0:
                    targets.append({"type": "bearish_ob_supply", "price": ob["low"], "quality": 1.3})
        else:
            for fvg in levels.get("bullish_fvgs", []):
                if fvg["high"] < entry and fvg["high"] > 0:
                    targets.append({"type": "bullish_fvg", "price": fvg["high"], "quality": 1.1})
            for ob in levels.get("bullish_obs", []):
                if ob["high"] < entry and ob["high"] > 0:
                    targets.append({"type": "bullish_ob_demand", "price": ob["high"], "quality": 1.3})

        if side == "buy":
            for swing in levels.get("swing_highs", []):
                price = float(swing.get("price", 0) or 0)
                if price > entry:
                    targets.append({"type": "swing_high_liquidity", "price": price, "quality": 1.0})
        else:
            for swing in levels.get("swing_lows", []):
                price = float(swing.get("price", 0) or 0)
                if 0 < price < entry:
                    targets.append({"type": "swing_low_liquidity", "price": price, "quality": 1.0})
                    
        return targets

    @staticmethod
    def _finite_positive(value, default=0.0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return float(default)
        return value if math.isfinite(value) and value > 0 else float(default)

    def _legacy_initial_stop_plan(
        self, side, entry_price, safe_atr, use_atr,
        invalidation_level=None, market_levels=None,
    ):
        """Reproduce the pre-V1 stop exactly for audit/rollback evidence."""
        atr_sl_mult = float(getattr(Config, "ATR_SL_MULTIPLIER", 1.5))
        min_sl_pct = 0.008
        max_sl_pct = float(getattr(Config, "MAX_SL_PCT", 0.05))
        candidates = []
        invalidation = self._finite_positive(invalidation_level)
        if invalidation:
            candidates.append({
                "source": "invalidation_level",
                "stop": invalidation - safe_atr * 0.3
                if side == "buy" else invalidation + safe_atr * 0.3,
            })
        levels = market_levels or {}
        families = (
            (("bullish_obs", "bullish_fvgs"), "low", -1)
            if side == "buy"
            else (("bearish_obs", "bearish_fvgs"), "high", 1)
        )
        for family in families[0]:
            for index, item in enumerate(levels.get(family, []) or []):
                level = self._finite_positive((item or {}).get(families[1]))
                if not level:
                    continue
                valid_side = level < entry_price if side == "buy" else level > entry_price
                if valid_side:
                    candidates.append({
                        "source": f"{family}[{index}]",
                        "stop": level + families[2] * safe_atr * 0.2,
                    })
        if use_atr:
            candidates.append({
                "source": "atr_fallback",
                "stop": entry_price - safe_atr * atr_sl_mult
                if side == "buy" else entry_price + safe_atr * atr_sl_mult,
            })
        valid = [
            item for item in candidates
            if (0 < item["stop"] < entry_price if side == "buy"
                else item["stop"] > entry_price > 0)
        ]
        if valid:
            selected = min(valid, key=lambda item: item["stop"]) if side == "buy" else max(
                valid, key=lambda item: item["stop"]
            )
            stop = float(selected["stop"])
            selected_source = selected["source"]
        else:
            stop = entry_price - safe_atr * atr_sl_mult if side == "buy" else entry_price + safe_atr * atr_sl_mult
            selected_source = "atr_fallback"
        before_clamp = float(stop)
        min_distance = entry_price * min_sl_pct
        max_distance = min(
            entry_price * max_sl_pct,
            safe_atr * float(getattr(Config, "MAX_SL_ATR_MULTIPLE", 4.0)),
        )
        distance = abs(entry_price - stop)
        clamp = "NONE"
        if distance < min_distance:
            stop = entry_price - min_distance if side == "buy" else entry_price + min_distance
            clamp = "MIN_DISTANCE_EXPANSION"
        elif distance > max_distance:
            stop = entry_price - max_distance if side == "buy" else entry_price + max_distance
            clamp = "MAX_DISTANCE_TIGHTENING"
        invalidation_inside = bool(
            invalidation and (
                (side == "buy" and stop >= invalidation)
                or (side == "sell" and stop <= invalidation)
            )
        )
        return float(stop), {
            "policy": "LEGACY_INITIAL_STOP",
            "entry": float(entry_price), "side": side,
            "atr": float(safe_atr), "atr_multiplier": atr_sl_mult,
            "invalidation_level": invalidation, "candidates": candidates,
            "selected_source": selected_source,
            "selected_before_clamp": before_clamp,
            "min_distance": float(min_distance), "max_distance": float(max_distance),
            "final_sl": float(stop),
            "final_risk_distance": float(abs(entry_price - stop)),
            "clamp": clamp,
            "clamp_moved_stop_inside_invalidation": invalidation_inside,
        }

    def _structural_initial_stop_plan(
        self, side, entry_price, safe_atr, *, invalidation_level=None,
        market_levels=None, setup_anchor=None, tick_size=0.0, spread=0.0,
    ):
        """Place the stop outside the causal thesis level; never clamp it inward."""
        levels = dict(market_levels or {})
        anchor = dict(setup_anchor or {})
        tick = self._finite_positive(tick_size)
        spread = self._finite_positive(spread)
        invalidation = self._finite_positive(invalidation_level)
        candidates, rejected = [], []
        if side == "buy":
            required_anchor_type, anchor_key = "bullish_ob", "low"
            families = (("bullish_obs", "low", "15m_bullish_ob"),
                        ("swing_lows", "price", "15m_swing_low"))
        else:
            required_anchor_type, anchor_key = "bearish_ob", "high"
            families = (("bearish_obs", "high", "15m_bearish_ob"),
                        ("swing_highs", "price", "15m_swing_high"))

        def add(raw, source, created_at, thesis_anchor=False):
            level = self._finite_positive(raw)
            record = {
                "structural_level": level, "level_source": source,
                "timeframe": "15m", "created_at": str(created_at or ""),
                "thesis_anchor": bool(thesis_anchor),
            }
            if not level:
                record["reject_reason"] = "INVALID_PRICE"
            elif not record["created_at"]:
                record["reject_reason"] = "MISSING_CAUSAL_TIMESTAMP"
            elif (side == "buy" and level >= entry_price) or (side == "sell" and level <= entry_price):
                record["reject_reason"] = "WRONG_SIDE_OF_ENTRY"
            else:
                candidates.append(record)
                return
            rejected.append(record)

        if anchor:
            anchor_type = str(anchor.get("type") or "")
            if anchor_type == required_anchor_type:
                add(anchor.get(anchor_key), f"setup_anchor:{anchor_type}", anchor.get("created_at"), True)
            else:
                rejected.append({
                    "structural_level": self._finite_positive(anchor.get(anchor_key)),
                    "level_source": f"setup_anchor:{anchor_type or 'unknown'}",
                    "timeframe": "15m", "created_at": str(anchor.get("created_at") or ""),
                    "thesis_anchor": True, "reject_reason": "ANCHOR_SIDE_MISMATCH",
                })
        for family, key, source in families:
            for index, item in enumerate(levels.get(family, []) or []):
                item = item or {}
                add(item.get(key), f"{source}[{index}]", item.get("created_at"))

        selected = next((item for item in candidates if item["thesis_anchor"]), None)
        selection_reason = "SETUP_ANCHOR"
        if selected is None and invalidation:
            tolerance = max(tick, safe_atr * 0.05)
            aligned = [item for item in candidates if abs(item["structural_level"] - invalidation) <= tolerance]
            if aligned:
                selected = min(aligned, key=lambda item: abs(item["structural_level"] - invalidation))
                selection_reason = "CAUSAL_LEVEL_ALIGNED_WITH_INVALIDATION"
        for item in candidates:
            if item is not selected:
                rejected.append({**item, "reject_reason": "NOT_LINKED_TO_CURRENT_THESIS"})
        if selected is None:
            return None, {
                "policy": "STRUCTURAL_INITIAL_STOP_V1", "entry": float(entry_price),
                "side": side, "atr": float(safe_atr),
                "invalidation_level": invalidation,
                "all_structural_candidates": candidates,
                "rejected_structural_candidates": rejected,
                "reason": "NO_VALID_STRUCTURAL_INVALIDATION", "clamp": "NONE",
            }

        structural_level = float(selected["structural_level"])
        buffer = max(safe_atr * 0.3, tick * 2.0, spread)
        raw_stop = structural_level - buffer if side == "buy" else structural_level + buffer
        if tick:
            units = raw_stop / tick
            stop = math.floor(units + 1e-12) * tick if side == "buy" else math.ceil(units - 1e-12) * tick
        else:
            stop = raw_stop
        invariant_ok = stop < structural_level if side == "buy" else stop > structural_level
        side_ok = stop > 0 and (stop < entry_price if side == "buy" else stop > entry_price)
        return (float(stop) if invariant_ok and side_ok else None), {
            "policy": "STRUCTURAL_INITIAL_STOP_V1",
            "entry": float(entry_price), "side": side, "atr": float(safe_atr),
            "atr_multiplier": float(getattr(Config, "ATR_SL_MULTIPLIER", 1.5)),
            "invalidation_level": invalidation,
            "selected_structural_level": structural_level,
            "structural_level_type": selected["level_source"],
            "structural_level_timeframe": selected["timeframe"],
            "structural_level_created_at": selected["created_at"],
            "selection_reason": selection_reason,
            "all_structural_candidates": candidates,
            "rejected_structural_candidates": rejected,
            "volatility_buffer": float(buffer),
            "buffer_components": {"atr_0_3": float(safe_atr * 0.3), "two_ticks": float(tick * 2), "spread": float(spread)},
            "tick_size": float(tick), "raw_structural_stop": float(raw_stop),
            "final_sl": float(stop), "final_risk_distance": float(abs(entry_price - stop)),
            "distance_pct": float(abs(entry_price - stop) / entry_price),
            "distance_atr": float(abs(entry_price - stop) / safe_atr),
            "clamp": "NONE", "clamp_attempted": False,
            "structural_invariant_ok": bool(invariant_ok), "side_invariant_ok": bool(side_ok),
            "reason": "VALID" if invariant_ok and side_ok else "STRUCTURAL_STOP_INVARIANT_FAILED",
        }

    def _structural_initial_stop_v2_plan(
        self, side, entry_price, safe_atr, *, invalidation_level=None,
        market_levels=None, setup_anchor=None, tick_size=0.0, spread=0.0,
    ):
        """Select thesis invalidation rather than automatically selecting entry anchor.

        All supplied market levels are produced from closed candles before the
        entry decision.  V2 ranks explicit invalidation and meaningful swings
        above a local entry OB, then applies a volatility/tick/spread buffer.
        """
        levels = dict(market_levels or {})
        anchor = dict(setup_anchor or {})
        tick = self._finite_positive(tick_size)
        spread = self._finite_positive(spread)
        invalidation = self._finite_positive(invalidation_level)
        candidates, rejected = [], []
        is_long = side == "buy"
        anchor_type = str(anchor.get("type") or "")
        anchor_key = "low" if is_long else "high"
        expected_anchor = "bullish_ob" if is_long else "bearish_ob"
        entry_anchor_level = self._finite_positive(anchor.get(anchor_key)) if anchor else None
        anchor_tolerance = max(tick, safe_atr * 0.05)

        def add(raw, source, *, timeframe="15m", created_at="", relation="STRUCTURE", strength=0.0):
            level = self._finite_positive(raw)
            record = {
                "structural_level": level,
                "level_source": str(source),
                "timeframe": str(timeframe or "15m"),
                "created_at": str(created_at or ""),
                "relation_to_thesis": str(relation),
                "strength": float(strength or 0.0),
            }
            if not level:
                record["reject_reason"] = "INVALID_PRICE"
            elif (is_long and level >= entry_price) or (not is_long and level <= entry_price):
                record["reject_reason"] = "WRONG_SIDE_OF_ENTRY"
            else:
                record["distance_pct"] = abs(entry_price - level) / entry_price
                record["distance_atr"] = abs(entry_price - level) / safe_atr
                candidates.append(record)
                return
            rejected.append(record)

        if invalidation:
            invalidation_is_anchor_alias = bool(
                entry_anchor_level
                and abs(invalidation - entry_anchor_level) <= anchor_tolerance
                and not bool(anchor.get("is_thesis_invalidation", False))
            )
            add(
                invalidation,
                (
                    "setup_invalidation_matches_entry_anchor"
                    if invalidation_is_anchor_alias else "setup_invalidation"
                ),
                created_at=anchor.get("created_at"),
                relation=(
                    "ENTRY_ANCHOR_ALIAS"
                    if invalidation_is_anchor_alias else "EXPLICIT_THESIS_INVALIDATION"
                ),
                strength=65.0 if invalidation_is_anchor_alias else 100.0,
            )

        if anchor:
            add(
                anchor.get(anchor_key), f"entry_anchor:{anchor_type or 'unknown'}",
                created_at=anchor.get("created_at"), relation="ENTRY_ANCHOR",
                strength=65.0 if anchor_type == expected_anchor else 35.0,
            )

        families = (
            (("swing_lows", "price", "swing_low", 90.0),
             ("bullish_obs", "low", "bullish_ob", 80.0))
            if is_long else
            (("swing_highs", "price", "swing_high", 90.0),
             ("bearish_obs", "high", "bearish_ob", 80.0))
        )
        for family, key, label, base_strength in families:
            for index, item in enumerate(levels.get(family, []) or []):
                item = item or {}
                age = float(item.get("age") or 0.0)
                supplied_strength = float(item.get("strength") or 0.0)
                recency_bonus = max(0.0, 5.0 - min(age, 5.0))
                raw_level = self._finite_positive(item.get(key))
                is_anchor_alias = bool(
                    entry_anchor_level
                    and raw_level
                    and abs(raw_level - entry_anchor_level) <= anchor_tolerance
                )
                add(
                    raw_level, f"{label}[{index}]",
                    created_at=item.get("created_at"),
                    relation="ENTRY_ANCHOR_ALIAS" if is_anchor_alias else "THESIS_STRUCTURE",
                    strength=(65.0 if is_anchor_alias else base_strength + supplied_strength + recency_bonus),
                )

        selectable = [
            item for item in candidates
            if item.get("relation_to_thesis") not in {"ENTRY_ANCHOR", "ENTRY_ANCHOR_ALIAS"}
        ]
        if not selectable:
            for item in candidates:
                rejected.append({**item, "reject_reason": "ENTRY_ANCHOR_NOT_THESIS_INVALIDATION"})
            return None, {
                "policy": "STRUCTURAL_INITIAL_STOP_V2", "entry": float(entry_price),
                "side": side, "atr": float(safe_atr),
                "invalidation_level": invalidation,
                "entry_anchor_level": entry_anchor_level,
                "selected_thesis_invalidation": None,
                "all_structural_candidates": candidates,
                "rejected_structural_candidates": rejected,
                "reason": "NO_VALID_THESIS_INVALIDATION", "clamp": "NONE",
            }

        # Higher thesis relevance wins.  For equal relevance, prefer the more
        # conservative level (lower support for LONG, higher resistance for SHORT).
        selected = sorted(
            selectable,
            key=lambda item: (
                item["strength"],
                -item["structural_level"] if is_long else item["structural_level"],
            ),
            reverse=True,
        )[0]
        for item in candidates:
            if item is not selected:
                rejected.append({**item, "reject_reason": "LOWER_THESIS_RELEVANCE"})

        structural_level = float(selected["structural_level"])
        buffer = max(safe_atr * 0.3, tick * 2.0, spread)
        raw_stop = structural_level - buffer if is_long else structural_level + buffer
        if tick:
            units = raw_stop / tick
            stop = math.floor(units + 1e-12) * tick if is_long else math.ceil(units - 1e-12) * tick
        else:
            stop = raw_stop
        invariant_ok = stop < structural_level if is_long else stop > structural_level
        side_ok = stop > 0 and (stop < entry_price if is_long else stop > entry_price)
        return (float(stop) if invariant_ok and side_ok else None), {
            "policy": "STRUCTURAL_INITIAL_STOP_V2",
            "entry": float(entry_price), "side": side, "atr": float(safe_atr),
            "invalidation_level": invalidation,
            "entry_anchor_level": entry_anchor_level,
            "selected_thesis_invalidation": structural_level,
            "thesis_invalidation_source": selected["level_source"],
            "selected_structural_level": structural_level,
            "structural_level_type": selected["level_source"],
            "structural_level_timeframe": selected["timeframe"],
            "structural_level_created_at": selected["created_at"],
            "relation_to_thesis": selected["relation_to_thesis"],
            "selected_strength": selected["strength"],
            "selection_reason": "HIGHEST_THESIS_INVALIDATION_RELEVANCE",
            "all_structural_candidates": candidates,
            "rejected_structural_candidates": rejected,
            "volatility_buffer": float(buffer),
            "buffer_components": {
                "atr_0_3": float(safe_atr * 0.3), "two_ticks": float(tick * 2),
                "spread": float(spread),
            },
            "tick_size": float(tick), "raw_structural_stop": float(raw_stop),
            "final_sl": float(stop), "final_risk_distance": float(abs(entry_price - stop)),
            "distance_pct": float(abs(entry_price - stop) / entry_price),
            "distance_atr": float(abs(entry_price - stop) / safe_atr),
            "clamp": "NONE", "clamp_attempted": False,
            "structural_invariant_ok": bool(invariant_ok),
            "side_invariant_ok": bool(side_ok),
            "reason": "VALID" if invariant_ok and side_ok else "STRUCTURAL_STOP_INVARIANT_FAILED",
        }

    @staticmethod
    def evaluate_structural_stop_account_risk(*, equity, amount, entry_price, stop_price, round_trip_cost_pct=None):
        """Check actual rounded exposure without changing stop or sizing semantics."""
        try:
            equity, amount, entry, stop = map(float, (equity, amount, entry_price, stop_price))
            costs_pct = float(Config.STRUCTURAL_STOP_ROUND_TRIP_COST_PCT if round_trip_cost_pct is None else round_trip_cost_pct)
            if not all(math.isfinite(v) for v in (equity, amount, entry, stop, costs_pct)):
                raise ValueError("non_finite")
            if equity <= 0 or amount <= 0 or entry <= 0 or stop <= 0 or costs_pct < 0:
                raise ValueError("non_positive")
        except (TypeError, ValueError):
            return {"valid": False, "reason": "STRUCTURAL_STOP_RISK_INPUT_INVALID"}
        stop_loss_usd = amount * abs(entry - stop)
        estimated_cost_usd = amount * entry * costs_pct
        actual_risk_usd = stop_loss_usd + estimated_cost_usd
        actual_risk_pct = actual_risk_usd / equity * 100.0
        hard_cap_pct = float(Config.HARD_MAX_RISK_PER_TRADE_PERCENT)
        return {
            "valid": actual_risk_pct <= hard_cap_pct,
            "reason": "PASS" if actual_risk_pct <= hard_cap_pct else "SKIP_STRUCTURAL_STOP_RISK_TOO_HIGH",
            "equity": equity, "rounded_amount": amount, "entry": entry, "stop": stop,
            "stop_loss_usd": stop_loss_usd,
            "estimated_round_trip_cost_usd": estimated_cost_usd,
            "round_trip_cost_pct": costs_pct, "actual_risk_usd": actual_risk_usd,
            "actual_risk_pct": actual_risk_pct, "hard_risk_cap_pct": hard_cap_pct,
        }

    def calculate_sl_tp(
        self,
        side: str,
        entry_price: float,
        support: float = None,
        resistance: float = None,
        atr: float = None,
        invalidation_level: float = None,
        market_levels: dict = None,
        filter_profile: dict = None,
        setup_anchor: dict = None,
        tick_size: float = 0.0,
        spread: float = 0.0,
    ):
        """
        SL احترافي: يُوضع تحت أقوى دعم هيكلي (OB low / FVG low / Swing Low)
        مع حد أدنى للبُعد (0.8%) لتجنب SL الضيق الذي ضرب الصفقات سابقاً.
        يُرجع SL وهدف الخروج النهائي الحقيقي. مراحل الحماية تُبنى منفصلة
        ولا تغلق أجزاء من المركز.
        """
        # ── حماية: ATR يجب أن يكون موجوداً ─────────────────────────
        atr_sl_mult = getattr(Config, 'ATR_SL_MULTIPLIER', 1.5)

        filter_profile    = filter_profile or {}
        profile_min_rr    = float(filter_profile.get("min_rr", self.min_rr))
        allow_rr_fallback = bool(filter_profile.get("allow_rr_fallback", False))
        fallback_rr       = float(filter_profile.get("fallback_rr") or 1.5)
        nearest_liquidity_checkpoint_only = bool(
            filter_profile.get("nearest_liquidity_checkpoint_only", False)
        )

        use_atr = atr is not None and float(atr) > 0
        safe_atr = float(atr) if use_atr else entry_price * 0.005

        legacy_sl, legacy_stop_plan = self._legacy_initial_stop_plan(
            side, entry_price, safe_atr, use_atr,
            invalidation_level=invalidation_level,
            market_levels=market_levels,
        )
        structural_v2_enabled = bool(
            getattr(Config, "STRUCTURAL_STOP_V2_ENABLED", False)
        )
        structural_enabled = bool(
            getattr(Config, "STRUCTURAL_INITIAL_STOP_V1_ENABLED", False)
        )
        if structural_v2_enabled or structural_enabled:
            planner = (
                self._structural_initial_stop_v2_plan
                if structural_v2_enabled else self._structural_initial_stop_plan
            )
            sl, stop_plan = planner(
                side, entry_price, safe_atr,
                invalidation_level=invalidation_level,
                market_levels=market_levels,
                setup_anchor=setup_anchor,
                tick_size=tick_size,
                spread=spread,
            )
            stop_plan["old_stop"] = legacy_sl
            stop_plan["old_stop_plan"] = legacy_stop_plan
            if sl is None:
                self.last_block_reason = str(
                    stop_plan.get("reason") or "NO_VALID_STRUCTURAL_INVALIDATION"
                )
                self.last_plan = {
                    "valid": False,
                    "reason": self.last_block_reason,
                    "stop_plan": stop_plan,
                }
                logger.warning(f"[StructuralSL] fail-closed: {self.last_block_reason}")
                return None, None
            invalidation_inside_final_stop = not bool(
                stop_plan.get("structural_invariant_ok")
                and stop_plan.get("side_invariant_ok")
            )
            logger.info(
                "[StructuralSL] side=%s entry=%.8f level=%.8f source=%s "
                "buffer=%.8f old_sl=%.8f final_sl=%.8f distance=%.4f%%/%.3fATR clamp=NONE",
                side, entry_price, stop_plan["selected_structural_level"],
                stop_plan["structural_level_type"], stop_plan["volatility_buffer"],
                legacy_sl, sl, stop_plan["distance_pct"] * 100.0,
                stop_plan["distance_atr"],
            )
        else:
            sl, stop_plan = legacy_sl, legacy_stop_plan
            invalidation_inside_final_stop = bool(
                legacy_stop_plan.get("clamp_moved_stop_inside_invalidation")
            )
        risk_amount = abs(entry_price - sl)

        # ── الخطوة 4: بناء 3 أهداف TP احترافية ──────────────────────
        # TP1 = 1.5× المخاطرة | TP2 = 2.5× | TP3 = 4× أو هدف SMC
        if side == 'buy':
            tp1 = entry_price + risk_amount * 1.5
            tp2 = entry_price + risk_amount * 2.5
            tp3_base = entry_price + risk_amount * 4.0
        else:
            tp1 = entry_price - risk_amount * 1.5
            tp2 = entry_price - risk_amount * 2.5
            tp3_base = entry_price - risk_amount * 4.0

        # ─── امسح نتيجة الحساب السابق ───
        self.last_block_reason = None
        self.last_plan = {}

        if invalidation_inside_final_stop:
            self.last_block_reason = (
                "رفض سلامة: حد مسافة SL سيضع الوقف داخل مستوى الإبطال الهيكلي؛ "
                "لا يجوز تحسين RR بتغيير معنى الفكرة"
            )
            self.last_plan = {
                "valid": False,
                "reason": "SL_CLAMP_INSIDE_STRUCTURAL_INVALIDATION",
                "stop_plan": stop_plan,
            }
            logger.warning(f"[SL] {self.last_block_reason}")
            return sl, None

        # البروفايل هو مصدر الحقيقة لقبول هدف السيولة. في الأوضاع الصارمة
        # لا نختلق هدفاً حسابياً إذا لم يوجد هدف SMC يحقق الحد الأدنى المعلن.
        tp3 = tp3_base
        if market_levels:
            plan = self.calculate_trade_plan(
                side, entry_price, sl, market_levels, min_rr=profile_min_rr
            )
            if nearest_liquidity_checkpoint_only:
                # Explicit Demo-only mode: retain the nearest opposing
                # liquidity in the audit as a checkpoint while leaving the
                # existing TrailingStopManager/4R stage authoritative.
                checkpoint = (
                    plan.get("nearest_opposing_target")
                    or plan.get("blocking_target")
                )
                tp3 = tp3_base
                self.last_block_reason = None
                self.last_plan = {
                    "valid": True,
                    "source": "nearest_liquidity_checkpoint_only",
                    "rr": 4.0,
                    "tp": tp3,
                    "target_type": "trailing_manager_final_stage",
                    "liquidity_checkpoint": checkpoint,
                    "evaluated_targets": plan.get("evaluated_targets", []),
                    "stop_plan": stop_plan,
                }
                logger.info(
                    f"[Target] أقرب سيولة checkpoint فقط؛ الهدف الحمائي النهائي "
                    f"يبقى مرحلة الإدارة الأصلية 4R: {tp3:.5f}"
                )
            elif plan.get('valid'):
                smc_tp = plan['tp']
                rr_smc = plan.get('rr', 0)
                tp3 = smc_tp
                self.last_plan = dict(plan)
                self.last_plan["stop_plan"] = stop_plan
                logger.info(
                    f"[Target] هدف السوق الفعلي مستخدم: {tp3:.5f} | "
                    f"RR={rr_smc:.2f} | type={plan.get('target_type')}"
                )
            elif not allow_rr_fallback:
                # calculate_trade_plan قد يكون سجّل المستوى المحدد الذي سد
                # الطريق؛ لا نستبدله برسالة عامة تفقدنا الدليل التشخيصي.
                if not self.last_block_reason:
                    self.last_block_reason = (
                        f"لا يوجد هدف سيولة يحقق الحد الأدنى للبروفايل "
                        f"({profile_min_rr:.2f}R)"
                    )
                logger.warning(f"[RR] رفض الصفقة: {self.last_block_reason}")
                self.last_plan = dict(plan)
                self.last_plan["stop_plan"] = stop_plan
                return sl, None
            else:
                fallback_rr = max(profile_min_rr, fallback_rr)
                tp3 = (
                    entry_price + risk_amount * fallback_rr
                    if side == "buy"
                    else entry_price - risk_amount * fallback_rr
                )
                self.last_plan = {
                    "valid": True,
                    "source": "non_smc_fallback",
                    "rr": fallback_rr,
                    "tp": tp3,
                    "target_type": "calculated_fallback",
                    "stop_plan": stop_plan,
                }
                logger.info(
                    f"[RR] البروفايل غير المؤسسي يسمح بهدف حسابي محدود "
                    f"({fallback_rr:.2f}R)، وليس 4R وهمياً."
                )
        elif not allow_rr_fallback:
            self.last_block_reason = "بيانات مستويات SMC غير متاحة والبروفايل يمنع RR fallback"
            self.last_plan = {
                "valid": False,
                "reason": "SMC_LEVELS_UNAVAILABLE",
                "min_rr": profile_min_rr,
                "stop_plan": stop_plan,
            }
            logger.warning(f"[RR] رفض الصفقة: {self.last_block_reason}")
            return sl, None
        else:
            fallback_rr = max(profile_min_rr, fallback_rr)
            tp3 = (
                entry_price + risk_amount * fallback_rr
                if side == "buy"
                else entry_price - risk_amount * fallback_rr
            )
            self.last_plan = {
                "valid": True,
                "source": "non_smc_fallback",
                "rr": fallback_rr,
                "tp": tp3,
                "target_type": "calculated_fallback",
                "stop_plan": stop_plan,
            }

        # TP الرئيسي = TP3 (للتوافق مع باقي الكود)
        tp_main = tp3
        self.last_plan.setdefault("stop_plan", stop_plan)

        logger.info(
            f"[SL/TP] دخول={entry_price:.5f} | SL={sl:.5f} "
            f"(-{risk_amount/entry_price*100:.2f}%) | الهدف={tp3:.5f} | "
            f"RR_net={risk_amount and (abs(tp3-entry_price)/risk_amount):.2f} | "
            f"نوع={self.last_plan.get('target_type', 'unknown')}"
        )

        return sl, tp_main

    def get_tp_steps(self, side: str, entry_price: float, sl: float, tp3_override: float = None, is_bottom_fishing: bool = False) -> list:
        """
        يُعيد مراحل حماية مرتبة لا تتجاوز هدف السوق النهائي. لا تمثل
        المراحل نسب إغلاق؛ المركز يبقى كاملاً حتى الهدف النهائي أو SL.
        """
        risk_amount = abs(entry_price - sl)
        if risk_amount <= 0:
            return []

        if is_bottom_fishing:
            # أهداف متباعدة جداً لركوب الترند الطويل واصطياد القاع
            if side == 'buy':
                tp1 = entry_price + risk_amount * 3.0
                tp2 = entry_price + risk_amount * 6.0
                tp3 = entry_price + risk_amount * 10.0
            else:
                tp1 = entry_price - risk_amount * 3.0
                tp2 = entry_price - risk_amount * 6.0
                tp3 = entry_price - risk_amount * 10.0
            
            ttp_dev = getattr(Config, 'RUNNER_TRAILING_DEVIATION_PCT', 0.02)
        else:
            tp3 = tp3_override if tp3_override else (
                entry_price + risk_amount * 1.5
                if side == 'buy'
                else entry_price - risk_amount * 1.5
            )
            final_rr = abs(tp3 - entry_price) / risk_amount
            milestone_rrs = [rr for rr in (1.5, 2.5) if rr < final_rr - 1e-9]
            prices = [
                entry_price + risk_amount * rr if side == 'buy'
                else entry_price - risk_amount * rr
                for rr in milestone_rrs
            ]
            prices.append(tp3)
            ttp_dev = getattr(Config, 'TTP_DEVIATION_PCT', 0.005)

            return [
                {
                    "position": index + 1,
                    "label": "FINAL_TARGET" if index == len(prices) - 1 else f"PROTECT_{index + 1}",
                    "price": price,
                    # هذه مراحل حماية للكمية الكاملة، وليست نسب إغلاق.
                    "volume": 0.0,
                    "trailing": {
                        "enabled": bool(getattr(Config, 'TTP_ENABLED', False))
                        if index == len(prices) - 1 else False,
                        "percent": ttp_dev if index == len(prices) - 1 else 0,
                    },
                    "sl_after": "protected",
                    "status": "idle",
                }
                for index, price in enumerate(prices)
            ]

        # قراءة نسب التوزيع من الكونفيج للتناسق مع Config.TP_STEP_VOLUMES
        _tp_vols = list(getattr(Config, 'TP_STEP_VOLUMES', [0.33, 0.33, 0.34]))
        if len(_tp_vols) != 3 or abs(sum(_tp_vols) - 1.0) > 0.01:
            _tp_vols = [0.33, 0.33, 0.34]  # قيمة احتياطية آمنة

        return [
            {"position": 1, "price": tp1, "volume": _tp_vols[0],
             "trailing": {"enabled": False, "percent": 0},
             "sl_after": "breakeven", "status": "idle"},
            {"position": 2, "price": tp2, "volume": _tp_vols[1],
             "trailing": {"enabled": False, "percent": 0},
             "sl_after": "tp1", "status": "idle"},
            {"position": 3, "price": tp3, "volume": _tp_vols[2],
             "trailing": {"enabled": bool(getattr(Config, 'TTP_ENABLED', False)),
                           "percent": ttp_dev},
             "sl_after": "tp2", "status": "idle"},
        ]
