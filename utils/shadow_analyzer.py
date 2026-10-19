"""
utils/shadow_analyzer.py
─────────────────────────────────────────────────────────────
محلل أداء البوت — يقرأ trade_journal.csv ويستخرج إحصائيات
قابل للإرسال مباشرة عبر تيليجرام
─────────────────────────────────────────────────────────────
"""
import os
from datetime import datetime
from core.logger import logger

try:
    import pandas as pd
    import numpy as np
    _PANDAS_AVAILABLE = True
except ImportError:
    _PANDAS_AVAILABLE = False
    logger.warning("[ShadowAnalyzer] pandas غير مثبت — pip install pandas")


class ShadowAnalyzer:
    """
    يقرأ ملف CSV لتاريخ الصفقات ويحسب:
    - Win Rate الإجمالي ولكل عملة
    - أداء حسب يوم الأسبوع والساعة
    - مؤشر الخروج المبكر (Early Exit)
    - تقرير نصي جاهز لتيليجرام
    """

    # الأعمدة التي يتوقعها المحلل — بناءً على قاعدة بيانات SQLite
    COL_TIMESTAMP  = "exit_time"
    COL_SYMBOL     = "symbol"
    COL_SIDE       = "side"
    COL_ENTRY      = "entry_price"
    COL_EXIT       = "exit_price"
    COL_PNL_PCT    = "pnl_pct"
    COL_PNL_USDT   = "pnl"
    COL_OUTCOME    = "outcome"          # WIN / LOSS / BE
    COL_TP_TARGET  = "tp_target"        # اختياري — السعر المستهدف
    COL_MAX_AFTER  = "max_price_after_entry"  # اختياري — أعلى سعر بعد الدخول

    def __init__(self, csv_path: str = "testnet_trades_log.csv"):
        self.csv_path = csv_path
        self._df: "pd.DataFrame | None" = None

    # ─────────────────────────────────────────────────────────────
    # تحميل البيانات
    # ─────────────────────────────────────────────────────────────

    def load(self) -> bool:
        """يقرأ البيانات من قاعدة بيانات SQLite. يُرجع True إذا نجح."""
        if not _PANDAS_AVAILABLE:
            logger.error("[ShadowAnalyzer] pandas مطلوب")
            return False

        try:
            from utils.db_manager import load_trades_df
            df = load_trades_df()
            if df.empty:
                logger.warning("[ShadowAnalyzer] لا توجد صفقات في قاعدة البيانات")
                return False

            # تحديد النتيجة (Outcome) بناءً على pnl_pct أو pnl كبديل آمن
            if "pnl_pct" in df.columns:
                df["outcome"] = np.where(df["pnl_pct"] > 0, "WIN", "LOSS")
            elif "pnl" in df.columns:
                # fallback: استخدم USDT pnl إذا غاب pnl_pct
                df["outcome"] = np.where(df["pnl"] > 0, "WIN", "LOSS")
            else:
                logger.warning("[ShadowAnalyzer] لا يوجد pnl_pct أو pnl — سيكون win_rate = 0% (تقرير غير دقيق)")
                df["outcome"] = "LOSS"

            # تحويل الأنواع
            df[self.COL_TIMESTAMP] = pd.to_datetime(df[self.COL_TIMESTAMP], errors="coerce")
            df[self.COL_PNL_PCT]   = pd.to_numeric(df[self.COL_PNL_PCT],   errors="coerce").fillna(0)

            if self.COL_PNL_USDT in df.columns:
                df[self.COL_PNL_USDT] = pd.to_numeric(df[self.COL_PNL_USDT], errors="coerce").fillna(0)

            df["_day_of_week"] = df[self.COL_TIMESTAMP].dt.day_name()
            df["_hour"]        = df[self.COL_TIMESTAMP].dt.hour
            df["_is_win"]      = df[self.COL_OUTCOME].str.upper().str.strip() == "WIN"

            self._df = df
            logger.info(f"[ShadowAnalyzer] تم تحميل {len(df)} صفقة من قاعدة البيانات")
            return True

        except Exception as e:
            logger.error(f"[ShadowAnalyzer] فشل قراءة قاعدة البيانات: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # الحسابات الرئيسية
    # ─────────────────────────────────────────────────────────────

    def overall_win_rate(self) -> dict:
        """Win Rate الإجمالي"""
        df = self._df
        total   = len(df)
        wins    = int(df["_is_win"].sum())
        losses  = total - wins
        win_rate = (wins / total * 100) if total > 0 else 0.0
        avg_pnl  = float(df[self.COL_PNL_PCT].mean())
        total_pnl_usdt = float(df[self.COL_PNL_USDT].sum()) if self.COL_PNL_USDT in df.columns else None

        return {
            "total":          total,
            "wins":           wins,
            "losses":         losses,
            "win_rate_pct":   round(win_rate, 1),
            "avg_pnl_pct":    round(avg_pnl,  2),
            "total_pnl_usdt": round(total_pnl_usdt, 2) if total_pnl_usdt is not None else None,
        }

    def win_rate_per_symbol(self, top_n: int = 5) -> list[dict]:
        """
        Win Rate لكل عملة — يُرجع أفضل وأسوأ N عملات.
        """
        df = self._df
        grouped = df.groupby(self.COL_SYMBOL).agg(
            total   = ("_is_win", "count"),
            wins    = ("_is_win", "sum"),
            avg_pnl = (self.COL_PNL_PCT, "mean"),
        ).reset_index()
        grouped["win_rate"] = (grouped["wins"] / grouped["total"] * 100).round(1)
        grouped = grouped.sort_values("win_rate", ascending=False)

        result = []
        for _, row in grouped.iterrows():
            result.append({
                "symbol":   row[self.COL_SYMBOL],
                "total":    int(row["total"]),
                "wins":     int(row["wins"]),
                "win_rate": float(row["win_rate"]),
                "avg_pnl":  round(float(row["avg_pnl"]), 2),
            })
        return result

    def win_rate_by_day(self) -> list[dict]:
        """Win Rate حسب يوم الأسبوع — يكشف إذا البوت يخسر يوم معين"""
        df = self._df
        order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        grouped = df.groupby("_day_of_week").agg(
            total = ("_is_win", "count"),
            wins  = ("_is_win", "sum"),
        ).reindex(order).dropna()
        grouped["win_rate"] = (grouped["wins"] / grouped["total"] * 100).round(1)

        result = []
        for day, row in grouped.iterrows():
            result.append({
                "day":      day,
                "total":    int(row["total"]),
                "win_rate": float(row["win_rate"]),
            })
        return result

    def win_rate_by_hour(self) -> list[dict]:
        """Win Rate حسب ساعة الدخول — يكشف أفضل وأسوأ أوقات التداول"""
        df = self._df
        grouped = df.groupby("_hour").agg(
            total = ("_is_win", "count"),
            wins  = ("_is_win", "sum"),
        ).reset_index()
        grouped["win_rate"] = (grouped["wins"] / grouped["total"] * 100).round(1)
        grouped = grouped.sort_values("_hour")

        result = []
        for _, row in grouped.iterrows():
            result.append({
                "hour":     int(row["_hour"]),
                "total":    int(row["total"]),
                "win_rate": float(row["win_rate"]),
            })
        return result

    def early_exit_analysis(self) -> dict:
        """
        مؤشر الخروج المبكر (Early Exit):
        يقارن exit_price مع max_price_after_entry لصفقات BUY الرابحة.
        إذا كان السعر واصل للـ TP لكن البوت خرج مبكراً → خسارة فرصة.

        يتطلب: COL_TP_TARGET و COL_MAX_AFTER في الـ CSV.
        """
        df = self._df
        if self.COL_TP_TARGET not in df.columns or self.COL_MAX_AFTER not in df.columns:
            return {"available": False, "reason": "أعمدة tp_target أو max_price_after_entry غير موجودة في CSV"}

        buy_wins = df[
            (df["_is_win"]) &
            (df[self.COL_SIDE].str.upper() == "BUY")
        ].copy()

        if buy_wins.empty:
            return {"available": True, "early_exit_count": 0, "missed_pnl_avg": 0}

        buy_wins["tp_target"]   = pd.to_numeric(buy_wins[self.COL_TP_TARGET],  errors="coerce")
        buy_wins["max_after"]   = pd.to_numeric(buy_wins[self.COL_MAX_AFTER],  errors="coerce")
        buy_wins["exit_p"]      = pd.to_numeric(buy_wins[self.COL_EXIT],        errors="coerce")
        buy_wins["entry_p"]     = pd.to_numeric(buy_wins[self.COL_ENTRY],       errors="coerce")

        # خروج مبكر: السعر وصل للهدف لكن البوت خرج قبله
        early_mask = (
            buy_wins["max_after"] >= buy_wins["tp_target"]
        ) & (
            buy_wins["exit_p"] < buy_wins["tp_target"]
        )

        early_df = buy_wins[early_mask]
        count    = len(early_df)

        # تقدير الربح الضائع
        if count > 0 and (early_df["entry_p"] > 0).all():
            missed = ((early_df["tp_target"] - early_df["exit_p"]) / early_df["entry_p"] * 100).mean()
        else:
            missed = 0.0

        return {
            "available":        True,
            "total_buy_wins":   len(buy_wins),
            "early_exit_count": count,
            "early_exit_pct":   round(count / len(buy_wins) * 100, 1),  # buy_wins تحققنا أنها ليست فاضية أعلاه
            "missed_pnl_avg":   round(missed, 2),
        }

    # ─────────────────────────────────────────────────────────────
    # بناء تقرير تيليجرام
    # ─────────────────────────────────────────────────────────────

    def build_telegram_report(self, top_symbols: int = 5) -> str:
        """
        يبني تقريراً نصياً كاملاً جاهزاً للإرسال لتيليجرام.
        """
        if not self.load():
            return "⚠️ [Shadow Analyzer] فشل تحميل بيانات التداول."

        lines = []
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines.append(f"📊 <b>تقرير أداء البوت — {now}</b>")
        lines.append("─" * 30)

        # ── 1. الإجمالي ───────────────────────────────────────────
        ov = self.overall_win_rate()
        lines.append(f"📈 <b>إجمالي الصفقات:</b> {ov['total']}")
        lines.append(f"✅ فوز: {ov['wins']} | ❌ خسارة: {ov['losses']}")
        lines.append(f"🎯 <b>Win Rate:</b> {ov['win_rate_pct']}%")
        lines.append(f"💹 متوسط P&L: {ov['avg_pnl_pct']:+.2f}%")
        if ov["total_pnl_usdt"] is not None:
            emoji = "🟢" if ov["total_pnl_usdt"] >= 0 else "🔴"
            lines.append(f"{emoji} إجمالي الربح: {ov['total_pnl_usdt']:+.2f} USDT")

        # ── 2. أفضل وأسوأ عملات ──────────────────────────────────
        lines.append("\n🏆 <b>أفضل العملات:</b>")
        symbols = self.win_rate_per_symbol(top_n=top_symbols)
        for s in symbols[:3]:
            lines.append(
                f"  {s['symbol']}: {s['win_rate']}% ({s['wins']}/{s['total']}) "
                f"| متوسط: {s['avg_pnl']:+.2f}%"
            )

        lines.append("\n💀 <b>أسوأ العملات:</b>")
        for s in symbols[-3:]:
            lines.append(
                f"  {s['symbol']}: {s['win_rate']}% ({s['wins']}/{s['total']}) "
                f"| متوسط: {s['avg_pnl']:+.2f}%"
            )

        # ── 3. أداء أيام الأسبوع ─────────────────────────────────
        days = self.win_rate_by_day()
        if days:
            lines.append("\n📅 <b>الأداء حسب اليوم:</b>")
            for d in days:
                bar = "🟢" if d["win_rate"] >= 55 else ("🔴" if d["win_rate"] < 40 else "🟡")
                lines.append(
                    f"  {bar} {d['day'][:3]}: {d['win_rate']}% ({d['total']} صفقة)"
                )

        # ── 4. أفضل وأسوأ ساعات ──────────────────────────────────
        hours = self.win_rate_by_hour()
        if hours:
            best_hour  = max(hours, key=lambda x: x["win_rate"])
            worst_hour = min(hours, key=lambda x: x["win_rate"])
            lines.append(f"\n⏰ <b>أفضل ساعة للتداول:</b> {best_hour['hour']:02d}:00 ({best_hour['win_rate']}%)")
            lines.append(f"⚠️ <b>أسوأ ساعة للتداول:</b> {worst_hour['hour']:02d}:00 ({worst_hour['win_rate']}%)")

        # ── 5. الخروج المبكر ─────────────────────────────────────
        early = self.early_exit_analysis()
        if early.get("available"):
            lines.append(f"\n🚪 <b>تحليل الخروج المبكر:</b>")
            lines.append(f"  حالات خروج مبكر: {early['early_exit_count']} ({early.get('early_exit_pct', 0)}%)")
            lines.append(f"  ربح ضائع متوسط: {early['missed_pnl_avg']:+.2f}%")
        else:
            lines.append(f"\nℹ️ تحليل الخروج المبكر: {early.get('reason', 'غير متاح')}")

        lines.append("\n⚠️ <i>تقرير تلقائي — للاطلاع فقط</i>")
        return "\n".join(lines)
