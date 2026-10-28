"""R2A offline threshold evidence. None of these contracts qualifies a BOS."""

from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal
from enum import Enum
from math import gcd

from .causality import require_utc
from .events import canonical_sha256
from .structure_models import BreakDirection


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} required")


def _digest(value: str, name: str, length: int = 64) -> None:
    if not isinstance(value, str) or len(value) != length or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError(f"{name} must be a {length}-character hex digest")


def _number(value: Decimal, name: str, *, positive: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or (
        value <= 0 if positive else value < 0
    ):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} Decimal")


def _integer_tick_multiple(floor: Decimal, size: Decimal) -> bool:
    """Test a decimal ratio exactly without expanding extreme exponents."""
    a, b = floor.as_tuple(), size.as_tuple()
    if len(a.digits) > 512 or len(b.digits) > 512:
        return False
    numerator = int("".join(map(str, a.digits)))
    denominator = int("".join(map(str, b.digits)))
    exponent_gap = a.exponent - b.exponent
    if exponent_gap < 0:
        scale = -exponent_gap
        return scale <= len(a.digits) and numerator % (denominator * 10 ** scale) == 0
    needed = denominator // gcd(numerator, denominator)
    twos = fives = 0
    while needed % 2 == 0:
        needed //= 2
        twos += 1
    while needed % 5 == 0:
        needed //= 5
        fives += 1
    return needed == 1 and twos <= exponent_gap and fives <= exponent_gap


class ATRSourcePolicy(str, Enum):
    PRE_BREAK_CLOSED = "ATR_PRE_BREAK_CLOSED"
    BREAK_CLOSE = "ATR_BREAK_CLOSE"


class GridPolicy(str, Enum):
    OUTWARD_TICK = "OUTWARD_TICK"


class NumericPolicy(str, Enum):
    EXACT_FRACTION_BOUNDED_V1 = "EXACT_FRACTION_BOUNDED_V1"


class ThresholdStatus(str, Enum):
    THRESHOLD_PASSED = "THRESHOLD_PASSED"
    THRESHOLD_NOT_PASSED = "THRESHOLD_NOT_PASSED"
    UNRESOLVED = "UNRESOLVED"


class EvidenceResolutionReason(str, Enum):
    INVALID_EVIDENCE_TYPE = "INVALID_EVIDENCE_TYPE"
    INVALID_EVIDENCE_STRUCTURE = "INVALID_EVIDENCE_STRUCTURE"
    MISSING_SPEC = "MISSING_SPEC"
    MISSING_PIVOT = "MISSING_PIVOT"
    MISSING_ATR = "MISSING_ATR"
    MISSING_TICK = "MISSING_TICK"
    MISSING_SOURCE_BARS = "MISSING_SOURCE_BARS"
    DUPLICATE_EVIDENCE = "DUPLICATE_EVIDENCE"
    INVALID_BAR_SEQUENCE = "INVALID_BAR_SEQUENCE"
    MIXED_CONTEXT = "MIXED_CONTEXT"
    SPEC_MISMATCH = "SPEC_MISMATCH"
    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    PIVOT_SOURCE_MISMATCH = "PIVOT_SOURCE_MISMATCH"
    ATR_SOURCE_MISMATCH = "ATR_SOURCE_MISMATCH"
    ATR_WINDOW_POLICY_MISMATCH = "ATR_WINDOW_POLICY_MISMATCH"
    ATR_VARIANT_MISMATCH = "ATR_VARIANT_MISMATCH"
    TICK_NOT_EFFECTIVE = "TICK_NOT_EFFECTIVE"
    OFF_GRID_PRICE = "OFF_GRID_PRICE"
    NUMERIC_THRESHOLD_UNCOMPUTABLE = "NUMERIC_THRESHOLD_UNCOMPUTABLE"
    NONPOSITIVE_THRESHOLD = "NONPOSITIVE_THRESHOLD"
    NO_CLOSE_CROSS = "NO_CLOSE_CROSS"
    STRUCTURE_ROLE_NOT_PROVEN = "STRUCTURE_ROLE_NOT_PROVEN"
    THESIS_POLICY_NOT_DEFINED = "THESIS_POLICY_NOT_DEFINED"


