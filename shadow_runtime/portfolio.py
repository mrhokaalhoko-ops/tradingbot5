"""Executable-account simulation for the R4 Shadow Runtime.

The simulator enforces the frozen 10-position, 5x, 1%-margin and 0.25%-base
stop-risk model.  It creates ledger records only and has no exchange client.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from typing import Any

from .models import Candle, ExecutionQuote, Geometry, MarketSnapshot, utc_iso


MAX_OPEN_POSITIONS = 10
LEVERAGE = 5.0
TARGET_MARGIN_PCT = 1.0
BASE_STOP_RISK_PCT = 0.25
HARD_MAX_STOP_RISK_PCT = 1.0
MIN_STOP_DISTANCE_PCT = 0.003
MAX_NOTIONAL_BALANCE_PCT = 0.30


def _stable(prefix: str, value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def _floor_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
    return float(units * Decimal(str(step)))


class PortfolioSimulator:
    def __init__(self, store, *, initial_equity: float = 1_000.0):
        self.store = store
        self.initial_equity = float(initial_equity)

    def account(self, policy_id: str) -> dict[str, float]:
        realized = float(self.store.get_meta(f"realized_pnl:{policy_id}", 0.0))
        equity = self.initial_equity + realized
        used_margin = sum(float(row.get("initial_margin") or 0) for row in self.store.open_positions(policy_id))
        return {
            "equity": max(equity, 0.00000001),
            "available_balance": max(equity - used_margin, 0.0),
            "realized_pnl": realized,
        }

    def simulate_fill(
        self,
        *,
        policy_id: str,
        thesis_id: str,
        snapshot: MarketSnapshot,
        geometry: Geometry,
        execution_quote: ExecutionQuote,
        exchange_filter: dict[str, float],
    ) -> tuple[dict | None, dict, dict | None, str]:
        open_positions = self.store.open_positions(policy_id)
        costs = self._entry_costs(execution_quote)
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return None, costs, None, "REJECT_CAPACITY_10"
        account = self.account(policy_id)
        entry = geometry.executable_entry
        stop_distance = max(entry - geometry.stop, entry * MIN_STOP_DISTANCE_PCT)
        if stop_distance <= 0:
            return None, costs, None, "REJECT_INVALID_STOP_DISTANCE"
        target_margin = account["equity"] * TARGET_MARGIN_PCT / 100.0
        notional = min(
            target_margin * LEVERAGE,
            account["available_balance"] * 0.95 * LEVERAGE,
            account["equity"] * LEVERAGE,
            account["equity"] * MAX_NOTIONAL_BALANCE_PCT,
            account["equity"] * (BASE_STOP_RISK_PCT / 100.0) / stop_distance * entry,
        )
        quantity = _floor_step(notional / entry, float(exchange_filter.get("step_size") or 0))
        notional = quantity * entry
        filter_passed = bool(
            quantity > 0
            and quantity >= float(exchange_filter.get("min_qty") or 0)
            and notional >= float(exchange_filter.get("min_notional") or 0)
        )
        if not filter_passed:
            return None, costs, None, "REJECT_EXCHANGE_FILTERS"
        initial_margin = notional / LEVERAGE
        planned_risk = quantity * (entry - geometry.stop)
        risk_pct = planned_risk / account["equity"] * 100.0
        if risk_pct > HARD_MAX_STOP_RISK_PCT + 1e-9:
            return None, costs, None, "REJECT_HARD_RISK_CAP"
        simulation = {
            "equity_before": account["equity"],
            "available_balance_before": account["available_balance"],
            "leverage": LEVERAGE,
            "quantity": quantity,
            "notional": notional,
            "initial_margin": initial_margin,
            "initial_margin_pct_of_equity": initial_margin / account["equity"] * 100.0,
            "planned_stop_risk_amount": planned_risk,
            "planned_stop_risk_pct_of_equity": risk_pct,
            "base_stop_risk_pct_of_equity": BASE_STOP_RISK_PCT,
            "hard_max_stop_risk_pct_of_equity": HARD_MAX_STOP_RISK_PCT,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "open_positions_before": len(open_positions),
            "exchange_filters_passed": True,
            "sizing_formula_id": "R4_FIXED_MARGIN_WITH_STOP_RISK_CAP_V1",
        }
        position_id = _stable("position", {"policy": policy_id, "thesis": thesis_id})
        entry_fee = notional * 5.0 / 10_000.0
        state = {
            "policy_id": policy_id,
            "position_id": position_id,
            "thesis_id": thesis_id,
            "candidate_id": snapshot.candidate.candidate_id,
            "symbol": snapshot.symbol,
            "side": "LONG",
            "status": "OPEN",
            "opened_at": utc_iso(geometry.executable_quote_time),
            "updated_at": utc_iso(snapshot.as_of),
            "last_snapshot_id": snapshot.snapshot_id,
            "entry": entry,
            "entry_best_bid": execution_quote.best_bid,
            "entry_best_ask": execution_quote.best_ask,
            "stop": geometry.stop,
            "target": geometry.target,
            "risk_distance": entry - geometry.stop,
            "quantity": quantity,
            "notional": notional,
            "initial_margin": initial_margin,
            "entry_fee": entry_fee,
            "funding_cashflow": 0.0,
            "next_funding_time": utc_iso(snapshot.quote.next_funding_time) if snapshot.quote.next_funding_time else None,
            "funding_rate_at_next_settlement": snapshot.quote.funding_rate,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "geometry": geometry.as_dict(),
            "portfolio_simulation": simulation,
            "execution_costs": costs,
            "last_execution_quote_id": execution_quote.event_id,
            "last_execution_quote_time": utc_iso(execution_quote.event_time),
        }
        return simulation, costs, state, "SIMULATED_FILL"

    def update_position_quote(self, state: dict, quote: ExecutionQuote) -> tuple[dict, dict | None]:
        if state.get("status") != "OPEN":
            return state, None
        if state.get("last_execution_quote_id") == quote.event_id:
            return state, None
        last_time = state.get("last_execution_quote_time")
        if last_time and quote.event_time <= _parse_time(last_time):
            return state, None
        updated = dict(state)
        risk = float(updated["risk_distance"])
        entry = float(updated["entry"])
        bid = quote.best_bid
        updated["mfe_r"] = max(float(updated.get("mfe_r") or 0), (bid - entry) / risk)
        updated["mae_r"] = max(float(updated.get("mae_r") or 0), (entry - bid) / risk)

        next_funding = updated.get("next_funding_time")
        if next_funding:
            from datetime import datetime
            due = datetime.fromisoformat(str(next_funding).replace("Z", "+00:00"))
            if quote.event_time >= due:
                rate = float(updated.get("funding_rate_at_next_settlement") or 0)
                updated["funding_cashflow"] = float(updated.get("funding_cashflow") or 0) - float(updated["notional"]) * rate
                updated["next_funding_time"] = None

        opened = _parse_time(updated["opened_at"])
        terminal = None
        if bid <= float(updated["stop"]):
            terminal = ("SL", "BEST_BID_REACHED_STOP")
        elif bid >= float(updated["target"]):
            terminal = ("TP", "BEST_BID_REACHED_TARGET")
        elif quote.event_time >= opened + timedelta(minutes=60):
            terminal = ("TIME_EXIT", "FIRST_QUOTE_AT_OR_AFTER_60_MINUTES")
        updated["updated_at"] = utc_iso(quote.event_time)
        updated["last_execution_quote_id"] = quote.event_id
        updated["last_execution_quote_time"] = utc_iso(quote.event_time)
        if terminal is None:
            return updated, None
        return self._finalize(updated, quote, *terminal, same_bar_conflict=False)

    def reconcile_closed_bar(
        self, state: dict, candle: Candle, *, ordered_quote_coverage: bool,
    ) -> tuple[dict, dict | None]:
        """Resolve missing-stream intervals without inventing intrabar order."""
        if state.get("status") != "OPEN" or ordered_quote_coverage:
            return state, None
        hit_stop = candle.low <= float(state["stop"])
        hit_target = candle.high >= float(state["target"])
        if not hit_stop and not hit_target:
            return state, None
        if hit_stop and hit_target:
            status, reason, bid = "CAUSAL_AMBIGUOUS", "TP_AND_SL_TOUCHED_WITHOUT_ORDERED_QUOTES", float(state["stop"])
            conflict = True
        elif hit_stop:
            status, reason, bid = "SL", "CLOSED_1M_BAR_TOUCHED_ONLY_STOP_DURING_STREAM_GAP", float(state["stop"])
            conflict = False
        else:
            status, reason, bid = "TP", "CLOSED_1M_BAR_TOUCHED_ONLY_TARGET_DURING_STREAM_GAP", float(state["target"])
            conflict = False
        synthetic = ExecutionQuote(
            symbol=state["symbol"], event_time=candle.close_time, received_at=candle.close_time,
            best_bid=bid, best_ask=max(bid, bid * 1.00001),
            update_id=int(candle.close_time.timestamp() * 1000), source="CLOSED_1M_GAP_RECONCILIATION",
        )
        updated = dict(state)
        updated["updated_at"] = utc_iso(candle.close_time)
        return self._finalize(updated, synthetic, status, reason, same_bar_conflict=conflict)

    def _finalize(
        self, updated: dict, quote: ExecutionQuote, status: str, reason: str,
        *, same_bar_conflict: bool,
    ) -> tuple[dict, dict]:
        entry = float(updated["entry"])
        risk = float(updated["risk_distance"])
        bid = quote.best_bid
        exit_price = bid * 0.9999
        quantity = float(updated["quantity"])
        gross_pnl = (exit_price - entry) * quantity
        exit_fee = exit_price * quantity * 5.0 / 10_000.0
        entry_fee = float(updated["entry_fee"])
        funding = float(updated.get("funding_cashflow") or 0)
        net_pnl = gross_pnl - entry_fee - exit_fee + funding
        one_r_cash = quantity * risk
        gross_r = gross_pnl / one_r_cash
        fees_r = (entry_fee + exit_fee) / one_r_cash
        slippage_r = ((float(updated["entry_best_ask"]) * 0.0001) + (quote.best_bid * 0.0001)) / risk
        spread_r = (
            (float(updated["entry_best_ask"]) - float(updated["entry_best_bid"]))
            + (quote.best_ask - quote.best_bid)
        ) / (2.0 * risk)
        funding_r = funding / one_r_cash
        net_r = net_pnl / one_r_cash
        outcome = {
            "terminal_status": status,
            "exit_time": utc_iso(quote.event_time),
            "exit_price": exit_price,
            "exit_price_source": "BEST_BID_MINUS_1_BPS_ADVERSE_SLIPPAGE",
            "terminal_reason": reason,
            "gross_R": gross_r,
            "net_R": net_r,
            "fees_R": fees_r,
            "spread_R": max(spread_r, 0.0),
            "slippage_R": max(slippage_r, 0.0),
            "funding_R": funding_r,
            "MFE_R": float(updated["mfe_r"]),
            "MAE_R": float(updated["mae_r"]),
            "same_bar_conflict": bool(same_bar_conflict),
            "ordered_quote_event_id": None if same_bar_conflict else quote.event_id,
        }
        updated.update({
            "status": status,
            "exit_time": outcome["exit_time"],
            "exit_price": exit_price,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "outcome": outcome,
        })
        realized_key = f"realized_pnl:{updated['policy_id']}"
        self.store.put_meta(realized_key, float(self.store.get_meta(realized_key, 0.0)) + net_pnl)
        return updated, outcome

    @staticmethod
    def _entry_costs(quote: ExecutionQuote) -> dict:
        return {
            "model_id": "TAKER_QUOTES_5_PLUS_1_BPS_PER_SIDE_V1",
            "entry_spread_bps": quote.spread_bps,
            "entry_fee_bps": 5.0,
            "entry_slippage_bps": 1.0,
            "expected_exit_fee_bps": 5.0,
            "expected_exit_slippage_bps": 1.0,
            "actual_exit_spread_bps": None,
            "actual_exit_fee_bps": None,
            "actual_exit_slippage_bps": None,
            "spread_embedded_in_quotes": True,
            "generic_12_bps_subtracted_again": False,
        }


def terminal_costs(entry_costs: dict, quote: ExecutionQuote) -> dict:
    costs = dict(entry_costs)
    costs.update({
        "actual_exit_spread_bps": quote.spread_bps,
        "actual_exit_fee_bps": 5.0,
        "actual_exit_slippage_bps": 1.0,
    })
    return costs


def _parse_time(value: str):
    from datetime import datetime
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
