"""Pure R10 cost-aware cohort validation; no execution or profitability claims."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from .events import canonical_sha256
from .causality import require_utc
from .r4_risk import CostModelSpec, RiskPlan, RiskPlanStatus
from .r7_proposal import AtomicResearchProposal
from .r9_replay import ReplayCase, ReplayOutcome, ReplayOutcomeStatus


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


def _bounded_fraction(value: Decimal, name: str) -> Fraction:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be finite Decimal")
    parts = value.as_tuple()
    if len(parts.digits) > 64 or abs(parts.exponent) > 64:
        raise ValueError(f"{name} outside validation numeric domain")
    return Fraction(value)


class ValidationSegment(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    TUNING = "TUNING"
    HOLDOUT = "HOLDOUT"


class ValidationVerdict(str, Enum):
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNRESOLVED = "UNRESOLVED"


class ValidationReason(str, Enum):
    MIXED_COHORT = "MIXED_COHORT"
    DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
    SAMPLE_TOO_SMALL = "SAMPLE_TOO_SMALL"
    SCORED_SAMPLE_TOO_SMALL = "SCORED_SAMPLE_TOO_SMALL"
    DISTINCT_THESIS_TOO_SMALL = "DISTINCT_THESIS_TOO_SMALL"
    CENSORED_RATE_TOO_HIGH = "CENSORED_RATE_TOO_HIGH"
    UNRESOLVED_RATE_TOO_HIGH = "UNRESOLVED_RATE_TOO_HIGH"
    OPEN_RATE_TOO_HIGH = "OPEN_RATE_TOO_HIGH"
    MEAN_NET_R_BELOW_MINIMUM = "MEAN_NET_R_BELOW_MINIMUM"


@dataclass(frozen=True, slots=True)
class ValidationSplitManifest:
    source_commit: str
    manifest_id: str
    strategy_variant_hash: str
    development_start: datetime
    development_end: datetime
    tuning_end: datetime
    holdout_end: datetime
    manifest_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        _text(self.manifest_id, "manifest_id")
        _digest(self.strategy_variant_hash, "strategy_variant_hash")
        for name in (
            "development_start", "development_end", "tuning_end", "holdout_end",
        ):
            require_utc(getattr(self, name), name)
        if not (
            self.development_start < self.development_end
            < self.tuning_end < self.holdout_end
        ):
            raise ValueError("validation split boundaries must be strictly ordered")
        object.__setattr__(self, "manifest_hash", canonical_sha256((
            self.source_commit, self.manifest_id, self.strategy_variant_hash,
            self.development_start, self.development_end,
            self.tuning_end, self.holdout_end,
        )).lower())

    def segment_for(self, decision_at: datetime) -> ValidationSegment:
        require_utc(decision_at, "decision_at")
        if not self.development_start <= decision_at < self.holdout_end:
            raise ValueError("decision lies outside preregistered split interval")
        if decision_at < self.development_end:
            return ValidationSegment.DEVELOPMENT
        if decision_at < self.tuning_end:
            return ValidationSegment.TUNING
        return ValidationSegment.HOLDOUT


def _exact_performance(
    case: ReplayCase,
    outcome: ReplayOutcome,
    risk: RiskPlan,
    cost_model: CostModelSpec,
) -> tuple[int | None, int | None, int | None, int | None]:
    if outcome.status not in (
        ReplayOutcomeStatus.TARGET_HIT, ReplayOutcomeStatus.STOP_HIT,
    ):
        return None, None, None, None
    entry = _bounded_fraction(case.entry_price, "entry_price")
    stop = _bounded_fraction(case.stop_price, "stop_price")
    observed_exit = _bounded_fraction(outcome.exit_price, "exit_price")
    quantity = _bounded_fraction(risk.quantity, "quantity")
    raw_r = (
        (observed_exit - entry) / (entry - stop)
        if case.side.value == "LONG"
        else (entry - observed_exit) / (stop - entry)
    )
    if (outcome.gross_r_numerator, outcome.gross_r_denominator) != (
        raw_r.numerator, raw_r.denominator,
    ):
        raise ValueError("replay gross R disagrees with case geometry")
    stop_slippage = _bounded_fraction(
        cost_model.stop_slippage_rate, "stop_slippage_rate"
    )
    adverse_exit = observed_exit
    if outcome.status is ReplayOutcomeStatus.STOP_HIT:
        adverse_exit = (
            observed_exit * (1 - stop_slippage)
            if case.side.value == "LONG"
            else observed_exit * (1 + stop_slippage)
        )
    entry_cost_rate = sum((
        _bounded_fraction(cost_model.entry_fee_rate, "entry_fee_rate"),
        _bounded_fraction(cost_model.entry_slippage_rate, "entry_slippage_rate"),
        _bounded_fraction(cost_model.funding_rate_abs, "funding_rate_abs"),
        _bounded_fraction(cost_model.uncertainty_rate, "uncertainty_rate"),
    ), Fraction(0))
    exit_fee_rate = _bounded_fraction(cost_model.exit_fee_rate, "exit_fee_rate")
    normal_adverse_stop = (
        stop * (1 - stop_slippage)
        if case.side.value == "LONG"
        else stop * (1 + stop_slippage)
    )
    planned_price_loss = (
        entry - normal_adverse_stop
        if case.side.value == "LONG"
        else normal_adverse_stop - entry
    )
    planned_per_unit = (
        planned_price_loss
        + entry * entry_cost_rate
        + normal_adverse_stop * exit_fee_rate
    )
    planned_total_loss = quantity * planned_per_unit
    if (
        _bounded_fraction(risk.entry_price, "risk.entry_price") != entry
        or _bounded_fraction(risk.hard_stop_price, "risk.hard_stop_price") != stop
        or _bounded_fraction(risk.notional, "risk.notional") != quantity * entry
        or _bounded_fraction(
            risk.worst_loss_per_unit, "risk.worst_loss_per_unit"
        ) != planned_per_unit
        or _bounded_fraction(
            risk.projected_total_loss, "risk.projected_total_loss"
        ) != planned_total_loss
    ):
        raise ValueError("risk plan disagrees with case or cost model")
    direction_pnl = (
        adverse_exit - entry
        if case.side.value == "LONG"
        else entry - adverse_exit
    )
    net_pnl = quantity * (
        direction_pnl - entry * entry_cost_rate - adverse_exit * exit_fee_rate
    )
    if planned_total_loss <= 0:
        raise ValueError("positive projected loss required for net R")
    net_r = net_pnl / planned_total_loss
    return (
        net_pnl.numerator, net_pnl.denominator,
        net_r.numerator, net_r.denominator,
    )


@dataclass(frozen=True, slots=True)
class ValidationObservation:
    proposal: AtomicResearchProposal
    case: ReplayCase
    outcome: ReplayOutcome
    risk: RiskPlan
    cost_model: CostModelSpec
    split_manifest: ValidationSplitManifest
    strategy_variant_hash: str
    segment: ValidationSegment
    net_pnl_numerator: int | None
    net_pnl_denominator: int | None
    net_r_numerator: int | None
    net_r_denominator: int | None
    observation_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not all(isinstance(value, expected) for value, expected in (
            (self.proposal, AtomicResearchProposal), (self.case, ReplayCase),
            (self.outcome, ReplayOutcome), (self.risk, RiskPlan),
            (self.cost_model, CostModelSpec),
            (self.split_manifest, ValidationSplitManifest),
        )):
            raise TypeError("typed validation inputs required")
        _digest(self.strategy_variant_hash, "strategy_variant_hash")
        if not isinstance(self.segment, ValidationSegment):
            raise TypeError("typed validation segment required")
        if (
            self.split_manifest.strategy_variant_hash != self.strategy_variant_hash
            or self.split_manifest.segment_for(self.case.decision_at) is not self.segment
        ):
            raise ValueError("validation segment must come from preregistered split")
        if any(getattr(value, "execution_authority", True) for value in (
            self.proposal, self.case, self.outcome, self.risk, self.cost_model,
        )):
            raise ValueError("validation evidence cannot carry execution authority")
        if not (
            self.proposal.assembly_id == self.case.proposal_assembly_id
            and self.case.case_id == self.outcome.case_id
            and self.proposal.risk_plan_id == self.risk.plan_id
            == self.case.risk_plan_id
            and self.risk.status is RiskPlanStatus.READY
            and self.risk.cost_hash == self.cost_model.cost_hash
            and self.proposal.decision.side is self.case.side is self.risk.side
            and self.proposal.decision.symbol == self.case.symbol == self.risk.symbol
            and self.proposal.contract_epoch_id == self.case.contract_epoch_id
            == self.risk.contract_epoch_id
        ):
            raise ValueError("validation identities disagree")
        exact_values = (
            self.net_pnl_numerator, self.net_pnl_denominator,
            self.net_r_numerator, self.net_r_denominator,
        )
        terminal = self.outcome.status in (
            ReplayOutcomeStatus.TARGET_HIT, ReplayOutcomeStatus.STOP_HIT,
        )
        if terminal:
            if any(type(value) is not int for value in exact_values):
                raise ValueError("scored observation requires exact net PnL and R")
            if self.net_pnl_denominator <= 0 or self.net_r_denominator <= 0:
                raise ValueError("exact denominators must be positive")
        elif any(value is not None for value in exact_values):
            raise ValueError("unscored observation cannot carry net performance")
        if exact_values != _exact_performance(
            self.case, self.outcome, self.risk, self.cost_model
        ):
            raise ValueError("net performance fields disagree with exact recomputation")
        object.__setattr__(self, "observation_id", "vob_" + canonical_sha256((
            self.proposal.assembly_id, self.case.case_id,
            self.outcome.outcome_id, self.risk.plan_id,
            self.cost_model.cost_hash, self.strategy_variant_hash,
            self.split_manifest.manifest_hash, self.segment, *exact_values,
        )).lower())


@dataclass(frozen=True, slots=True)
class ValidationPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    expected_segment: ValidationSegment
    strategy_variant_hash: str
    split_manifest_hash: str
    minimum_total_observations: int
    minimum_scored_observations: int
    minimum_distinct_theses: int
    maximum_censored_fraction: Decimal
    maximum_unresolved_fraction: Decimal
    maximum_open_fraction: Decimal
    minimum_mean_net_r: Decimal
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        _digest(self.strategy_variant_hash, "strategy_variant_hash")
        _digest(self.split_manifest_hash, "split_manifest_hash")
        _text(self.policy_id, "policy_id")
        _text(self.rule_version, "rule_version")
        if not isinstance(self.expected_segment, ValidationSegment):
            raise TypeError("typed expected validation segment required")
        for name in (
            "minimum_total_observations", "minimum_scored_observations",
            "minimum_distinct_theses",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.minimum_scored_observations > self.minimum_total_observations:
            raise ValueError("scored minimum cannot exceed total minimum")
        if self.minimum_distinct_theses > self.minimum_total_observations:
            raise ValueError("distinct thesis minimum cannot exceed total minimum")
        for name in (
            "maximum_censored_fraction", "maximum_unresolved_fraction",
            "maximum_open_fraction",
        ):
            value = _bounded_fraction(getattr(self, name), name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        _bounded_fraction(self.minimum_mean_net_r, "minimum_mean_net_r")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
            self.expected_segment, self.strategy_variant_hash,
            self.split_manifest_hash,
            self.minimum_total_observations,
            self.minimum_scored_observations, self.minimum_distinct_theses,
            self.maximum_censored_fraction, self.maximum_unresolved_fraction,
            self.maximum_open_fraction, self.minimum_mean_net_r,
        )).lower())


@dataclass(frozen=True, slots=True)
class ValidationMetrics:
    total: int
    scored: int
    target_hits: int
    stop_hits: int
    censored: int
    unresolved: int
    open_count: int
    distinct_theses: int
    total_net_r_numerator: int | None
    total_net_r_denominator: int | None
    mean_net_r_numerator: int | None
    mean_net_r_denominator: int | None
    win_rate_numerator: int | None
    win_rate_denominator: int | None
    metrics_hash: str = field(init=False)

    def __post_init__(self) -> None:
        counts = (
            self.total, self.scored, self.target_hits, self.stop_hits,
            self.censored, self.unresolved, self.open_count,
            self.distinct_theses,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("metrics counts must be nonnegative integers")
        if self.scored != self.target_hits + self.stop_hits:
            raise ValueError("scored count mismatch")
        if self.total != self.scored + self.censored + self.unresolved + self.open_count:
            raise ValueError("total count mismatch")
        ratios = (
            (self.total_net_r_numerator, self.total_net_r_denominator),
            (self.mean_net_r_numerator, self.mean_net_r_denominator),
            (self.win_rate_numerator, self.win_rate_denominator),
        )
        if self.scored:
            if any(type(n) is not int or type(d) is not int or d <= 0
                   for n, d in ratios):
                raise ValueError("scored metrics require exact rational values")
            if Fraction(
                self.mean_net_r_numerator, self.mean_net_r_denominator
            ) != Fraction(
                self.total_net_r_numerator, self.total_net_r_denominator
            ) / self.scored:
                raise ValueError("mean net R disagrees with total net R")
            if Fraction(
                self.win_rate_numerator, self.win_rate_denominator
            ) != Fraction(self.target_hits, self.scored):
                raise ValueError("win rate disagrees with scored outcomes")
        elif any(value is not None for pair in ratios for value in pair):
            raise ValueError("unscored metrics cannot claim performance")
        object.__setattr__(self, "metrics_hash", canonical_sha256((
            *counts, *tuple(value for pair in ratios for value in pair),
        )).lower())


@dataclass(frozen=True, slots=True)
class ValidationResult:
    verdict: ValidationVerdict
    reason: ValidationReason | None
    metrics: ValidationMetrics
    policy_variant_hash: str
    observation_ids: tuple[str, ...]
    result_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ValidationVerdict):
            raise TypeError("typed validation verdict required")
        if self.verdict is ValidationVerdict.QUALIFIED:
            if self.reason is not None:
                raise ValueError("qualified validation cannot carry rejection")
        elif not isinstance(self.reason, ValidationReason):
            raise ValueError("non-qualified validation requires typed reason")
        if not isinstance(self.metrics, ValidationMetrics):
            raise TypeError("typed validation metrics required")
        _digest(self.policy_variant_hash, "policy_variant_hash")
        if (
            not isinstance(self.observation_ids, tuple)
            or len(set(self.observation_ids)) != len(self.observation_ids)
            or any(not isinstance(item, str) or not item.strip()
                   for item in self.observation_ids)
        ):
            raise ValueError("unique validation observation IDs required")
        object.__setattr__(self, "result_id", "val_" + canonical_sha256((
            self.verdict, self.reason, self.metrics.metrics_hash,
            self.policy_variant_hash, self.observation_ids,
        )).lower())


def build_validation_observation(
    *,
    proposal: AtomicResearchProposal,
    case: ReplayCase,
    outcome: ReplayOutcome,
    risk: RiskPlan,
    cost_model: CostModelSpec,
    split_manifest: ValidationSplitManifest,
) -> ValidationObservation:
    if not all(isinstance(value, expected) for value, expected in (
        (proposal, AtomicResearchProposal), (case, ReplayCase),
        (outcome, ReplayOutcome), (risk, RiskPlan),
        (cost_model, CostModelSpec),
        (split_manifest, ValidationSplitManifest),
    )):
        raise TypeError("typed validation observation inputs required")
    strategy_variant_hash = split_manifest.strategy_variant_hash
    segment = split_manifest.segment_for(case.decision_at)
    exact_values = _exact_performance(case, outcome, risk, cost_model)
    return ValidationObservation(
        proposal, case, outcome, risk, cost_model, split_manifest,
        strategy_variant_hash, segment, *exact_values,
    )


def _metrics(observations: tuple[ValidationObservation, ...]) -> ValidationMetrics:
    target_hits = sum(item.outcome.status is ReplayOutcomeStatus.TARGET_HIT
                      for item in observations)
    stop_hits = sum(item.outcome.status is ReplayOutcomeStatus.STOP_HIT
                    for item in observations)
    censored = sum(item.outcome.status is ReplayOutcomeStatus.CENSORED
                   for item in observations)
    unresolved = sum(item.outcome.status is ReplayOutcomeStatus.UNRESOLVED
                     for item in observations)
    open_count = sum(item.outcome.status is ReplayOutcomeStatus.OPEN
                     for item in observations)
    scored = target_hits + stop_hits
    total_r = mean_r = win_rate = None
    if scored:
        total_r = sum((Fraction(item.net_r_numerator, item.net_r_denominator)
                       for item in observations
                       if item.net_r_numerator is not None), Fraction(0))
        mean_r = total_r / scored
        win_rate = Fraction(target_hits, scored)
    return ValidationMetrics(
        len(observations), scored, target_hits, stop_hits, censored,
        unresolved, open_count,
        len({item.proposal.decision.thesis_id for item in observations}),
        total_r.numerator if total_r is not None else None,
        total_r.denominator if total_r is not None else None,
        mean_r.numerator if mean_r is not None else None,
        mean_r.denominator if mean_r is not None else None,
        win_rate.numerator if win_rate is not None else None,
        win_rate.denominator if win_rate is not None else None,
    )


def evaluate_validation_cohort(
    *,
    observations: tuple[ValidationObservation, ...],
    policy: ValidationPolicySpec,
) -> ValidationResult:
    if not isinstance(observations, tuple) or any(
        type(item) is not ValidationObservation for item in observations
    ):
        raise TypeError("typed immutable validation observations required")
    if not isinstance(policy, ValidationPolicySpec):
        raise TypeError("typed validation policy required")
    metrics = _metrics(observations)
    ids = tuple(item.observation_id for item in observations)
    if len(set(ids)) != len(ids) or len({
        item.proposal.decision.attempt_id for item in observations
    }) != len(observations):
        return ValidationResult(
            ValidationVerdict.UNRESOLVED, ValidationReason.DUPLICATE_OBSERVATION,
            metrics, policy.variant_hash, tuple(dict.fromkeys(ids)),
        )
    if any(
        item.execution_authority
        or item.segment is not policy.expected_segment
        or item.strategy_variant_hash != policy.strategy_variant_hash
        or item.split_manifest.manifest_hash != policy.split_manifest_hash
        for item in observations
    ):
        return ValidationResult(
            ValidationVerdict.UNRESOLVED, ValidationReason.MIXED_COHORT,
            metrics, policy.variant_hash, ids,
        )
    checks = (
        (metrics.total < policy.minimum_total_observations,
         ValidationReason.SAMPLE_TOO_SMALL),
        (metrics.scored < policy.minimum_scored_observations,
         ValidationReason.SCORED_SAMPLE_TOO_SMALL),
        (metrics.distinct_theses < policy.minimum_distinct_theses,
         ValidationReason.DISTINCT_THESIS_TOO_SMALL),
    )
    for failed, reason in checks:
        if failed:
            return ValidationResult(
                ValidationVerdict.INSUFFICIENT_EVIDENCE, reason, metrics,
                policy.variant_hash, ids,
            )
    total = metrics.total
    coverage_checks = (
        (Fraction(metrics.censored, total), policy.maximum_censored_fraction,
         ValidationReason.CENSORED_RATE_TOO_HIGH),
        (Fraction(metrics.unresolved, total), policy.maximum_unresolved_fraction,
         ValidationReason.UNRESOLVED_RATE_TOO_HIGH),
        (Fraction(metrics.open_count, total), policy.maximum_open_fraction,
         ValidationReason.OPEN_RATE_TOO_HIGH),
    )
    for actual, maximum, reason in coverage_checks:
        if actual > _bounded_fraction(maximum, reason.value):
            return ValidationResult(
                ValidationVerdict.INSUFFICIENT_EVIDENCE, reason, metrics,
                policy.variant_hash, ids,
            )
    mean_net_r = Fraction(metrics.mean_net_r_numerator, metrics.mean_net_r_denominator)
    if mean_net_r < _bounded_fraction(
        policy.minimum_mean_net_r, "minimum_mean_net_r"
    ):
        return ValidationResult(
            ValidationVerdict.REJECTED,
            ValidationReason.MEAN_NET_R_BELOW_MINIMUM,
            metrics, policy.variant_hash, ids,
        )
    return ValidationResult(
        ValidationVerdict.QUALIFIED, None, metrics, policy.variant_hash, ids,
    )
