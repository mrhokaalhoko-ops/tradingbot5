"""Small contracts shared by historical replay and future live shadow mode."""

from __future__ import annotations

from typing import Protocol, Sequence

from .models import MarketSnapshot, TradePlan


class TradingStrategy(Protocol):
    strategy_id: str

    def evaluate(self, snapshot: MarketSnapshot) -> Sequence[TradePlan]:
        """Return complete immutable plans using only data in ``snapshot``."""
        ...