@dataclass(frozen=True, slots=True)
class BOSInputSpec:
    """Explicit frozen research variant; b and ATR source have no defaults."""

    source_commit: str
    structure_spec_hash: str
    rule_version: str
    variant_id: str
    threshold_policy_id: str
    multiplier_b: Decimal
    atr_source_policy: ATRSourcePolicy
    atr_method_id: str
    atr_period: int
    atr_policy_id: str
    grid_policy: GridPolicy
    numeric_policy: NumericPolicy
    price_basis: str
    symbol: str
    timeframe: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        _digest(self.structure_spec_hash, "structure_spec_hash")
        for name in ("rule_version", "variant_id", "threshold_policy_id",
                     "atr_method_id", "atr_policy_id",
                     "price_basis", "symbol", "timeframe"):
            _text(getattr(self, name), name)
        _number(self.multiplier_b, "multiplier_b", positive=False)
        if type(self.atr_period) is not int or self.atr_period < 1:
            raise ValueError("explicit positive atr_period required")
        if not isinstance(self.atr_source_policy, ATRSourcePolicy):
            raise TypeError("explicit ATRSourcePolicy required")
        if self.grid_policy is not GridPolicy.OUTWARD_TICK:
            raise TypeError("explicit OUTWARD_TICK grid policy required")
        if self.numeric_policy is not NumericPolicy.EXACT_FRACTION_BOUNDED_V1:
            raise TypeError("explicit EXACT_FRACTION_BOUNDED_V1 numeric policy required")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.structure_spec_hash, self.rule_version,
            self.variant_id, self.threshold_policy_id, self.multiplier_b,
            self.atr_source_policy, self.atr_method_id, self.atr_period,
            self.atr_policy_id, self.grid_policy, self.numeric_policy, self.price_basis,
            self.symbol, self.timeframe,
        )))


