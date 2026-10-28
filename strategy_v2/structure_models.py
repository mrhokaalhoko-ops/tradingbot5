"""Non-authoritative structure evidence and explicitly unresolved protection."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .causality import CausalTimes
from .events import canonical_sha256
from .models import ThesisId


def _sha256_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError(f"{name} must be a SHA-256 digest")


def make_pivot_id(*, pivot_type: PivotType, price: Decimal,
                  source_bar_id: str, confirmation_bar_id: str,
                  symbol: str, timeframe: str, times: CausalTimes,
                  parent_evidence_ids: tuple[str, ...], source_window_hash: str,
                  spec_id: str, rule_version: str) -> str:
    """Bind every stored pivot field to its deterministic evidence identity."""
    return "pvt_" + canonical_sha256((
        pivot_type, price, source_bar_id, confirmation_bar_id, symbol, timeframe,
        times, parent_evidence_ids, source_window_hash, spec_id, rule_version,
    )).lower()


def make_structure_point_id(*, label: StructureLabel, pivot_id: str,
                            prior_pivot_id: str | None, price: Decimal,
                            symbol: str, timeframe: str, times: CausalTimes,
                            parent_evidence_ids: tuple[str, ...], spec_id: str,
                            rule_version: str) -> str:
    """Bind classification, price, provenance, and lineage to the point ID."""
    return "str_" + canonical_sha256((
        label, pivot_id, prior_pivot_id, price, symbol, timeframe, times,
        parent_evidence_ids, spec_id, rule_version,
    )).lower()


def make_break_id(*, broken_level_id: str, break_direction: BreakDirection,
                  break_mode: BreakMode, break_bar_id: str, price: Decimal,
                  threshold: Decimal, symbol: str, timeframe: str,
                  times: CausalTimes, parent_evidence_ids: tuple[str, ...],
                  break_support_hash: str, spec_id: str, rule_version: str) -> str:
    """Bind both close-cross evidence and its full support hash to the break ID."""
    return "brk_" + canonical_sha256((
        broken_level_id, break_direction, break_mode, break_bar_id, price,
        threshold, symbol, timeframe, times, parent_evidence_ids,
        break_support_hash, spec_id, rule_version,
    )).lower()


class PivotType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class EqualTiePolicy(str, Enum):
    EARLIEST = "EARLIEST"


class StructureLabel(str, Enum):
    HH = "HH"
    HL = "HL"
    LH = "LH"
    LL = "LL"
    EQUAL_HIGH = "EQUAL_HIGH"
    EQUAL_LOW = "EQUAL_LOW"
    UNCLASSIFIED = "UNCLASSIFIED"


class BreakDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


class BreakMode(str, Enum):
    CLOSE_ONLY = "CLOSE_ONLY"


class ProtectionStatus(str, Enum):
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class StructureSpec:
    pivot_radius_left: int
    pivot_radius_right: int
    equal_price_tolerance: Decimal
    equal_tie_policy: EqualTiePolicy
    close_break_buffer: Decimal
    rule_version: str

    def __post_init__(self) -> None:
        for name in ("pivot_radius_left", "pivot_radius_right"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be an explicit positive integer")
        for name in ("equal_price_tolerance", "close_break_buffer"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be an explicit nonnegative Decimal")
        if self.equal_tie_policy is not EqualTiePolicy.EARLIEST:
            raise ValueError("explicit EARLIEST tie policy required")
        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise ValueError("rule_version required")


@dataclass(frozen=True, slots=True)
class ConfirmedPivot:
    pivot_id: str
    pivot_type: PivotType
    price: Decimal
    source_bar_id: str
    confirmation_bar_id: str
    symbol: str
    timeframe: str
    times: CausalTimes
    parent_evidence_ids: tuple[str, ...]
    source_window_hash: str
    spec_id: str
    rule_version: str

    def __post_init__(self) -> None:
        _evidence(self.pivot_id, self.price, self.symbol, self.timeframe,
                  self.times, self.parent_evidence_ids, self.spec_id,
                  self.rule_version)
        if not isinstance(self.pivot_type, PivotType):
            raise TypeError("pivot_type required")
        if not self.source_bar_id or not self.confirmation_bar_id:
            raise ValueError("source and confirmation bar IDs required")
        if (self.source_bar_id not in self.parent_evidence_ids or
                self.confirmation_bar_id not in self.parent_evidence_ids):
            raise ValueError("pivot parent bars required")
        _sha256_digest(self.source_window_hash, "source_window_hash")
        expected_id = make_pivot_id(
            pivot_type=self.pivot_type, price=self.price,
            source_bar_id=self.source_bar_id,
            confirmation_bar_id=self.confirmation_bar_id,
            symbol=self.symbol, timeframe=self.timeframe, times=self.times,
            parent_evidence_ids=self.parent_evidence_ids,
            source_window_hash=self.source_window_hash, spec_id=self.spec_id,
            rule_version=self.rule_version,
        )
        if self.pivot_id != expected_id:
            raise ValueError("pivot_id does not match pivot evidence")

    @property
    def source_at(self):
        return self.times.source_at

    @property
    def confirmed_at(self):
        return self.times.confirmed_at

    @property
    def available_at(self):
        return self.times.available_at


@dataclass(frozen=True, slots=True)
class StructurePoint:
    point_id: str
    label: StructureLabel
    pivot_id: str
    prior_pivot_id: str | None
    price: Decimal
    symbol: str
    timeframe: str
    times: CausalTimes
    parent_evidence_ids: tuple[str, ...]
    spec_id: str
    rule_version: str

    def __post_init__(self) -> None:
        _evidence(self.point_id, self.price, self.symbol, self.timeframe,
                  self.times, self.parent_evidence_ids, self.spec_id,
                  self.rule_version)
        if not isinstance(self.label, StructureLabel) or not self.pivot_id:
            raise ValueError("structure label and pivot ID required")
        expected = ((self.pivot_id,) if self.prior_pivot_id is None
                    else (self.prior_pivot_id, self.pivot_id))
        if self.parent_evidence_ids != expected:
            raise ValueError("structure point parent pivot IDs mismatch")
        expected_id = make_structure_point_id(
            label=self.label, pivot_id=self.pivot_id,
            prior_pivot_id=self.prior_pivot_id, price=self.price,
            symbol=self.symbol, timeframe=self.timeframe, times=self.times,
            parent_evidence_ids=self.parent_evidence_ids, spec_id=self.spec_id,
            rule_version=self.rule_version,
        )
        if self.point_id != expected_id:
            raise ValueError("point_id does not match structure evidence")

    @property
    def available_at(self):
        return self.times.available_at


@dataclass(frozen=True, slots=True)
class ProtectedLevel:
    """Schema only: requires an independently established thesis and continuation."""
    level_id: str
    level_type: PivotType
    price: Decimal
    originating_pivot_id: str
    thesis_id: ThesisId
    continuation_event_id: str
    protection_reason: str
    symbol: str
    timeframe: str
    times: CausalTimes
    parent_evidence_ids: tuple[str, ...]
    spec_id: str
    rule_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.level_type, PivotType):
            raise TypeError("level_type required")
        _evidence(self.level_id, self.price, self.symbol, self.timeframe,
                  self.times, self.parent_evidence_ids, self.spec_id,
                  self.rule_version)
        if type(self.thesis_id) is not ThesisId:
            raise TypeError("thesis_id must be ThesisId")
        if any(not isinstance(value, str) or not value.strip() for value in
               (self.originating_pivot_id,
                self.continuation_event_id, self.protection_reason)):
            raise ValueError("protected level requires pivot, thesis, continuation and reason")
        if (self.originating_pivot_id not in self.parent_evidence_ids or
                self.continuation_event_id not in self.parent_evidence_ids):
            raise ValueError("protected level parent evidence mismatch")


@dataclass(frozen=True, slots=True)
class ProtectionResolution:
    status: ProtectionStatus
    reason: str
    levels: tuple[ProtectedLevel, ...]

    def __post_init__(self) -> None:
        if self.status is not ProtectionStatus.UNRESOLVED or not self.reason or self.levels:
            raise ValueError("R1 protection must remain unresolved and empty")


@dataclass(frozen=True, slots=True)
class StructureBreak:
    """Generic close-cross evidence, not a BOS or CHoCH classification."""
    break_id: str
    broken_level_id: str
    break_direction: BreakDirection
    break_mode: BreakMode
    break_bar_id: str
    price: Decimal
    threshold: Decimal
    symbol: str
    timeframe: str
    times: CausalTimes
    parent_evidence_ids: tuple[str, ...]
    break_support_hash: str
    spec_id: str
    rule_version: str

    def __post_init__(self) -> None:
        _evidence(self.break_id, self.price, self.symbol, self.timeframe,
                  self.times, self.parent_evidence_ids, self.spec_id,
                  self.rule_version)
        if (not self.broken_level_id or not self.break_bar_id or
                not isinstance(self.break_direction, BreakDirection) or
                self.break_mode is not BreakMode.CLOSE_ONLY):
            raise ValueError("generic close break identity required")
        if self.broken_level_id not in self.parent_evidence_ids or self.break_bar_id not in self.parent_evidence_ids:
            raise ValueError("break parent evidence mismatch")
        if not isinstance(self.threshold, Decimal) or not self.threshold.is_finite() or self.threshold <= 0:
            raise ValueError("positive Decimal threshold required")
        _sha256_digest(self.break_support_hash, "break_support_hash")
        expected_id = make_break_id(
            broken_level_id=self.broken_level_id,
            break_direction=self.break_direction, break_mode=self.break_mode,
            break_bar_id=self.break_bar_id, price=self.price,
            threshold=self.threshold, symbol=self.symbol,
            timeframe=self.timeframe, times=self.times,
            parent_evidence_ids=self.parent_evidence_ids,
            break_support_hash=self.break_support_hash, spec_id=self.spec_id,
            rule_version=self.rule_version,
        )
        if self.break_id != expected_id:
            raise ValueError("break_id does not match break evidence")

    @property
    def available_at(self):
        return self.times.available_at


def _evidence(item_id: str, price: Decimal, symbol: str, timeframe: str,
              times: CausalTimes, parents: tuple[str, ...], spec_id: str,
              rule_version: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in
           (item_id, symbol, timeframe, rule_version)):
        raise ValueError("evidence identity required")
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise ValueError("positive Decimal evidence price required")
    if not isinstance(times, CausalTimes):
        raise TypeError("causal times required")
    if not isinstance(parents, tuple) or not parents or any(
            not isinstance(value, str) or not value.strip() for value in parents):
        raise ValueError("parent evidence IDs required")
    if not isinstance(spec_id, str) or len(spec_id) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in spec_id):
        raise ValueError("spec_id must be a SHA-256 digest")
