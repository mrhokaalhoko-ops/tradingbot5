"""Pure R9 causal outcome replay for research proposals; never an execution simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from .causality import require_utc
from .enums import Side
from .events import canonical_sha256
from .r3_planning import CausalTargetPlan, InitialStopPlan, PlanStatus, TargetMode
from .r4_risk import RiskPlan, RiskPlanStatus
from .r6_entry import EntryGateStatus, EntryQualification
from .r7_proposal import AtomicResearchProposal


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        raise ValueError(f"{name} must be a 64-character hex digest")


def _decimal(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be finite Decimal")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'}")
    parts = value.as_tuple()
    if len(parts.digits) > 64 or abs(parts.exponent) > 64:
        raise ValueError(f"{name} outside replay numeric domain")


class ReplayFeedKind(str, Enum):
    USD_M_FUTURES_TRADE_PRICE_FINAL_KLINE = "USD_M_FUTURES_TRADE_PRICE_FINAL_KLINE"


class ReplayOutcomeStatus(str, Enum):
    TARGET_HIT = "TARGET_HIT"
    STOP_HIT = "STOP_HIT"
    OPEN = "OPEN"
    CENSORED = "CENSORED"
    UNRESOLVED = "UNRESOLVED"


class ReplayOutcomeReason(str, Enum):
    DUAL_TOUCH_AMBIGUOUS = "DUAL_TOUCH_AMBIGUOUS"
    DATA_GAP = "DATA_GAP"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    PRE_ENTRY_BAR = "PRE_ENTRY_BAR"
    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    RUNNER_POLICY_NOT_REPLAYABLE = "RUNNER_POLICY_NOT_REPLAYABLE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DUPLICATE_BAR = "DUPLICATE_BAR"
    TIMEFRAME_MISMATCH = "TIMEFRAME_MISMATCH"
    EVALUATION_BEFORE_DECISION = "EVALUATION_BEFORE_DECISION"


@dataclass(frozen=True, slots=True)
class ReplayBar:
    bar_id: str
    symbol: str
    contract_epoch_id: str
    timeframe: str
    feed_kind: ReplayFeedKind
    open_time: datetime
    close_time: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source_hash: str
    record_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("bar_id", "symbol", "contract_epoch_id", "timeframe"):
            _text(getattr(self, name), name)
        if not isinstance(self.feed_kind, ReplayFeedKind):
            raise TypeError("typed replay feed kind required")
        for name in ("open_time", "close_time", "available_at"):
            require_utc(getattr(self, name), name)
        if not self.open_time < self.close_time <= self.available_at:
            raise ValueError("bar chronology invalid")
        for name in ("open", "high", "low", "close"):
            _decimal(getattr(self, name), name)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high below OHLC maximum")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low above OHLC minimum")
        _digest(self.source_hash, "source_hash")
        object.__setattr__(self, "record_id", "rpb_" + canonical_sha256((
            self.bar_id, self.symbol, self.contract_epoch_id, self.timeframe,
            self.feed_kind, self.open_time, self.close_time, self.available_at,
            self.open, self.high, self.low, self.close, self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class ReplayCase:
    proposal_assembly_id: str
    symbol: str
    contract_epoch_id: str
    side: Side
    decision_at: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    target_mode: TargetMode
    entry_qualification_id: str
    stop_plan_id: str
    target_plan_id: str
    risk_plan_id: str
    case_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in (
            "proposal_assembly_id", "symbol", "contract_epoch_id",
            "entry_qualification_id", "stop_plan_id", "target_plan_id",
            "risk_plan_id",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.side, Side) or not isinstance(self.target_mode, TargetMode):
            raise TypeError("typed side and target mode required")
        require_utc(self.decision_at, "decision_at")
        _decimal(self.entry_price, "entry_price")
        _decimal(self.stop_price, "stop_price")
        if self.target_price is not None:
            _decimal(self.target_price, "target_price")
        if self.side is Side.LONG and not self.stop_price < self.entry_price:
            raise ValueError("LONG replay stop must be below entry")
        if self.side is Side.SHORT and not self.stop_price > self.entry_price:
            raise ValueError("SHORT replay stop must be above entry")
        if self.target_mode is TargetMode.STRUCTURAL_FINAL_TP:
            if self.target_price is None:
                raise ValueError("structural replay requires target")
            if self.side is Side.LONG and not self.target_price > self.entry_price:
                raise ValueError("LONG replay target must be above entry")
            if self.side is Side.SHORT and not self.target_price < self.entry_price:
                raise ValueError("SHORT replay target must be below entry")
        elif self.target_price is not None:
            raise ValueError("runner replay cannot carry fixed target")
        object.__setattr__(self, "case_id", "rpc_" + canonical_sha256((
            self.proposal_assembly_id, self.symbol, self.contract_epoch_id,
            self.side, self.decision_at, self.entry_price, self.stop_price,
            self.target_price, self.target_mode, self.entry_qualification_id,
            self.stop_plan_id, self.target_plan_id, self.risk_plan_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    status: ReplayOutcomeStatus
    reason: ReplayOutcomeReason | None
    case_id: str
    evaluation_at: datetime
    terminal_bar_id: str | None
    exit_price: Decimal | None
    gross_r_numerator: int | None
    gross_r_denominator: int | None
    source_record_ids: tuple[str, ...]
    outcome_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReplayOutcomeStatus):
            raise TypeError("typed replay outcome required")
        _text(self.case_id, "case_id")
        require_utc(self.evaluation_at, "evaluation_at")
        if self.status in (ReplayOutcomeStatus.TARGET_HIT, ReplayOutcomeStatus.STOP_HIT):
            if (
                self.reason is not None
                or self.terminal_bar_id is None
                or self.exit_price is None
                or type(self.gross_r_numerator) is not int
                or type(self.gross_r_denominator) is not int
                or self.gross_r_denominator <= 0
            ):
                raise ValueError("terminal outcome requires exact exit and R multiple")
        elif self.status is ReplayOutcomeStatus.OPEN:
            if any(value is not None for value in (
                self.reason, self.terminal_bar_id, self.exit_price,
                self.gross_r_numerator, self.gross_r_denominator,
            )):
                raise ValueError("open outcome cannot carry terminal fields")
        else:
            if not isinstance(self.reason, ReplayOutcomeReason):
                raise ValueError("non-terminal replay result needs typed reason")
            if any(value is not None for value in (
                self.exit_price, self.gross_r_numerator, self.gross_r_denominator,
            )):
                raise ValueError("unresolved/censored result cannot claim PnL")
        if (
            not isinstance(self.source_record_ids, tuple)
            or len(set(self.source_record_ids)) != len(self.source_record_ids)
            or any(not isinstance(item, str) or not item.strip()
                   for item in self.source_record_ids)
        ):
            raise ValueError("unique replay sources required")
        object.__setattr__(self, "outcome_id", "rpo_" + canonical_sha256((
            self.status, self.reason, self.case_id, self.evaluation_at,
            self.terminal_bar_id, self.exit_price, self.gross_r_numerator,
            self.gross_r_denominator, self.source_record_ids,
        )).lower())


def build_replay_case(
    *,
    proposal: AtomicResearchProposal,
    entry: EntryQualification,
    stop: InitialStopPlan,
    target: CausalTargetPlan,
    risk: RiskPlan,
) -> ReplayCase:
    if not all(isinstance(value, expected) for value, expected in (
        (proposal, AtomicResearchProposal), (entry, EntryQualification),
        (stop, InitialStopPlan), (target, CausalTargetPlan), (risk, RiskPlan),
    )):
        raise TypeError("typed replay case inputs required")
    if any(getattr(value, "execution_authority", True)
           for value in (proposal, entry, stop, target, risk)):
        raise ValueError("replay case cannot carry execution authority")
    if not (
        entry.status is EntryGateStatus.READY
        and stop.status is PlanStatus.READY
        and target.status is PlanStatus.READY
        and risk.status is RiskPlanStatus.READY
    ):
        raise ValueError("replay case requires ready components")
    if not (
        proposal.entry_qualification_id == entry.qualification_id
        and proposal.stop_plan_id == stop.plan_id
        and proposal.target_plan_id == target.plan_id
        and proposal.risk_plan_id == risk.plan_id
    ):
        raise ValueError("proposal/component references disagree")
    if not (
        proposal.decision.side is entry.side is stop.side is target.side is risk.side
        and proposal.decision.thesis_id == entry.thesis_id == stop.thesis_id
        == risk.thesis_id
        and proposal.decision.symbol == risk.symbol
        and proposal.contract_epoch_id == risk.contract_epoch_id
        and entry.executable_price == stop.entry_price == target.entry_price
        == risk.entry_price
        and stop.final_stop == target.stop_price == risk.hard_stop_price
    ):
        raise ValueError("replay identity or geometry mismatch")
    selected_price = target.selected_target.price if target.selected_target else None
    return ReplayCase(
        proposal.assembly_id, proposal.decision.symbol,
        proposal.contract_epoch_id, proposal.decision.side,
        proposal.decision.decision_at, entry.executable_price, stop.final_stop,
        selected_price, target.mode, entry.qualification_id, stop.plan_id,
        target.plan_id, risk.plan_id,
    )


def _result(
    status: ReplayOutcomeStatus,
    case: ReplayCase,
    evaluation_at: datetime,
    sources: tuple[str, ...],
    *,
    reason: ReplayOutcomeReason | None = None,
    bar: ReplayBar | None = None,
    exit_price: Decimal | None = None,
) -> ReplayOutcome:
    numerator = denominator = None
    if exit_price is not None:
        entry = Fraction(case.entry_price)
        stop = Fraction(case.stop_price)
        exit_fraction = Fraction(exit_price)
        if case.side.value == "LONG":
            gross_r = (exit_fraction - entry) / (entry - stop)
        else:
            gross_r = (entry - exit_fraction) / (stop - entry)
        numerator, denominator = gross_r.numerator, gross_r.denominator
    return ReplayOutcome(
        status, reason, case.case_id, evaluation_at,
        bar.bar_id if bar else None, exit_price, numerator, denominator,
        sources,
    )


def evaluate_replay_case(
    *,
    case: ReplayCase,
    bars: tuple[ReplayBar, ...],
    evaluation_at: datetime,
) -> ReplayOutcome:
    """Evaluate final bars after entry; ambiguity and missing data remain unscored."""

    if not isinstance(case, ReplayCase):
        raise TypeError("typed replay case required")
    require_utc(evaluation_at, "evaluation_at")
    if not isinstance(bars, tuple) or any(type(bar) is not ReplayBar for bar in bars):
        raise TypeError("typed immutable replay bars required")
    if case.target_mode is TargetMode.FULL_POSITION_RUNNER:
        return _result(
            ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at, (),
            reason=ReplayOutcomeReason.RUNNER_POLICY_NOT_REPLAYABLE,
        )
    if evaluation_at < case.decision_at:
        return _result(
            ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at, (),
            reason=ReplayOutcomeReason.EVALUATION_BEFORE_DECISION,
        )
    if not bars:
        return _result(
            ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at, (),
            reason=ReplayOutcomeReason.DATA_GAP,
        )
    source_ids: list[str] = []
    bar_ids: set[str] = set()
    previous: ReplayBar | None = None
    duration = None
    for bar in bars:
        source_ids.append(bar.record_id)
        if bar.bar_id in bar_ids:
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.DUPLICATE_BAR,
            )
        bar_ids.add(bar.bar_id)
        if bar.execution_authority or (
            bar.symbol != case.symbol
            or bar.contract_epoch_id != case.contract_epoch_id
        ):
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.IDENTITY_MISMATCH,
            )
        if bar.available_at > evaluation_at:
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.FUTURE_EVIDENCE,
            )
        if bar.open_time < case.decision_at:
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.PRE_ENTRY_BAR,
            )
        if previous is None and bar.open_time > case.decision_at:
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.DATA_GAP,
            )
        current_duration = bar.close_time - bar.open_time
        if previous is None:
            duration = current_duration
        elif (
            bar.open_time < previous.close_time
            or bar.close_time <= previous.close_time
            or bar.available_at <= previous.available_at
        ):
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.OUT_OF_ORDER,
            )
        elif bar.timeframe != previous.timeframe:
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.TIMEFRAME_MISMATCH,
            )
        elif bar.open_time != previous.close_time or current_duration != duration:
            return _result(
                ReplayOutcomeStatus.UNRESOLVED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.DATA_GAP,
            )
        previous = bar

        target = case.target_price
        if case.side is Side.LONG:
            if bar.open <= case.stop_price:
                return _result(
                    ReplayOutcomeStatus.STOP_HIT, case, evaluation_at,
                    tuple(source_ids), bar=bar, exit_price=bar.open,
                )
            if bar.open >= target:
                return _result(
                    ReplayOutcomeStatus.TARGET_HIT, case, evaluation_at,
                    tuple(source_ids), bar=bar, exit_price=target,
                )
            stop_hit = bar.low <= case.stop_price
            target_hit = bar.high >= target
        else:
            if bar.open >= case.stop_price:
                return _result(
                    ReplayOutcomeStatus.STOP_HIT, case, evaluation_at,
                    tuple(source_ids), bar=bar, exit_price=bar.open,
                )
            if bar.open <= target:
                return _result(
                    ReplayOutcomeStatus.TARGET_HIT, case, evaluation_at,
                    tuple(source_ids), bar=bar, exit_price=target,
                )
            stop_hit = bar.high >= case.stop_price
            target_hit = bar.low <= target
        if stop_hit and target_hit:
            return _result(
                ReplayOutcomeStatus.CENSORED, case, evaluation_at,
                tuple(source_ids), reason=ReplayOutcomeReason.DUAL_TOUCH_AMBIGUOUS,
                bar=bar,
            )
        if stop_hit:
            return _result(
                ReplayOutcomeStatus.STOP_HIT, case, evaluation_at,
                tuple(source_ids), bar=bar, exit_price=case.stop_price,
            )
        if target_hit:
            return _result(
                ReplayOutcomeStatus.TARGET_HIT, case, evaluation_at,
                tuple(source_ids), bar=bar, exit_price=target,
            )
    return _result(
        ReplayOutcomeStatus.OPEN, case, evaluation_at, tuple(source_ids),
    )
