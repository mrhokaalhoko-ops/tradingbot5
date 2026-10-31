"""Pure, fail-closed Strategy V2 pre-trade risk sizing contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from .causality import require_utc
from .enums import Side
from .events import canonical_sha256
from .models import ThesisId


_MAX_INPUT_DIGITS = 64
_MAX_INPUT_EXPONENT = 64
_MAX_RESULT_DIGITS = 256
_MAX_RESULT_SCALE = 128


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
    if len(parts.digits) > _MAX_INPUT_DIGITS or abs(parts.exponent) > _MAX_INPUT_EXPONENT:
        raise ValueError("input outside risk numeric domain")
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
        raise ValueError("nonterminating decimal outside risk policy")
    scale = max(twos, fives)
    if scale > _MAX_RESULT_SCALE:
        raise ValueError("result scale outside risk numeric domain")
    coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    if len(str(coefficient)) > _MAX_RESULT_DIGITS:
        raise ValueError("result digits outside risk numeric domain")
    return Decimal((int(value < 0), tuple(map(int, str(coefficient))), -scale))


def _floor_to_step(value: Fraction, step: Fraction) -> Fraction:
    ratio = value / step
    return (ratio.numerator // ratio.denominator) * step


def _floor_ratio(value: Fraction, places: int) -> Decimal:
    scale = 10 ** places
    scaled = value * scale
    return _exact_decimal(Fraction(scaled.numerator // scaled.denominator, scale))


class MetadataProvenance(str, Enum):
    EXACT_EFFECTIVE_INTERVAL = "EXACT_EFFECTIVE_INTERVAL"
    BOUNDED_TRANSITION = "BOUNDED_TRANSITION"
    CURRENT_SNAPSHOT_ONLY = "CURRENT_SNAPSHOT_ONLY"


class QuantityFilterKind(str, Enum):
    LOT_SIZE = "LOT_SIZE"
    MARKET_LOT_SIZE = "MARKET_LOT_SIZE"


class RiskPlanStatus(str, Enum):
    READY = "READY"
    SKIPPED = "SKIPPED"
    UNRESOLVED = "UNRESOLVED"


class RiskPlanReason(str, Enum):
    FILTER_EVIDENCE_FUTURE = "FILTER_EVIDENCE_FUTURE"
    FILTER_INTERVAL_MISMATCH = "FILTER_INTERVAL_MISMATCH"
    FILTER_KIND_MISMATCH = "FILTER_KIND_MISMATCH"
    METADATA_PROVENANCE_INSUFFICIENT = "METADATA_PROVENANCE_INSUFFICIENT"
    INVALID_STOP_GEOMETRY = "INVALID_STOP_GEOMETRY"
    NUMERIC_UNCOMPUTABLE = "NUMERIC_UNCOMPUTABLE"
    ZERO_SIZED = "ZERO_SIZED"
    BELOW_MIN_QUANTITY = "BELOW_MIN_QUANTITY"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    TARGET_UTILIZATION_TOO_LOW = "TARGET_UTILIZATION_TOO_LOW"
    HARD_RISK_CAP_EXCEEDED = "HARD_RISK_CAP_EXCEEDED"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"


@dataclass(frozen=True, slots=True)
class VenueFilterSnapshot:
    symbol: str
    contract_epoch_id: str
    snapshot_id: str
    source_hash: str
    quantity_filter_kind: QuantityFilterKind
    quantity_step: Decimal
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    minimum_notional: Decimal
    maximum_notional: Decimal | None
    effective_from: datetime
    effective_until: datetime | None
    confirmed_at: datetime
    available_at: datetime
    provenance: MetadataProvenance
    filter_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("symbol", "contract_epoch_id", "snapshot_id"):
            _text(getattr(self, name), name)
        _digest(self.source_hash, "source_hash")
        if not isinstance(self.quantity_filter_kind, QuantityFilterKind):
            raise TypeError("typed quantity filter kind required")
        for name in ("quantity_step", "minimum_quantity", "maximum_quantity"):
            _decimal(getattr(self, name), name)
        _decimal(self.minimum_notional, "minimum_notional", allow_zero=True)
        if self.maximum_notional is not None:
            _decimal(self.maximum_notional, "maximum_notional")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("minimum quantity exceeds maximum quantity")
        if (self.maximum_notional is not None
                and self.minimum_notional > self.maximum_notional):
            raise ValueError("minimum notional exceeds maximum notional")
        for name in ("effective_from", "confirmed_at", "available_at"):
            require_utc(getattr(self, name), name)
        if self.effective_until is not None:
            require_utc(self.effective_until, "effective_until")
            if self.effective_until <= self.effective_from:
                raise ValueError("invalid effective interval")
        if self.confirmed_at > self.available_at:
            raise ValueError("filter cannot be available before confirmation")
        if not isinstance(self.provenance, MetadataProvenance):
            raise TypeError("typed metadata provenance required")
        object.__setattr__(self, "filter_id", "flt_" + canonical_sha256((
            self.symbol, self.contract_epoch_id, self.snapshot_id,
            self.source_hash, self.quantity_filter_kind,
            self.quantity_step, self.minimum_quantity,
            self.maximum_quantity, self.minimum_notional,
            self.maximum_notional, self.effective_from, self.effective_until,
            self.confirmed_at, self.available_at, self.provenance,
        )).lower())


@dataclass(frozen=True, slots=True)
class CostModelSpec:
    policy_id: str
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    entry_slippage_rate: Decimal
    stop_slippage_rate: Decimal
    funding_rate_abs: Decimal
    uncertainty_rate: Decimal
    cost_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        for name in (
            "entry_fee_rate", "exit_fee_rate", "entry_slippage_rate",
            "stop_slippage_rate", "funding_rate_abs", "uncertainty_rate",
        ):
            value = getattr(self, name)
            _decimal(value, name, allow_zero=True)
            if value >= 1:
                raise ValueError(f"{name} must be below one")
        object.__setattr__(self, "cost_hash", canonical_sha256((
            self.policy_id, self.entry_fee_rate, self.exit_fee_rate,
            self.entry_slippage_rate, self.stop_slippage_rate,
            self.funding_rate_abs, self.uncertainty_rate,
        )).lower())


@dataclass(frozen=True, slots=True)
class RiskPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    target_equity_risk_fraction: Decimal
    hard_max_equity_risk_fraction: Decimal
    target_initial_margin_fraction: Decimal
    minimum_target_utilization: Decimal
    leverage: Decimal
    utilization_decimal_places: int
    numeric_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        for name in ("policy_id", "rule_version", "numeric_policy_id"):
            _text(getattr(self, name), name)
        for name in (
            "target_equity_risk_fraction", "hard_max_equity_risk_fraction",
            "target_initial_margin_fraction", "minimum_target_utilization",
        ):
            value = getattr(self, name)
            _decimal(value, name)
            if value > 1:
                raise ValueError(f"{name} cannot exceed one")
        _decimal(self.leverage, "leverage")
        if self.target_equity_risk_fraction >= self.hard_max_equity_risk_fraction:
            raise ValueError("target risk must be below hard risk cap")
        if type(self.utilization_decimal_places) is not int or not 0 <= self.utilization_decimal_places <= 12:
            raise ValueError("utilization_decimal_places must be in [0,12]")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
            self.target_equity_risk_fraction,
            self.hard_max_equity_risk_fraction,
            self.target_initial_margin_fraction,
            self.minimum_target_utilization, self.leverage,
            self.utilization_decimal_places, self.numeric_policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class RiskPlan:
    status: RiskPlanStatus
    reason: RiskPlanReason | None
    thesis_id: ThesisId
    side: Side
    symbol: str
    contract_epoch_id: str
    entry_price: Decimal
    hard_stop_price: Decimal
    account_equity: Decimal
    quantity: Decimal | None
    notional: Decimal | None
    target_risk_budget: Decimal | None
    hard_risk_cap: Decimal | None
    worst_loss_per_unit: Decimal | None
    projected_total_loss: Decimal | None
    initial_margin: Decimal | None
    target_utilization: Decimal | None
    filter_id: str
    cost_hash: str
    policy_variant_hash: str
    decision_at: datetime
    plan_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, RiskPlanStatus) or type(self.thesis_id) is not ThesisId:
            raise TypeError("typed risk status and thesis ID required")
        if not isinstance(self.side, Side):
            raise TypeError("typed side required")
        for name in ("symbol", "contract_epoch_id", "filter_id"):
            _text(getattr(self, name), name)
        for name in ("cost_hash", "policy_variant_hash"):
            _digest(getattr(self, name), name)
        require_utc(self.decision_at, "decision_at")
        _decimal(self.account_equity, "account_equity")
        if self.status is RiskPlanStatus.READY:
            if self.reason is not None or any(value is None for value in (
                self.quantity, self.notional, self.target_risk_budget,
                self.hard_risk_cap, self.worst_loss_per_unit,
                self.projected_total_loss, self.initial_margin,
                self.target_utilization,
            )):
                raise ValueError("ready risk plan requires complete values")
            if self.projected_total_loss > self.hard_risk_cap:
                raise ValueError("ready plan exceeds hard risk cap")
        elif not isinstance(self.reason, RiskPlanReason):
            raise ValueError("non-ready risk plan requires typed reason")
        object.__setattr__(self, "plan_id", "rsk_" + canonical_sha256((
            self.status, self.reason, self.thesis_id, self.side, self.symbol,
            self.contract_epoch_id, self.entry_price, self.hard_stop_price,
            self.account_equity,
            self.quantity, self.notional, self.target_risk_budget,
            self.hard_risk_cap, self.worst_loss_per_unit,
            self.projected_total_loss, self.initial_margin,
            self.target_utilization, self.filter_id, self.cost_hash,
            self.policy_variant_hash,
        )).lower())


def _plan(
    status: RiskPlanStatus,
    reason: RiskPlanReason | None,
    *, thesis_id: ThesisId,
    side: Side,
    symbol: str,
    filters: VenueFilterSnapshot,
    entry_price: Decimal,
    hard_stop_price: Decimal,
    account_equity: Decimal,
    policy: RiskPolicySpec,
    costs: CostModelSpec,
    decision_at: datetime,
    quantity: Decimal | None = None,
    notional: Decimal | None = None,
    target_budget: Decimal | None = None,
    hard_cap: Decimal | None = None,
    per_unit: Decimal | None = None,
    projected_loss: Decimal | None = None,
    initial_margin: Decimal | None = None,
    utilization: Decimal | None = None,
) -> RiskPlan:
    return RiskPlan(
        status, reason, thesis_id, side, symbol, filters.contract_epoch_id,
        entry_price, hard_stop_price, account_equity, quantity, notional, target_budget,
        hard_cap, per_unit, projected_loss, initial_margin, utilization,
        filters.filter_id, costs.cost_hash, policy.variant_hash, decision_at,
    )


def build_risk_plan(
    *,
    thesis_id: ThesisId,
    side: Side,
    symbol: str,
    contract_epoch_id: str,
    entry_price: Decimal,
    hard_stop_price: Decimal,
    account_equity: Decimal,
    available_initial_margin: Decimal,
    quantity_filter_kind: QuantityFilterKind,
    filters: VenueFilterSnapshot,
    costs: CostModelSpec,
    policy: RiskPolicySpec,
    decision_at: datetime,
) -> RiskPlan:
    """Size from final hard stop and costs; never mutates the stop or raises risk."""

    require_utc(decision_at, "decision_at")
    if type(thesis_id) is not ThesisId or not isinstance(side, Side):
        raise TypeError("typed thesis ID and side required")
    if not isinstance(quantity_filter_kind, QuantityFilterKind):
        raise TypeError("typed quantity filter kind required")
    for name, value in (("symbol", symbol), ("contract_epoch_id", contract_epoch_id)):
        _text(value, name)
    if not isinstance(filters, VenueFilterSnapshot) or not isinstance(costs, CostModelSpec) or not isinstance(policy, RiskPolicySpec):
        raise TypeError("typed filters, costs, and risk policy required")
    for name, value in (
        ("entry_price", entry_price), ("hard_stop_price", hard_stop_price),
        ("account_equity", account_equity),
        ("available_initial_margin", available_initial_margin),
    ):
        _decimal(value, name, allow_zero=name == "available_initial_margin")
    common = dict(
        thesis_id=thesis_id, side=side, symbol=symbol, filters=filters,
        entry_price=entry_price, hard_stop_price=hard_stop_price,
        account_equity=account_equity,
        policy=policy, costs=costs, decision_at=decision_at,
    )
    if filters.symbol != symbol or filters.contract_epoch_id != contract_epoch_id:
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.FILTER_INTERVAL_MISMATCH, **common)
    if filters.quantity_filter_kind is not quantity_filter_kind:
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.FILTER_KIND_MISMATCH, **common)
    if filters.available_at > decision_at:
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.FILTER_EVIDENCE_FUTURE, **common)
    if filters.provenance is not MetadataProvenance.EXACT_EFFECTIVE_INTERVAL:
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.METADATA_PROVENANCE_INSUFFICIENT, **common)
    if not (filters.effective_from <= decision_at
            and (filters.effective_until is None or decision_at < filters.effective_until)):
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.FILTER_INTERVAL_MISMATCH, **common)
    if ((side is Side.LONG and hard_stop_price >= entry_price)
            or (side is Side.SHORT and hard_stop_price <= entry_price)):
        return _plan(RiskPlanStatus.SKIPPED, RiskPlanReason.INVALID_STOP_GEOMETRY, **common)
    try:
        entry = _fraction(entry_price)
        stop = _fraction(hard_stop_price)
        equity = _fraction(account_equity)
        available_margin = _fraction(available_initial_margin)
        target_budget_f = equity * _fraction(policy.target_equity_risk_fraction)
        hard_cap_f = equity * _fraction(policy.hard_max_equity_risk_fraction)
        stop_slippage = stop * _fraction(costs.stop_slippage_rate)
        adverse_stop = stop - stop_slippage if side is Side.LONG else stop + stop_slippage
        price_loss = entry - adverse_stop if side is Side.LONG else adverse_stop - entry
        per_unit_f = (
            price_loss
            + entry * (_fraction(costs.entry_fee_rate)
                       + _fraction(costs.entry_slippage_rate)
                       + _fraction(costs.funding_rate_abs)
                       + _fraction(costs.uncertainty_rate))
            + adverse_stop * _fraction(costs.exit_fee_rate)
        )
        if per_unit_f <= 0:
            return _plan(RiskPlanStatus.SKIPPED, RiskPlanReason.INVALID_STOP_GEOMETRY, **common)
        step = _fraction(filters.quantity_step)
        risk_quantity = target_budget_f / per_unit_f
        policy_margin_cap = equity * _fraction(policy.target_initial_margin_fraction)
        usable_margin = min(policy_margin_cap, available_margin)
        margin_quantity = usable_margin * _fraction(policy.leverage) / entry
        quantity_f = min(risk_quantity, margin_quantity, _fraction(filters.maximum_quantity))
        if filters.maximum_notional is not None:
            quantity_f = min(quantity_f, _fraction(filters.maximum_notional) / entry)
        quantity_f = _floor_to_step(quantity_f, step)
        target_budget = _exact_decimal(target_budget_f)
        hard_cap = _exact_decimal(hard_cap_f)
        per_unit = _exact_decimal(per_unit_f)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.NUMERIC_UNCOMPUTABLE, **common)
    if quantity_f <= 0:
        reason = (RiskPlanReason.INSUFFICIENT_MARGIN if available_initial_margin == 0
                  else RiskPlanReason.ZERO_SIZED)
        return _plan(
            RiskPlanStatus.SKIPPED, reason, target_budget=target_budget,
            hard_cap=hard_cap, per_unit=per_unit, **common,
        )
    try:
        quantity = _exact_decimal(quantity_f)
        notional_f = quantity_f * entry
        projected_loss_f = quantity_f * per_unit_f
        initial_margin_f = notional_f / _fraction(policy.leverage)
        utilization_f = projected_loss_f / target_budget_f
        notional = _exact_decimal(notional_f)
        projected_loss = _exact_decimal(projected_loss_f)
        initial_margin = _exact_decimal(initial_margin_f)
        utilization = _floor_ratio(utilization_f, policy.utilization_decimal_places)
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        return _plan(RiskPlanStatus.UNRESOLVED, RiskPlanReason.NUMERIC_UNCOMPUTABLE, **common)
    values = dict(
        quantity=quantity, notional=notional, target_budget=target_budget,
        hard_cap=hard_cap, per_unit=per_unit,
        projected_loss=projected_loss, initial_margin=initial_margin,
        utilization=utilization,
    )
    if quantity < filters.minimum_quantity:
        return _plan(RiskPlanStatus.SKIPPED, RiskPlanReason.BELOW_MIN_QUANTITY, **values, **common)
    if notional < filters.minimum_notional:
        return _plan(RiskPlanStatus.SKIPPED, RiskPlanReason.BELOW_MIN_NOTIONAL, **values, **common)
    if projected_loss_f > hard_cap_f:
        return _plan(RiskPlanStatus.SKIPPED, RiskPlanReason.HARD_RISK_CAP_EXCEEDED, **values, **common)
    if utilization_f < _fraction(policy.minimum_target_utilization):
        return _plan(RiskPlanStatus.SKIPPED, RiskPlanReason.TARGET_UTILIZATION_TOO_LOW, **values, **common)
    return _plan(RiskPlanStatus.READY, None, **values, **common)