@dataclass(frozen=True, slots=True)
class VolatilityObservation:
    atr: Decimal
    period: int
    method: str
    symbol: str
    timeframe: str
    price_basis: str
    source_bars_hash: str
    window_end_at: datetime
    confirmed_at: datetime
    available_at: datetime
    policy_id: str
    evidence_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _number(self.atr, "atr")
        if type(self.period) is not int or self.period < 1:
            raise ValueError("explicit positive ATR period required")
        for name in ("method", "symbol", "timeframe", "price_basis", "policy_id"):
            _text(getattr(self, name), name)
        _digest(self.source_bars_hash, "source_bars_hash")
        for name in ("window_end_at", "confirmed_at", "available_at"):
            require_utc(getattr(self, name), name)
        if not self.window_end_at <= self.confirmed_at <= self.available_at:
            raise ValueError("ATR window_end_at <= confirmed_at <= available_at required")
        object.__setattr__(self, "evidence_id", "atr_" + canonical_sha256((
            self.atr, self.period, self.method, self.symbol, self.timeframe,
            self.price_basis, self.source_bars_hash, self.window_end_at,
            self.confirmed_at, self.available_at, self.policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class InstrumentTickObservation:
    symbol: str
    price_basis: str
    tick_size: Decimal
    tick_floor: Decimal
    metadata_source_id: str
    effective_at: datetime
    available_at: datetime
    metadata_fields: tuple[tuple[str, str], ...]
    metadata_hash: str
    evidence_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("symbol", "price_basis", "metadata_source_id"):
            _text(getattr(self, name), name)
        _number(self.tick_size, "tick_size")
        _number(self.tick_floor, "tick_floor")
        if not _integer_tick_multiple(self.tick_floor, self.tick_size):
            raise ValueError("tick_floor must be an integer multiple of tick_size")
        require_utc(self.effective_at, "effective_at")
        require_utc(self.available_at, "available_at")
        if self.effective_at > self.available_at:
            raise ValueError("tick effective_at must not exceed available_at")
        if not isinstance(self.metadata_fields, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 or
            not all(isinstance(v, str) and v.strip() for v in pair)
            for pair in self.metadata_fields
        ):
            raise ValueError("immutable nonempty metadata field pairs required")
        keys = [pair[0] for pair in self.metadata_fields]
        if keys != sorted(set(keys)):
            raise ValueError("metadata fields must be unique and sorted")
        raw = dict(self.metadata_fields)
        if any(raw.get(key) != value for key, value in (
            ("symbol", self.symbol), ("price_basis", self.price_basis),
            ("tick_size", str(self.tick_size)), ("tick_floor", str(self.tick_floor)),
        )):
            raise ValueError("tick metadata must bind symbol, basis, and tick values")
        _digest(self.metadata_hash, "metadata_hash")
        if self.metadata_hash != canonical_sha256(self.metadata_fields):
            raise ValueError("metadata_hash disagrees with metadata_fields")
        object.__setattr__(self, "evidence_id", "tick_" + canonical_sha256((
            self.symbol, self.price_basis, self.tick_size, self.tick_floor,
            self.metadata_source_id, self.effective_at, self.available_at,
            self.metadata_fields, self.metadata_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class CausalQualificationResolution:
    """R2A cannot promote threshold evidence into a qualified structure event."""

    status: ThresholdStatus
    reason: EvidenceResolutionReason
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.status is not ThresholdStatus.UNRESOLVED or self.reason not in (
            EvidenceResolutionReason.STRUCTURE_ROLE_NOT_PROVEN,
            EvidenceResolutionReason.THESIS_POLICY_NOT_DEFINED,
        ):
            raise ValueError("R2A qualification must remain unresolved")


@dataclass(frozen=True, slots=True)
class CloseBreakCandidate:
    """Auditable numeric observation, never a BOS or a trading proposal."""

    status: ThresholdStatus
    reason: EvidenceResolutionReason | None
    direction: BreakDirection | None
    symbol: str | None
    timeframe: str | None
    previous_bar_id: str | None
    break_bar_id: str | None
    broken_pivot_id: str | None
    atr_evidence_id: str | None
    tick_evidence_id: str | None
    threshold_delta: Decimal | None
    threshold_price: Decimal | None
    pivot_source_window_hash: str | None
    break_support_hash: str | None
    variant_hash: str | None
    threshold_policy_id: str | None
    rule_version: str | None
    source_commit: str | None
    atr_source_policy: ATRSourcePolicy | None
    atr_method_id: str | None
    atr_period: int | None
    atr_policy_id: str | None
    price_basis: str | None
    source_at: datetime | None
    confirmed_at: datetime | None
    available_at: datetime | None
    decision_at: datetime
    parent_evidence_ids: tuple[str, ...]
    evidence_hash: str
    qualification: CausalQualificationResolution
    event_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        require_utc(self.decision_at, "decision_at")
        if not isinstance(self.status, ThresholdStatus) or not isinstance(
            self.qualification, CausalQualificationResolution
        ):
            raise TypeError("typed threshold and qualification status required")
        if self.status is ThresholdStatus.UNRESOLVED and self.reason is None:
            raise ValueError("unresolved candidate requires typed reason")
        if self.status is ThresholdStatus.THRESHOLD_PASSED and self.reason is not None:
            raise ValueError("passed threshold cannot carry a rejection reason")
        if self.status is ThresholdStatus.THRESHOLD_NOT_PASSED and self.reason is not EvidenceResolutionReason.NO_CLOSE_CROSS:
            raise ValueError("failed close cross needs NO_CLOSE_CROSS reason")
        if self.reason is not None and not isinstance(self.reason, EvidenceResolutionReason):
            raise TypeError("typed evidence reason required")
        if self.direction is not None and not isinstance(self.direction, BreakDirection):
            raise TypeError("direction must be BreakDirection")
        if self.atr_source_policy is not None and not isinstance(
            self.atr_source_policy, ATRSourcePolicy
        ):
            raise TypeError("atr_source_policy must be ATRSourcePolicy")
        for name in ("symbol", "timeframe", "previous_bar_id", "break_bar_id",
                     "broken_pivot_id", "atr_evidence_id", "tick_evidence_id",
                     "threshold_policy_id", "rule_version", "atr_method_id",
                     "atr_policy_id", "price_basis"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        for name in ("pivot_source_window_hash", "break_support_hash", "variant_hash"):
            value = getattr(self, name)
            if value is not None:
                _digest(value, name)
        if self.source_commit is not None:
            _digest(self.source_commit, "source_commit", 40)
        if self.atr_period is not None and (type(self.atr_period) is not int or self.atr_period < 1):
            raise ValueError("positive atr_period required")
        if self.threshold_delta is not None:
            _number(self.threshold_delta, "threshold_delta")
        if self.threshold_price is not None:
            _number(self.threshold_price, "threshold_price")
        for name in ("source_at", "confirmed_at", "available_at"):
            value = getattr(self, name)
            if value is not None:
                require_utc(value, name)
        if self.available_at is not None and self.available_at > self.decision_at:
            raise ValueError("candidate availability cannot be after decision")
        if not isinstance(self.parent_evidence_ids, tuple) or any(
            not isinstance(value, str) or not value.strip() for value in self.parent_evidence_ids
        ) or len(set(self.parent_evidence_ids)) != len(self.parent_evidence_ids):
            raise ValueError("unique parent evidence IDs required")
        _digest(self.evidence_hash, "evidence_hash")
        if self.status is not ThresholdStatus.UNRESOLVED:
            if not self.parent_evidence_ids:
                raise ValueError("resolved candidate requires parent evidence")
            for name in ("direction", "symbol", "timeframe", "previous_bar_id",
                         "break_bar_id", "broken_pivot_id", "atr_evidence_id",
                         "tick_evidence_id", "threshold_delta", "threshold_price",
                         "pivot_source_window_hash", "break_support_hash", "variant_hash",
                         "threshold_policy_id", "rule_version", "source_commit",
                         "atr_source_policy", "atr_method_id", "atr_period",
                         "atr_policy_id", "price_basis", "source_at",
                         "confirmed_at", "available_at"):
                if getattr(self, name) is None:
                    raise ValueError(f"resolved candidate missing {name}")
            if not self.source_at <= self.confirmed_at <= self.available_at <= self.decision_at:
                raise ValueError("candidate causal times invalid")
        identity = tuple((item.name, getattr(self, item.name)) for item in fields(self)
                         if item.name not in ("event_id", "execution_authority"))
        object.__setattr__(self, "event_id", "r2a_" + canonical_sha256(identity).lower())
