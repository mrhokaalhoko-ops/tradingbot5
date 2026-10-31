"""Pure Strategy V2 aggregate portfolio-risk gate; no execution surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from itertools import combinations

from .causality import require_utc
from .enums import Side
from .events import canonical_sha256
from .models import ThesisId
from .r4_risk import RiskPlan, RiskPlanStatus


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


def _decimal(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be finite Decimal")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'}")


def _fraction(value: Decimal) -> Fraction:
    _decimal(value, "numeric input", allow_zero=True)
    parts = value.as_tuple()
    if len(parts.digits) > 64 or abs(parts.exponent) > 64:
        raise ValueError("input outside portfolio numeric domain")
    return Fraction(value)


def _exact_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("nonterminating decimal outside portfolio policy")
    scale = max(twos, fives)
    if scale > 128:
        raise ValueError("result scale outside portfolio numeric domain")
    coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    if len(str(coefficient)) > 256:
        raise ValueError("result digits outside portfolio numeric domain")
    return Decimal((int(value < 0), tuple(map(int, str(coefficient))), -scale))


def _side_sign(side: Side) -> int:
    return 1 if side is Side.LONG else -1


class ExposureState(str, Enum):
    OPEN_PROTECTED = "OPEN_PROTECTED"
    ENTRY_PENDING = "ENTRY_PENDING"
    PENDING_PROTECTION = "PENDING_PROTECTION"
    UNKNOWN = "UNKNOWN"


class PortfolioGateStatus(str, Enum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    UNRESOLVED = "UNRESOLVED"


class PortfolioGateReason(str, Enum):
    CANDIDATE_RISK_NOT_READY = "CANDIDATE_RISK_NOT_READY"
    ACCOUNT_EQUITY_MISMATCH = "ACCOUNT_EQUITY_MISMATCH"
    FUTURE_EXPOSURE_EVIDENCE = "FUTURE_EXPOSURE_EVIDENCE"
    DEPENDENCY_EVIDENCE_MISSING = "DEPENDENCY_EVIDENCE_MISSING"
    DEPENDENCY_EVIDENCE_FUTURE = "DEPENDENCY_EVIDENCE_FUTURE"
    DEPENDENCY_HISTORY_INSUFFICIENT = "DEPENDENCY_HISTORY_INSUFFICIENT"
    CORRELATION_MATRIX_INCOMPLETE = "CORRELATION_MATRIX_INCOMPLETE"
    BETA_VECTOR_INCOMPLETE = "BETA_VECTOR_INCOMPLETE"
    EXPOSURE_IDENTITY_CONFLICT = "EXPOSURE_IDENTITY_CONFLICT"
    DUPLICATE_THESIS_EXPOSURE = "DUPLICATE_THESIS_EXPOSURE"
    UNKNOWN_EXPOSURE_PRESENT = "UNKNOWN_EXPOSURE_PRESENT"
    UNPROTECTED_EXPOSURE_PRESENT = "UNPROTECTED_EXPOSURE_PRESENT"
    POSITION_COUNT_LIMIT = "POSITION_COUNT_LIMIT"
    AGGREGATE_STOP_RISK_LIMIT = "AGGREGATE_STOP_RISK_LIMIT"
    GROSS_EXPOSURE_LIMIT = "GROSS_EXPOSURE_LIMIT"
    NET_EXPOSURE_LIMIT = "NET_EXPOSURE_LIMIT"
    CLUSTER_RISK_LIMIT = "CLUSTER_RISK_LIMIT"
    BETA_WEIGHTED_EXPOSURE_LIMIT = "BETA_WEIGHTED_EXPOSURE_LIMIT"
    NUMERIC_UNCOMPUTABLE = "NUMERIC_UNCOMPUTABLE"


@dataclass(frozen=True, slots=True)
class PositionExposure:
    exposure_id: str
    symbol: str
    contract_epoch_id: str
    cluster_id: str
    side: Side
    state: ExposureState
    thesis_id: ThesisId
    notional: Decimal
    worst_case_stop_loss: Decimal
    initial_margin: Decimal
    observed_at: datetime
    available_at: datetime
    source_event_ids: tuple[str, ...]
    record_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("exposure_id", "symbol", "contract_epoch_id", "cluster_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.side, Side) or not isinstance(self.state, ExposureState):
            raise TypeError("typed exposure side and state required")
        if type(self.thesis_id) is not ThesisId:
            raise TypeError("typed exposure thesis ID required")
        for name in ("notional", "worst_case_stop_loss", "initial_margin"):
            _decimal(getattr(self, name), name, allow_zero=name == "initial_margin")
        require_utc(self.observed_at, "observed_at")
        require_utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise ValueError("exposure cannot be available before observation")
        if (not isinstance(self.source_event_ids, tuple) or not self.source_event_ids
                or len(set(self.source_event_ids)) != len(self.source_event_ids)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.source_event_ids)):
            raise ValueError("unique exposure source events required")
        if (self.state in (ExposureState.PENDING_PROTECTION, ExposureState.UNKNOWN)
                and self.worst_case_stop_loss < self.notional):
            raise ValueError("unprotected or unknown exposure must reserve full notional loss")
        object.__setattr__(self, "record_id", "exp_" + canonical_sha256((
            self.exposure_id, self.symbol, self.contract_epoch_id,
            self.cluster_id, self.side, self.state, self.notional,
            self.thesis_id,
            self.worst_case_stop_loss, self.initial_margin,
            self.observed_at, self.available_at, self.source_event_ids,
        )).lower())


@dataclass(frozen=True, slots=True)
class PairCorrelation:
    symbol_a: str
    symbol_b: str
    coefficient: Decimal

    def __post_init__(self) -> None:
        _text(self.symbol_a, "symbol_a")
        _text(self.symbol_b, "symbol_b")
        if self.symbol_a >= self.symbol_b:
            raise ValueError("correlation pair must use sorted distinct symbols")
        if (not isinstance(self.coefficient, Decimal)
                or not self.coefficient.is_finite()
                or not D_MINUS_ONE <= self.coefficient <= D_ONE):
            raise ValueError("correlation coefficient must be in [-1,1]")


D_MINUS_ONE = Decimal("-1")
D_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class SymbolBeta:
    symbol: str
    beta: Decimal

    def __post_init__(self) -> None:
        _text(self.symbol, "symbol")
        if not isinstance(self.beta, Decimal) or not self.beta.is_finite():
            raise ValueError("finite beta required")
        if abs(self.beta) > Decimal("10"):
            raise ValueError("beta outside explicit research domain")


@dataclass(frozen=True, slots=True)
class DependencySnapshot:
    snapshot_id: str
    benchmark_symbol: str
    window_start: datetime
    window_end: datetime
    confirmed_at: datetime
    available_at: datetime
    observation_count: int
    correlations: tuple[PairCorrelation, ...]
    betas: tuple[SymbolBeta, ...]
    source_hash: str
    dependency_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _text(self.benchmark_symbol, "benchmark_symbol")
        for name in ("window_start", "window_end", "confirmed_at", "available_at"):
            require_utc(getattr(self, name), name)
        if not self.window_start < self.window_end <= self.confirmed_at <= self.available_at:
            raise ValueError("dependency chronology invalid")
        if type(self.observation_count) is not int or self.observation_count < 1:
            raise ValueError("positive observation_count required")
        if (not isinstance(self.correlations, tuple)
                or any(type(item) is not PairCorrelation for item in self.correlations)):
            raise TypeError("typed immutable correlations required")
        if (not isinstance(self.betas, tuple) or not self.betas
                or any(type(item) is not SymbolBeta for item in self.betas)):
            raise TypeError("typed immutable betas required")
        pairs = tuple((item.symbol_a, item.symbol_b) for item in self.correlations)
        symbols = tuple(item.symbol for item in self.betas)
        if len(set(pairs)) != len(pairs) or len(set(symbols)) != len(symbols):
            raise ValueError("duplicate dependency entries forbidden")
        _digest(self.source_hash, "source_hash")
        object.__setattr__(self, "dependency_hash", canonical_sha256((
            self.snapshot_id, self.benchmark_symbol, self.window_start,
            self.window_end, self.confirmed_at, self.available_at,
            self.observation_count, self.correlations, self.betas,
            self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class PortfolioPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    max_positions: int
    max_aggregate_stop_risk_fraction: Decimal
    max_gross_exposure_fraction: Decimal
    max_absolute_net_exposure_fraction: Decimal
    max_cluster_stop_risk_fraction: Decimal
    max_absolute_beta_exposure_fraction: Decimal
    minimum_dependency_observations: int
    block_on_unprotected_exposure: bool
    numeric_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        for name in ("policy_id", "rule_version", "numeric_policy_id"):
            _text(getattr(self, name), name)
        if type(self.max_positions) is not int or self.max_positions < 1:
            raise ValueError("positive max_positions required")
        if type(self.minimum_dependency_observations) is not int or self.minimum_dependency_observations < 2:
            raise ValueError("minimum_dependency_observations must be at least two")
        if type(self.block_on_unprotected_exposure) is not bool:
            raise TypeError("block_on_unprotected_exposure must be bool")
        for name in (
            "max_aggregate_stop_risk_fraction", "max_gross_exposure_fraction",
            "max_absolute_net_exposure_fraction",
            "max_cluster_stop_risk_fraction",
            "max_absolute_beta_exposure_fraction",
        ):
            value = getattr(self, name)
            _decimal(value, name)
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
            self.max_positions, self.max_aggregate_stop_risk_fraction,
            self.max_gross_exposure_fraction,
            self.max_absolute_net_exposure_fraction,
            self.max_cluster_stop_risk_fraction,
            self.max_absolute_beta_exposure_fraction,
            self.minimum_dependency_observations,
            self.block_on_unprotected_exposure, self.numeric_policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class PortfolioGateResult:
    status: PortfolioGateStatus
    reason: PortfolioGateReason | None
    candidate_risk_plan_id: str
    position_count: int | None
    gross_exposure: Decimal | None
    net_exposure: Decimal | None
    aggregate_stop_risk: Decimal | None
    candidate_cluster_stop_risk: Decimal | None
    beta_weighted_net_exposure: Decimal | None
    dependency_hash: str | None
    policy_variant_hash: str
    decision_at: datetime
    result_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, PortfolioGateStatus):
            raise TypeError("typed portfolio status required")
        _text(self.candidate_risk_plan_id, "candidate_risk_plan_id")
        _digest(self.policy_variant_hash, "policy_variant_hash")
        if self.dependency_hash is not None:
            _digest(self.dependency_hash, "dependency_hash")
        require_utc(self.decision_at, "decision_at")
        if self.status is PortfolioGateStatus.APPROVED:
            if self.reason is not None or any(value is None for value in (
                self.position_count, self.gross_exposure, self.net_exposure,
                self.aggregate_stop_risk, self.candidate_cluster_stop_risk,
                self.beta_weighted_net_exposure, self.dependency_hash,
            )):
                raise ValueError("approved portfolio gate needs complete metrics")
        elif not isinstance(self.reason, PortfolioGateReason):
            raise ValueError("non-approved portfolio gate needs typed reason")
        object.__setattr__(self, "result_id", "pfg_" + canonical_sha256((
            self.status, self.reason, self.candidate_risk_plan_id,
            self.position_count, self.gross_exposure, self.net_exposure,
            self.aggregate_stop_risk, self.candidate_cluster_stop_risk,
            self.beta_weighted_net_exposure, self.dependency_hash,
            self.policy_variant_hash,
        )).lower())


def _result(
    status: PortfolioGateStatus,
    reason: PortfolioGateReason | None,
    candidate: RiskPlan,
    policy: PortfolioPolicySpec,
    decision_at: datetime,
    *,
    metrics: tuple[int, Decimal, Decimal, Decimal, Decimal, Decimal] | None = None,
    dependency_hash: str | None = None,
) -> PortfolioGateResult:
    values = metrics or (None, None, None, None, None, None)
    return PortfolioGateResult(
        status, reason, candidate.plan_id, *values, dependency_hash,
        policy.variant_hash, decision_at,
    )


def evaluate_portfolio_gate(
    *,
    candidate: RiskPlan,
    candidate_cluster_id: str,
    existing: tuple[PositionExposure, ...],
    account_equity: Decimal,
    dependencies: DependencySnapshot | None,
    policy: PortfolioPolicySpec,
    decision_at: datetime,
) -> PortfolioGateResult:
    """Evaluate projected aggregate risk without mutating positions or orders."""

    require_utc(decision_at, "decision_at")
    if not isinstance(candidate, RiskPlan) or not isinstance(policy, PortfolioPolicySpec):
        raise TypeError("typed risk plan and portfolio policy required")
    _text(candidate_cluster_id, "candidate_cluster_id")
    if not isinstance(existing, tuple) or any(type(item) is not PositionExposure for item in existing):
        raise TypeError("typed immutable existing exposures required")
    _decimal(account_equity, "account_equity")
    if candidate.status is not RiskPlanStatus.READY:
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.CANDIDATE_RISK_NOT_READY,
            candidate, policy, decision_at,
        )
    if candidate.account_equity != account_equity:
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.ACCOUNT_EQUITY_MISMATCH,
            candidate, policy, decision_at,
        )
    if len({item.exposure_id for item in existing}) != len(existing):
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.EXPOSURE_IDENTITY_CONFLICT,
            candidate, policy, decision_at,
        )
    if any(item.thesis_id == candidate.thesis_id for item in existing):
        return _result(
            PortfolioGateStatus.BLOCKED,
            PortfolioGateReason.DUPLICATE_THESIS_EXPOSURE,
            candidate, policy, decision_at,
        )
    if any(item.available_at > decision_at for item in existing):
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.FUTURE_EXPOSURE_EVIDENCE,
            candidate, policy, decision_at,
        )
    if any(item.state is ExposureState.UNKNOWN for item in existing):
        return _result(
            PortfolioGateStatus.BLOCKED,
            PortfolioGateReason.UNKNOWN_EXPOSURE_PRESENT,
            candidate, policy, decision_at,
        )
    if (policy.block_on_unprotected_exposure
            and any(item.state is ExposureState.PENDING_PROTECTION for item in existing)):
        return _result(
            PortfolioGateStatus.BLOCKED,
            PortfolioGateReason.UNPROTECTED_EXPOSURE_PRESENT,
            candidate, policy, decision_at,
        )
    if dependencies is None:
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.DEPENDENCY_EVIDENCE_MISSING,
            candidate, policy, decision_at,
        )
    if not isinstance(dependencies, DependencySnapshot):
        raise TypeError("typed dependency snapshot required")
    if dependencies.available_at > decision_at or dependencies.window_end > decision_at:
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.DEPENDENCY_EVIDENCE_FUTURE,
            candidate, policy, decision_at,
        )
    if dependencies.observation_count < policy.minimum_dependency_observations:
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.DEPENDENCY_HISTORY_INSUFFICIENT,
            candidate, policy, decision_at,
            dependency_hash=dependencies.dependency_hash,
        )
    symbols = tuple(sorted({candidate.symbol, *(item.symbol for item in existing)}))
    required_pairs = set(combinations(symbols, 2))
    actual_pairs = {(item.symbol_a, item.symbol_b) for item in dependencies.correlations}
    if not required_pairs.issubset(actual_pairs):
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.CORRELATION_MATRIX_INCOMPLETE,
            candidate, policy, decision_at,
            dependency_hash=dependencies.dependency_hash,
        )
    betas = {item.symbol: item.beta for item in dependencies.betas}
    if not set(symbols).issubset(betas):
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.BETA_VECTOR_INCOMPLETE,
            candidate, policy, decision_at,
            dependency_hash=dependencies.dependency_hash,
        )
    try:
        equity = _fraction(account_equity)
        candidate_notional = _fraction(candidate.notional)
        candidate_risk = _fraction(candidate.projected_total_loss)
        gross = candidate_notional + sum((_fraction(item.notional) for item in existing), Fraction())
        net = (_side_sign(candidate.side) * candidate_notional
               + sum((_side_sign(item.side) * _fraction(item.notional)
                      for item in existing), Fraction()))
        aggregate_risk = candidate_risk + sum(
            (_fraction(item.worst_case_stop_loss) for item in existing), Fraction()
        )
        cluster_risk = candidate_risk + sum(
            (_fraction(item.worst_case_stop_loss) for item in existing
             if item.cluster_id == candidate_cluster_id), Fraction()
        )
        beta_net = (
            _side_sign(candidate.side) * candidate_notional * _fraction(betas[candidate.symbol])
            + sum((_side_sign(item.side) * _fraction(item.notional)
                   * _fraction(betas[item.symbol]) for item in existing), Fraction())
        )
        metrics = (
            len(existing) + 1, _exact_decimal(gross), _exact_decimal(net),
            _exact_decimal(aggregate_risk), _exact_decimal(cluster_risk),
            _exact_decimal(beta_net),
        )
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        return _result(
            PortfolioGateStatus.UNRESOLVED,
            PortfolioGateReason.NUMERIC_UNCOMPUTABLE,
            candidate, policy, decision_at,
            dependency_hash=dependencies.dependency_hash,
        )
    checks = (
        (len(existing) + 1 > policy.max_positions,
         PortfolioGateReason.POSITION_COUNT_LIMIT),
        (aggregate_risk > equity * _fraction(policy.max_aggregate_stop_risk_fraction),
         PortfolioGateReason.AGGREGATE_STOP_RISK_LIMIT),
        (gross > equity * _fraction(policy.max_gross_exposure_fraction),
         PortfolioGateReason.GROSS_EXPOSURE_LIMIT),
        (abs(net) > equity * _fraction(policy.max_absolute_net_exposure_fraction),
         PortfolioGateReason.NET_EXPOSURE_LIMIT),
        (cluster_risk > equity * _fraction(policy.max_cluster_stop_risk_fraction),
         PortfolioGateReason.CLUSTER_RISK_LIMIT),
        (abs(beta_net) > equity * _fraction(policy.max_absolute_beta_exposure_fraction),
         PortfolioGateReason.BETA_WEIGHTED_EXPOSURE_LIMIT),
    )
    for breached, reason in checks:
        if breached:
            return _result(
                PortfolioGateStatus.BLOCKED, reason, candidate, policy,
                decision_at, metrics=metrics,
                dependency_hash=dependencies.dependency_hash,
            )
    return _result(
        PortfolioGateStatus.APPROVED, None, candidate, policy, decision_at,
        metrics=metrics, dependency_hash=dependencies.dependency_hash,
    )
