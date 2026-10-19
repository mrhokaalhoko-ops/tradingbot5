"""
استراتيجية SMC + RSI — مفصولة بالكامل عن main.py
نمط Strategy Context مستوحى من strategy.ts
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd

@dataclass
class StrategyContext:
    """
    حاوية البيانات التي يجهزها المحرك ويمررها للاستراتيجية.
    مستوحى من TypedStrategyContext في strategy.ts
    """
    symbol: str
    price: float
    bars_df: pd.DataFrame
    ta_data: dict
    coin_data: dict
    ticker: dict
    last_signal: Optional[str] = None  # 'buy' | 'sell' | None
    client: Optional[object] = None

    def is_flat(self) -> bool:
        return self.last_signal is None

    def get_latest(self, col: str):
        """يجلب آخر قيمة لعمود من الشموع"""
        try:
            return self.bars_df[col].dropna().iloc[-1]
        except Exception:
            return None

    def get_slice(self, col: str, n: int = 5):
        """يجلب آخر N قيمة لعمود (للتحليل الزمني مثل getCrossedSince)"""
        try:
            return self.bars_df[col].dropna().iloc[-n:].tolist()
        except Exception:
            return []


class SMCRSIStrategy:
    """
    الاستراتيجية الرئيسية: SMC + RSI
    تستقبل Context جاهزاً وتُرجع قرار buy/sell/hold
    """
    
    def get_description(self) -> str:
        return "SMC Liquidity Sweep + RSI Divergence Strategy"

    def execute(self, context: StrategyContext) -> dict:
        """
        يعتمد على المنطق الاحترافي المعالج في TAEngine الذي يدمج (OB, FVG, Liquidity, HTF).
        """
        ta_data = context.ta_data
        
        signal = ta_data.get("engine_trend", "neutral")
        reason = ta_data.get("engine_reason", "لا توجد إشارة SMC")
        
        # تحويل neutral إلى hold ليتوافق مع المعمارية
        if signal == "neutral":
            signal = "hold"
            
        return {
            "signal": signal,
            "reason": reason,
            "ta_data": ta_data
        }
