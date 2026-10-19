"""Causal, order-free replay for immutable V2 trade plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Iterable, Mapping

from .models import (
    Bar,
    EntryKind,
    MarketSnapshot,
    ReplayResult,
    ReplayStatus,
    Side,
    TradePlan,
)


@dataclass(frozen=True)
class ReplayCosts:
    entry_fee_bps: float = 5.0
    exit_fee_bps: float = 5.0
    entry_slippage_bps: float = 3.0
    stop_slippage_bps: float = 8.0
    target_slippage_bps: float = 2.0
    market_exit_slippage_bps: float | None = None

    def __post_init__(self) -> None:
        if self.market_exit_slippage_bps is None:
            object.__setattr__(
                self,
                "market_exit_slippage_bps",
                float(self.entry_slippage_bps),
            )
        if any(not math.isfinite(float(value)) or float(value) < 0
               for value in self.__dict__.values()):
            raise ValueError("replay costs must be finite and non-negative")


class CausalReplayEngine:
    """Build closed-data snapshots and replay plans without intrabar guesses."""

    def __init__(self, costs: ReplayCosts | None = None):
        self.costs = costs or ReplayCosts()

    @staticmethod
    def snapshot(
        *,
        symbol: str,
        as_of: datetime,
        bars_by_timeframe: Mapping[str, Iterable[Bar]],
        regime: str = "UNKNOWN",
        history_limit: int | None = None,
    ) -> MarketSnapshot:
        visible: dict[str, tuple[Bar, ...]] = {}
        for timeframe, bars in bars_by_timeframe.items():
            closed = tuple(sorted(
                (bar for bar in bars if bar.close_time <= as_of),
                key=lambda bar: bar.close_time,
            ))
            if history_limit is not None:
                closed = closed[-max(0, int(history_limit)):]
            visible[str(timeframe)] = closed
        return MarketSnapshot(
            symbol=symbol,
            as_of=as_of,
            bars=visible,
            regime=regime,
        )

    @staticmethod
    def _entry_price(plan: TradePlan, bar: Bar, costs: ReplayCosts) -> float:
        if plan.entry_kind is EntryKind.LIMIT:
            return float(plan.entry)
        slip = float(costs.entry_slippage_bps) / 10_000.0
        return float(bar.open) * (1.0 + slip if plan.side is Side.BUY else 1.0 - slip)

    @staticmethod
    def _limit_touched(plan: TradePlan, bar: Bar) -> bool:
        return (
            float(bar.low) <= float(plan.entry)
            if plan.side is Side.BUY
            else float(bar.high) >= float(plan.entry)
        )

    @staticmethod
    def _terminal_hits(plan: TradePlan, bar: Bar) -> tuple[bool, bool]:
        if plan.side is Side.BUY:
            return float(bar.low) <= float(plan.stop), float(bar.high) >= float(plan.target)
        return float(bar.high) >= float(plan.stop), float(bar.low) <= float(plan.target)

    @staticmethod
    def _excursions(plan: TradePlan, entry: float, bar: Bar) -> tuple[float, float]:
        risk = abs(entry - float(plan.stop))
        if risk <= 0:
            return 0.0, 0.0
        if plan.side is Side.BUY:
            return max(0.0, (float(bar.high) - entry) / risk), max(
                0.0, (entry - float(bar.low)) / risk
            )
        return max(0.0, (entry - float(bar.low)) / risk), max(
            0.0, (float(bar.high) - entry) / risk
        )

    def replay_plan(
        self,
        plan: TradePlan,
        execution_bars: Iterable[Bar],
        *,
        horizon_end: datetime | None = None,
    ) -> ReplayResult:
        if horizon_end is not None and horizon_end.tzinfo is None:
            raise ValueError("horizon_end must be timezone-aware")
        holding_end = min(plan.exit_by, horizon_end) if horizon_end is not None else plan.exit_by

        def incomplete(reason, *, filled_at=None, entry_price=None, observed=0):
            return ReplayResult(
                plan_id=plan.plan_id, status=ReplayStatus.INCOMPLETE_DATA,
                filled_at=filled_at, entry_price=entry_price,
                bars_observed=observed, reason=reason,
            )

        bars = tuple(sorted(
            (
                bar for bar in execution_bars
                if bar.symbol == plan.symbol and plan.created_at <= bar.open_time < holding_end
            ),
            key=lambda bar: bar.open_time,
        ))
        if not bars:
            return ReplayResult(
                plan_id=plan.plan_id,
                status=ReplayStatus.NO_EXECUTION_BARS,
                reason="No lower-timeframe bars after plan creation",
            )

        effective_end = (
            min(plan.valid_until, horizon_end)
            if horizon_end is not None
            else plan.valid_until
        )
        entry_bar_index: int | None = None
        entry_price: float | None = None
        entry_observed = 0
        # Binance stores the last millisecond of a candle; synthetic/other
        # adapters may store the exclusive end. Both represent the same slot.
        def slot_end(bar):
            unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(bar.timeframe[-1:])
            count = bar.timeframe[:-1]
            if unit is None or not count.isdigit() or int(count) <= 0:
                return None
            end = bar.open_time + timedelta(seconds=int(count) * unit)
            if bar.close_time not in (end, end - timedelta(milliseconds=1)):
                return None
            return end

        def continuous(index):
            bar = bars[index]
            return (
                bar.timeframe == bars[0].timeframe
                and slot_end(bar) is not None
                and (index == 0 or slot_end(bars[index - 1]) == bar.open_time)
                and (index + 1 == len(bars) or bars[index + 1].open_time >= slot_end(bar))
            )

        for index, bar in enumerate(bars):
            if bar.open_time >= effective_end:
                break
            if not continuous(index):
                return incomplete("Missing, duplicate, overlapping or mixed execution bars", observed=entry_observed)
            entry_observed += 1
            if plan.entry_kind is EntryKind.MARKET_NEXT_OPEN:
                if bar.open_time >= plan.created_at:
                    entry_bar_index = index
                    entry_price = self._entry_price(plan, bar, self.costs)
                    break
                continue

            # The OHLC of a bar crossing expiry cannot locate a limit touch
            # before expiry. Do not classify it using the later price path.
            if bar.close_time > effective_end:
                return incomplete("Entry deadline falls inside an execution bar", observed=entry_observed - 1)
            stop_hit, target_hit = self._terminal_hits(plan, bar)
            entry_touched = self._limit_touched(plan, bar)
            if (
                (plan.side is Side.BUY and float(bar.open) <= float(plan.stop))
                or (plan.side is Side.SELL and float(bar.open) >= float(plan.stop))
            ):
                return ReplayResult(
                    plan_id=plan.plan_id,
                    status=ReplayStatus.INVALIDATED_BEFORE_ENTRY,
                    exited_at=bar.open_time,
                    bars_observed=index + 1,
                    reason="Market gapped through structural invalidation before entry",
                )
            if entry_touched:
                if stop_hit or target_hit:
                    return ReplayResult(
                        plan_id=plan.plan_id,
                        status=ReplayStatus.AMBIGUOUS_ENTRY_BAR,
                        exited_at=bar.close_time,
                        bars_observed=index + 1,
                        reason=(
                            "Limit entry and a terminal level touched in the same bar; "
                            "tick order is unavailable"
                        ),
                    )
                entry_bar_index = index
                entry_price = float(plan.entry)
                break
            if target_hit:
                return ReplayResult(
                    plan_id=plan.plan_id,
                    status=ReplayStatus.TARGET_BEFORE_ENTRY,
                    exited_at=bar.close_time,
                    bars_observed=index + 1,
                    reason="Target traded before the limit entry was reached",
                )
        if entry_bar_index is None or entry_price is None:
            if not entry_observed or slot_end(bars[entry_observed - 1]) < effective_end:
                return incomplete("Execution data ends before the entry deadline", observed=entry_observed)
            return ReplayResult(
                plan_id=plan.plan_id,
                status=ReplayStatus.EXPIRED_UNFILLED,
                exited_at=effective_end,
                bars_observed=entry_observed,
                reason="Entry was not reached before plan expiry",
            )

        risk = abs(float(entry_price) - float(plan.stop))
        if risk <= 0 or (
            plan.side is Side.BUY
            and not (float(plan.stop) < entry_price < float(plan.target))
        ) or (
            plan.side is Side.SELL
            and not (float(plan.target) < entry_price < float(plan.stop))
        ):
            return ReplayResult(
                plan_id=plan.plan_id,
                status=ReplayStatus.INVALID_PLAN,
                filled_at=bars[entry_bar_index].open_time,
                entry_price=entry_price,
                reason="Actual fill made the stop/target geometry invalid",
            )

        mfe_r = 0.0
        mae_r = 0.0
        entry_fee = entry_price * float(self.costs.entry_fee_bps) / 10_000.0
        filled_at = bars[entry_bar_index].open_time
        observed = 0
        for index in range(entry_bar_index, len(bars)):
            bar = bars[index]
            if not continuous(index):
                return incomplete("Missing, duplicate, overlapping or mixed execution bars",
                                  filled_at=filled_at, entry_price=entry_price, observed=observed)
            stop_at_open = (float(bar.open) <= float(plan.stop) if plan.side is Side.BUY
                            else float(bar.open) >= float(plan.stop))
            target_at_open = (float(bar.open) >= float(plan.target) if plan.side is Side.BUY
                              else float(bar.open) <= float(plan.target))
            at_open = stop_at_open or target_at_open
            if not at_open and bar.close_time > holding_end:
                return incomplete("Holding deadline falls inside an execution bar",
                                  filled_at=filled_at, entry_price=entry_price, observed=observed)
            observed += 1
            if at_open:
                direction = 1.0 if plan.side is Side.BUY else -1.0
                move = direction * (float(bar.open) - entry_price) / risk
                bar_mfe, bar_mae = max(0.0, move), max(0.0, -move)
                stop_hit, target_hit = stop_at_open, target_at_open
            else:
                bar_mfe, bar_mae = self._excursions(plan, entry_price, bar)
                stop_hit, target_hit = self._terminal_hits(plan, bar)
            mfe_r, mae_r = max(mfe_r, bar_mfe), max(mae_r, bar_mae)

            if plan.entry_kind is EntryKind.LIMIT and index == entry_bar_index:
                stop_hit = target_hit = False
            if stop_hit and target_hit:
                return ReplayResult(
                    plan_id=plan.plan_id,
                    status=ReplayStatus.AMBIGUOUS_SAME_BAR,
                    filled_at=filled_at,
                    exited_at=bar.close_time,
                    entry_price=entry_price,
                    mfe_r=round(mfe_r, 6),
                    mae_r=round(mae_r, 6),
                    bars_observed=observed,
                    reason="Stop and target touched in the same bar",
                )
            if stop_hit or target_hit:
                if stop_hit:
                    slip = float(self.costs.stop_slippage_bps) / 10_000.0
                    # A resting stop cannot fill back at its trigger when
                    # the next observed market open is already worse.
                    stop_execution = float(bar.open) if stop_at_open else float(plan.stop)
                    exit_price = stop_execution * (
                        1.0 - slip if plan.side is Side.BUY else 1.0 + slip
                    )
                    status = ReplayStatus.SL_FIRST
                else:
                    slip = float(self.costs.target_slippage_bps) / 10_000.0
                    exit_price = float(plan.target) * (
                        1.0 - slip if plan.side is Side.BUY else 1.0 + slip
                    )
                    status = ReplayStatus.TP_FIRST
                direction = 1.0 if plan.side is Side.BUY else -1.0
                gross_r = direction * (exit_price - entry_price) / risk
                exit_fee = exit_price * float(self.costs.exit_fee_bps) / 10_000.0
                net_r = gross_r - (entry_fee + exit_fee) / risk
                return ReplayResult(
                    plan_id=plan.plan_id,
                    status=status,
                    filled_at=filled_at,
                    exited_at=bar.open_time if at_open else bar.close_time,
                    entry_price=round(entry_price, 10),
                    exit_price=round(exit_price, 10),
                    gross_r=round(gross_r, 6),
                    net_r=round(net_r, 6),
                    mfe_r=round(mfe_r, 6),
                    mae_r=round(mae_r, 6),
                    bars_observed=observed,
                )

        last_bar = bars[min(len(bars) - 1, entry_bar_index + max(0, observed - 1))]
        if not last_bar.close_time <= holding_end <= slot_end(last_bar):
            return incomplete("Execution data ends before the holding deadline",
                              filled_at=filled_at, entry_price=entry_price, observed=observed)
        exit_slip = float(self.costs.market_exit_slippage_bps) / 10_000.0
        exit_price = float(last_bar.close) * (
            1.0 - exit_slip if plan.side is Side.BUY else 1.0 + exit_slip
        )
        direction = 1.0 if plan.side is Side.BUY else -1.0
        gross_r = direction * (exit_price - entry_price) / risk
        exit_fee = exit_price * float(self.costs.exit_fee_bps) / 10_000.0
        net_r = gross_r - (entry_fee + exit_fee) / risk
        return ReplayResult(
            plan_id=plan.plan_id,
            status=ReplayStatus.OPEN_AT_HORIZON,
            filled_at=filled_at,
            exited_at=last_bar.close_time,
            entry_price=round(entry_price, 10),
            exit_price=round(exit_price, 10),
            gross_r=round(gross_r, 6),
            net_r=round(net_r, 6),
            mfe_r=round(mfe_r, 6),
            mae_r=round(mae_r, 6),
            bars_observed=observed,
            reason="Marked to market at replay horizon",
        )
