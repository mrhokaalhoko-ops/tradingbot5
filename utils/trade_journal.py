# utils/trade_journal.py

import os
import json
import time
import hashlib
import math
import pandas as pd
from datetime import datetime, timedelta, timezone
from core.config import Config
from core.logger import logger


class TradeJournal:
    """
    يسجل ملف تحقيق لكل صفقة:
    - JSON لتبرير الدخول والخروج.
    - CSV للشموع 1m/5m/15m من قبل الدخول إلى بعد الخروج.
    - CSV للصفقات الدقيقة recent trades إن توفرت.
    """

    def __init__(self):
        self.enabled = getattr(Config, "TRADE_JOURNAL_ENABLED", True)
        self.base_dir = getattr(Config, "TRADE_JOURNAL_DIR", "trade_journal")
        self.pre_minutes = int(getattr(Config, "TRADE_JOURNAL_PRE_MINUTES", 60))
        self.post_minutes = int(getattr(Config, "TRADE_JOURNAL_POST_MINUTES", 60))
        self.fetch_trades_enabled = bool(getattr(Config, "TRADE_JOURNAL_FETCH_TRADES", True))

        raw_tfs = getattr(Config, "TRADE_JOURNAL_TIMEFRAMES", "1m,5m,15m")
        self.timeframes = [x.strip() for x in str(raw_tfs).split(",") if x.strip()]

        os.makedirs(self.base_dir, exist_ok=True)

    def _safe_symbol(self, symbol: str) -> str:
        return str(symbol).replace("/", "").replace(":", "").replace("-", "")

    def _parse_time(self, value):
        if not value:
            return datetime.now(timezone.utc)

        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value)
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt = pd.to_datetime(text).to_pydatetime()
                except Exception:
                    dt = datetime.now()

        if dt.tzinfo is None:
            # نفس توقيت السيرفر. المهم أننا نحفظه ونستخدمه نسبيًا.
            return dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    def _to_ms(self, dt):
        return int(dt.timestamp() * 1000)

    def _trade_id(self, symbol, entry_meta):
        raw = (
            f"{symbol}|"
            f"{entry_meta.get('side')}|"
            f"{entry_meta.get('entry_time')}|"
            f"{entry_meta.get('entry_price')}|"
            f"{entry_meta.get('amount')}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _trade_dir(self, symbol, trade_id):
        d = os.path.join(self.base_dir, f"{datetime.now().strftime('%Y%m%d')}_{self._safe_symbol(symbol)}_{trade_id}")
        os.makedirs(d, exist_ok=True)
        return d

    def _fetch_ohlcv_range(self, client, symbol, timeframe, start_dt, end_dt):
        since = self._to_ms(start_dt)
        end_ms = self._to_ms(end_dt)
        all_rows = []

        while since < end_ms:
            rows = client.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not rows:
                break

            all_rows.extend(rows)
            last_ts = rows[-1][0]

            if last_ts <= since:
                break

            since = last_ts + 1

            if last_ts >= end_ms:
                break

            time.sleep(0.15)

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset=["timestamp"]).copy()
        df["utc_time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df[(df["timestamp"] >= self._to_ms(start_dt)) & (df["timestamp"] <= end_ms)].copy()
        return df

    def _fetch_trades_range(self, client, symbol, start_dt, end_dt):
        since = self._to_ms(start_dt)
        end_ms = self._to_ms(end_dt)
        all_rows = []

        while since < end_ms:
            trades = client.exchange.fetch_trades(symbol, since=since, limit=1000)
            if not trades:
                break

            all_rows.extend(trades)
            last_ts = trades[-1].get("timestamp")

            if not last_ts or last_ts <= since:
                break

            since = last_ts + 1

            if last_ts >= end_ms:
                break

            time.sleep(0.15)

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        if "timestamp" in df.columns:
            df["utc_time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df[(df["timestamp"] >= self._to_ms(start_dt)) & (df["timestamp"] <= end_ms)].copy()

        keep_cols = [c for c in ["utc_time", "timestamp", "price", "amount", "side"] if c in df.columns]
        return df[keep_cols].copy() if keep_cols else df

    def _annotate_candles(self, df, entry_price, sl, tp, side):
        if df.empty:
            return df

        entry_price = float(entry_price or 0)
        sl = float(sl or 0)
        tp = float(tp or 0)
        side = str(side or "").lower()

        df["entry_price"] = entry_price
        df["sl"] = sl
        df["tp"] = tp

        if side in ["sell", "short"]:
            df["sl_hit"] = df["high"] >= sl if sl > 0 else False
            df["tp_hit"] = df["low"] <= tp if tp > 0 else False
            df["adverse_pct"] = ((df["high"] - entry_price) / entry_price) * 100 if entry_price > 0 else 0
            df["favorable_pct"] = ((entry_price - df["low"]) / entry_price) * 100 if entry_price > 0 else 0
        else:
            df["sl_hit"] = df["low"] <= sl if sl > 0 else False
            df["tp_hit"] = df["high"] >= tp if tp > 0 else False
            df["adverse_pct"] = ((entry_price - df["low"]) / entry_price) * 100 if entry_price > 0 else 0
            df["favorable_pct"] = ((df["high"] - entry_price) / entry_price) * 100 if entry_price > 0 else 0

        return df

    def record_entry(self, client, symbol, entry_meta, decision_context=None):
        if not self.enabled:
            return None

        try:
            trade_id = entry_meta.get("trade_id") or self._trade_id(symbol, entry_meta)
            trade_dir = self._trade_dir(symbol, trade_id)

            entry_meta["trade_id"] = trade_id
            entry_meta["journal_dir"] = trade_dir

            payload = {
                "trade_id": trade_id,
                "symbol": symbol,
                "status": "OPEN",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_meta": entry_meta,
                "decision_context": decision_context or {},
            }

            with open(os.path.join(trade_dir, "trade_entry.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            # Snapshot قبل الدخول مباشرة للرجوع السريع
            entry_dt = self._parse_time(entry_meta.get("entry_time"))
            start_dt = entry_dt - timedelta(minutes=self.pre_minutes)
            end_dt = entry_dt + timedelta(minutes=5)

            for tf in self.timeframes:
                try:
                    df = self._fetch_ohlcv_range(client, symbol, tf, start_dt, end_dt)
                    df = self._annotate_candles(
                        df,
                        entry_meta.get("entry_price"),
                        entry_meta.get("sl"),
                        entry_meta.get("tp"),
                        entry_meta.get("side"),
                    )
                    df.to_csv(os.path.join(trade_dir, f"entry_snapshot_{tf}.csv"), index=False)
                except Exception as e:
                    logger.warning(f"[TradeJournal] فشل حفظ entry snapshot {symbol} {tf}: {e}")

            logger.info(f"[TradeJournal] تم إنشاء سجل دخول الصفقة: {trade_dir}")
            return trade_dir

        except Exception as e:
            logger.error(f"[TradeJournal] فشل تسجيل دخول الصفقة {symbol}: {e}")
            return None

    def finalize_trade(self, client, symbol, entry_meta, exit_details):
        if not self.enabled:
            return None

        try:
            trade_id = entry_meta.get("trade_id") or self._trade_id(symbol, entry_meta)
            trade_dir = entry_meta.get("journal_dir") or self._trade_dir(symbol, trade_id)
            os.makedirs(trade_dir, exist_ok=True)

            entry_dt = self._parse_time(entry_meta.get("entry_time"))
            exit_dt = self._parse_time(exit_details.get("exit_time"))

            start_dt = entry_dt - timedelta(minutes=self.pre_minutes)
            end_dt = exit_dt + timedelta(minutes=self.post_minutes)

            summary = {
                "trade_id": trade_id,
                "symbol": symbol,
                "status": "CLOSED",
                "finalized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_meta": entry_meta,
                "exit_details": exit_details,
                "quick_read": self._quick_read(entry_meta, exit_details),
            }

            with open(os.path.join(trade_dir, "trade_summary.json"), "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)

            for tf in self.timeframes:
                try:
                    df = self._fetch_ohlcv_range(client, symbol, tf, start_dt, end_dt)
                    df = self._annotate_candles(
                        df,
                        entry_meta.get("entry_price"),
                        entry_meta.get("sl"),
                        entry_meta.get("tp"),
                        entry_meta.get("side"),
                    )
                    df.to_csv(os.path.join(trade_dir, f"trade_window_{tf}.csv"), index=False)
                except Exception as e:
                    logger.warning(f"[TradeJournal] فشل حفظ trade window {symbol} {tf}: {e}")

            if self.fetch_trades_enabled:
                try:
                    trades_df = self._fetch_trades_range(client, symbol, start_dt, end_dt)
                    trades_df.to_csv(os.path.join(trade_dir, "recent_trades.csv"), index=False)
                except Exception as e:
                    logger.warning(f"[TradeJournal] فشل حفظ recent trades {symbol}: {e}")

            logger.info(f"[TradeJournal] تم إغلاق سجل الصفقة: {trade_dir}")
            return trade_dir

        except Exception as e:
            logger.error(f"[TradeJournal] فشل إنهاء سجل الصفقة {symbol}: {e}")
            return None

    def _quick_read(self, entry_meta, exit_details):
        try:
            side = str(entry_meta.get("side", "")).upper()
            entry = float(entry_meta.get("entry_price", 0) or 0)
            sl = float(entry_meta.get("sl", 0) or 0)
            tp = float(entry_meta.get("tp", 0) or 0)
            exit_price = float(exit_details.get("exit_price", 0) or 0)

            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0

            return {
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "exit_price": exit_price,
                "rr_planned": round(rr, 3),
                "filter_profile": entry_meta.get("filter_profile"),
                "entry_reason": entry_meta.get("entry_reason"),
                "exit_reason": exit_details.get("exit_reason"),
                "pnl": exit_details.get("pnl"),
                "pnl_pct": exit_details.get("pnl_pct"),
            }
        except Exception:
            return {}

    def build_post_trade_feedback(self, entry_meta: dict, exit_details: dict) -> dict:
        """
        يبني تقرير تغذية راجعة بعد إغلاق الصفقة.
        يقيّم جودة نقطة الدخول بناءً على أقصى حركة لصالحنا وأقصى انعكاس ضدنا.
        """
        try:
            side        = str(entry_meta.get("side", "buy")).lower()
            entry_price = float(entry_meta.get("entry_price", 0) or 0)
            tp          = float(entry_meta.get("tp", 0) or 0)
            exit_price  = float(exit_details.get("exit_price", 0) or 0)
            exit_reason = str(exit_details.get("exit_reason", ""))
            pnl_pct     = float(exit_details.get("pnl_pct", 0) or 0)
            symbol      = entry_meta.get("symbol", "")
            leverage_raw = entry_meta.get("leverage")
            leverage = float(leverage_raw) if leverage_raw not in (None, "") else None

            # Runtime excursion tracking is stored in R units.  The old
            # renderer looked only for percentage fields that are normally
            # absent, silently replaced them with zero, then called a losing
            # trade a "sharp reversal (0.00%)".  Prefer explicit percentages
            # when supplied; otherwise convert MFE/MAE R using the original
            # (not trailed) stop distance.
            explicit_fav = exit_details.get("max_favorable_pct")
            explicit_adv = exit_details.get("max_adverse_pct")
            initial_sl = float(entry_meta.get("initial_sl") or entry_meta.get("sl") or 0)
            risk_pct_per_r = (
                abs(entry_price - initial_sl) / entry_price * 100.0
                if entry_price > 0 and initial_sl > 0
                else None
            )
            r_fields_present = any(
                key in exit_details or key in entry_meta
                for key in ("mfe_r", "mae_r")
            )
            if explicit_fav is not None or explicit_adv is not None:
                max_fav_pct = max(0.0, float(explicit_fav or 0.0))
                max_adv_pct = max(0.0, float(explicit_adv or 0.0))
                excursion_available = True
            elif risk_pct_per_r is not None and r_fields_present:
                mfe_r = float(exit_details.get("mfe_r", entry_meta.get("mfe_r", 0.0)) or 0.0)
                mae_r = float(exit_details.get("mae_r", entry_meta.get("mae_r", 0.0)) or 0.0)
                max_fav_pct = max(0.0, mfe_r) * risk_pct_per_r
                max_adv_pct = max(0.0, mae_r) * risk_pct_per_r
                excursion_available = all(
                    math.isfinite(value)
                    for value in (max_fav_pct, max_adv_pct)
                )
            else:
                max_fav_pct = None
                max_adv_pct = None
                excursion_available = False

            # تصنيف جودة الدخول
            is_win = exit_reason in ("TAKE_PROFIT", "TP", "WIN") or pnl_pct > 0

            if not excursion_available:
                quality        = "INSUFFICIENT_DATA"
                reversal_label = "بيانات الحركة أثناء الصفقة غير متاحة؛ لا يمكن تقييم نقطة الدخول"
            elif max_adv_pct <= 0.3 and is_win:
                quality        = "PERFECT"
                reversal_label = "زيرو انعكاس تقريباً 🎯"
            elif max_adv_pct <= 1.0 and is_win:
                quality        = "GOOD"
                reversal_label = f"انعكاس بسيط ({max_adv_pct:.2f}%) قبل الصعود ✅"
            elif is_win and max_fav_pct > 0:
                quality        = "EARLY_EXIT" if (tp > 0 and exit_price < tp * 0.95 and side == "buy") else "GOOD"
                reversal_label = f"دخول جيد لكن فيه تذبذب ({max_adv_pct:.2f}%)"
            else:
                quality        = "BAD_ENTRY"
                reversal_label = f"انعكاس حاد ضدنا ({max_adv_pct:.2f}%) قبل الضرب ❌"

            direction  = "شراء 📈" if side == "buy" else "بيع 📉"
            outcome    = "✅ ربح" if is_win else "❌ خسارة"
            side_sign = 1.0 if side in ("buy", "long") else -1.0
            price_move_pct = (
                side_sign * (exit_price - entry_price) / entry_price * 100.0
                if entry_price > 0 and exit_price > 0
                else None
            )
            pnl_line = f"عائد الهامش الصافي: {pnl_pct:+.2f}%"
            if price_move_pct is not None and math.isfinite(price_move_pct):
                pnl_line += f" | حركة السعر: {price_move_pct:+.2f}%"
            if leverage is not None and math.isfinite(leverage):
                pnl_line += f" | الرافعة: {leverage:.0f}x"

            lines = [
                f"📋 <b>تغذية راجعة — {symbol}</b>",
                f"الاتجاه: {direction} | النتيجة: {outcome}",
                f"دخول: <code>{entry_price}</code> | خروج: <code>{exit_price}</code>",
                pnl_line,
                f"",
                f"📊 <b>جودة نقطة الدخول:</b>",
                (
                    f"أقصى حركة لصالحنا: <b>{max_fav_pct:+.2f}%</b>"
                    if excursion_available else
                    "أقصى حركة لصالحنا: <b>غير متاحة</b>"
                ),
                (
                    f"أقصى انعكاس ضدنا:   <b>{max_adv_pct:+.2f}%</b>"
                    if excursion_available else
                    "أقصى انعكاس ضدنا:   <b>غير متاح</b>"
                ),
                f"التقييم: {reversal_label}",
            ]

            if quality == "PERFECT":
                lines.append(f"\n🌟 <b>ماشاء الله! دخول موفق جداً — السعر انطلق مباشرةً بلا انعكاس يُذكر.</b>")
            elif quality == "EARLY_EXIT":
                lines.append(f"\n⚠️ <b>ملاحظة:</b> السعر واصل {max_fav_pct:.2f}% لصالحنا — هل خرجنا مبكراً قبل الهدف الكامل?")
            elif quality == "BAD_ENTRY":
                lines.append(f"\n🔴 <b>نقطة الدخول كانت ضعيفة</b> — الانعكاس {max_adv_pct:.2f}% يشير لدخول قبل أوانه.")

            return {
                "quality":           quality,
                "max_favorable_pct": max_fav_pct,
                "max_adverse_pct":   max_adv_pct,
                "excursion_available": excursion_available,
                "reversal_label":    reversal_label,
                "feedback_text":     "\n".join(lines),
                "is_win":            is_win,
            }

        except Exception as e:
            logger.error(f"[TradeJournal] فشل بناء التغذية الراجعة: {e}")
            return {"quality": "UNKNOWN", "feedback_text": "⚠️ فشل تحليل الصفقة"}
