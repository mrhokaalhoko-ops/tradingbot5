"""Clean, execution-free trading research kernel.

Nothing in this package is allowed to call an exchange order API. Strategies
produce immutable plans; the causal replay engine measures those plans.
"""

from .models import (
    Bar,
    EntryKind,
    MarketSnapshot,
    ReplayResult,
    ReplayStatus,
    Side,
    TradePlan,
)
from .replay import CausalReplayEngine, ReplayCosts
from .strategies import BreakoutContinuationStrategy, PullbackReclaimStrategy

__all__ = [
    "Bar",
    "CausalReplayEngine",
    "BreakoutContinuationStrategy",
    "EntryKind",
    "MarketSnapshot",
    "PullbackReclaimStrategy",
    "ReplayCosts",
    "ReplayResult",
    "ReplayStatus",
    "Side",
    "TradePlan",
]
