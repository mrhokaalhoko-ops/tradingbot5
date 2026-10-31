"""Pure causal Strategy V2 regime, zone, trigger, and entry qualification."""

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
from .r3_thesis import ThesisSnapshot, ThesisStatus


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
        raise ValueError("input outside entry numeric domain")
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
        raise ValueError("nonterminating decimal outside entry policy")
    scale = max(twos, fives)
    if scale > 128:
        raise ValueError("entry result scale outside domain")
    coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    if len(str(coefficient)) > 256:
        raise ValueError("entry result digits outside domain")
    return Decimal((int(value < 0), tuple(map(int, str(coefficient))), -scale))


def _floor_ratio(value: Fraction, places: int) -> Decimal:
    scale = 10 ** places
    scaled = value * scale
    return _exact_decimal(Fraction(scaled.numerator // scaled.denominator, scale))


class RegimeLabel(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL_COMPRESSION = "LOW_VOL_COMPRESSION"
    UNKNOWN = "UNKNOWN"


class TriggerType(str, Enum):
    CLOSED_RETEST_REJECTION = "CLOSED_RETEST_REJECTION"
    CLOSED_RECLAIM = "CLOSED_RECLAIM"
    CLOSED_BREAKOUT_ACCEPTANCE = "CLOSED_BREAKOUT_ACCEPTANCE"


class EntryGateStatus(str, Enum):
    READY = "READY"
    NO_TRADE = "NO_TRADE"
    UNRESOLVED = "UNRESOLVED"


class EntryGateReason(str, Enum):
    THESIS_NOT_WAITING_TRIGGER = "THESIS_NOT_WAITING_TRIGGER"
    THESIS_EVIDENCE_FUTURE = "THESIS_EVIDENCE_FUTURE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    REGIME_EVIDENCE_FUTURE = "REGIME_EVIDENCE_FUTURE"
    REGIME_DIRECTION_BLOCKED = "REGIME_DIRECTION_BLOCKED"
    ZONE_EVIDENCE_FUTURE = "ZONE_EVIDENCE_FUTURE"
    ZONE_EXPIRED = "ZONE_EXPIRED"
    TRIGGER_EVIDENCE_FUTURE = "TRIGGER_EVIDENCE_FUTURE"
    TRIGGER_NOT_FINAL = "TRIGGER_NOT_FINAL"
    TRIGGER_POLICY_BLOCKED = "TRIGGER_POLICY_BLOCKED"
    TRIGGER_PRECEDES_ZONE = "TRIGGER_PRECEDES_ZONE"
    VOLATILITY_EVIDENCE_FUTURE = "VOLATILITY_EVIDENCE_FUTURE"
    QUOTE_EVIDENCE_FUTURE = "QUOTE_EVIDENCE_FUTURE"
    QUOTE_PRECEDES_TRIGGER = "QUOTE_PRECEDES_TRIGGER"
    QUOTE_STALE = "QUOTE_STALE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    EXECUTABLE_PRICE_OUTSIDE_ZONE_POLICY = "EXECUTABLE_PRICE_OUTSIDE_ZONE_POLICY"
    CHASE_DISTANCE_EXCEEDED = "CHASE_DISTANCE_EXCEEDED"
    NUMERIC_UNCOMPUTABLE = "NUMERIC_UNCOMPUTABLE"


@dataclass(frozen=True, slots=True)
class RegimeContextSnapshot:
    symbol: str
    contract_epoch_id: str
    label: RegimeLabel
    source_timeframes: tuple[str, ...]
    confirmed_at: datetime
    available_at: datetime
    source_event_ids: tuple[str, ...]
    source_hash: str
    context_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _text(self.symbol, "symbol")
        _text(self.contract_epoch_id, "contract_epoch_id")
        if not isinstance(self.label, RegimeLabel):
            raise TypeError("typed regime label required")
        if (not isinstance(self.source_timeframes, tuple) or not self.source_timeframes
                or len(set(self.source_timeframes)) != len(self.source_timeframes)):
            raise ValueError("unique source timeframes required")
        if (not isinstance(self.source_event_ids, tuple) or not self.source_event_ids
                or len(set(self.source_event_ids)) != len(self.source_event_ids)):
            raise ValueError("unique regime source events required")
        for item in (*self.source_timeframes, *self.source_event_ids):
            _text(item, "regime source")
        require_utc(self.confirmed_at, "confirmed_at")
        require_utc(self.available_at, "available_at")
        if self.confirmed_at > self.available_at:
            raise ValueError("regime cannot be available before confirmation")
        _digest(self.source_hash, "source_hash")
        object.__setattr__(self, "context_id", "rgm_" + canonical_sha256((
            self.symbol, self.contract_epoch_id, self.label, self.source_timeframes,
            self.confirmed_at, self.available_at, self.source_event_ids,
            self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class EntryZone:
    zone_id: str
    thesis_id: ThesisId
    symbol: str
    contract_epoch_id: str
    side: Side
    timeframe: str
    lower_price: Decimal
    upper_price: Decimal
    confirmed_at: datetime
    available_at: datetime
    expires_at: datetime
    source_event_ids: tuple[str, ...]
    zone_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("zone_id", "symbol", "contract_epoch_id", "timeframe"):
            _text(getattr(self, name), name)
        if type(self.thesis_id) is not ThesisId or not isinstance(self.side, Side):
            raise TypeError("typed thesis and side required")
        _decimal(self.lower_price, "lower_price")
        _decimal(self.upper_price, "upper_price")
        if self.lower_price >= self.upper_price:
            raise ValueError("zone lower price must be below upper price")
        for name in ("confirmed_at", "available_at", "expires_at"):
            require_utc(getattr(self, name), name)
        if not self.confirmed_at <= self.available_at < self.expires_at:
            raise ValueError("zone chronology invalid")
        if (not isinstance(self.source_event_ids, tuple) or not self.source_event_ids
                or len(set(self.source_event_ids)) != len(self.source_event_ids)):
            raise ValueError("unique zone sources required")
        object.__setattr__(self, "zone_hash", canonical_sha256((
            self.zone_id, self.thesis_id, self.symbol, self.contract_epoch_id, self.side,
            self.timeframe, self.lower_price, self.upper_price,
            self.confirmed_at, self.available_at, self.expires_at,
            self.source_event_ids,
        )).lower())


@dataclass(frozen=True, slots=True)
class ClosedTriggerEvidence:
    trigger_id: str
    thesis_id: ThesisId
    zone_id: str
    symbol: str
    contract_epoch_id: str
    side: Side
    timeframe: str
    trigger_type: TriggerType
    bar_id: str
    close_price: Decimal
    bar_closed_at: datetime
    available_at: datetime
    is_final: bool
    source_hash: str
    evidence_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("trigger_id", "zone_id", "symbol", "contract_epoch_id", "timeframe", "bar_id"):
            _text(getattr(self, name), name)
        if type(self.thesis_id) is not ThesisId or not isinstance(self.side, Side) or not isinstance(self.trigger_type, TriggerType):
            raise TypeError("typed trigger thesis, side, and type required")
        _decimal(self.close_price, "close_price")
        require_utc(self.bar_closed_at, "bar_closed_at")
        require_utc(self.available_at, "available_at")
        if self.bar_closed_at > self.available_at:
            raise ValueError("trigger cannot be available before bar close")
        if type(self.is_final) is not bool:
            raise TypeError("is_final must be bool")
        _digest(self.source_hash, "source_hash")
        object.__setattr__(self, "evidence_id", "trg_" + canonical_sha256((
            self.trigger_id, self.thesis_id, self.zone_id, self.symbol,
            self.contract_epoch_id,
            self.side, self.timeframe, self.trigger_type, self.bar_id,
            self.close_price, self.bar_closed_at, self.available_at,
            self.is_final, self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class VolatilityEvidence:
    symbol: str
    contract_epoch_id: str
    timeframe: str
    atr: Decimal
    source_window_hash: str
    confirmed_at: datetime
    available_at: datetime
    evidence_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _text(self.symbol, "symbol")
        _text(self.contract_epoch_id, "contract_epoch_id")
        _text(self.timeframe, "timeframe")
        _decimal(self.atr, "atr")
        _digest(self.source_window_hash, "source_window_hash")
        require_utc(self.confirmed_at, "confirmed_at")
        require_utc(self.available_at, "available_at")
        if self.confirmed_at > self.available_at:
            raise ValueError("ATR cannot be available before confirmation")
        object.__setattr__(self, "evidence_id", "atr_" + canonical_sha256((
            self.symbol, self.contract_epoch_id, self.timeframe, self.atr, self.source_window_hash,
            self.confirmed_at, self.available_at,
        )).lower())


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    symbol: str
    contract_epoch_id: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    available_at: datetime
    source_hash: str
    quote_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _text(self.symbol, "symbol")
        _text(self.contract_epoch_id, "contract_epoch_id")
        _decimal(self.bid, "bid")
        _decimal(self.ask, "ask")
        if self.bid > self.ask:
            raise ValueError("crossed quote forbidden")
        require_utc(self.observed_at, "observed_at")
        require_utc(self.available_at, "available_at")
        if self.observed_at > self.available_at:
            raise ValueError("quote cannot be available before observation")
        _digest(self.source_hash, "source_hash")
        object.__setattr__(self, "quote_id", "qte_" + canonical_sha256((
            self.symbol, self.contract_epoch_id, self.bid, self.ask,
            self.observed_at, self.available_at, self.source_hash,
        )).lower())


@dataclass(frozen=True, slots=True)
class EntryPolicySpec:
    source_commit: str
    policy_id: str
    rule_version: str
    zone_timeframe: str
    trigger_timeframe: str
    allowed_trigger_types: tuple[TriggerType, ...]
    max_chase_atr_multiple: Decimal
    max_spread_fraction: Decimal
    max_quote_age_seconds: int
    ratio_decimal_places: int
    numeric_policy_id: str
    variant_hash: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _digest(self.source_commit, "source_commit", 40)
        for name in ("policy_id", "rule_version", "zone_timeframe",
                     "trigger_timeframe", "numeric_policy_id"):
            _text(getattr(self, name), name)
        if (not isinstance(self.allowed_trigger_types, tuple)
                or not self.allowed_trigger_types
                or len(set(self.allowed_trigger_types)) != len(self.allowed_trigger_types)
                or any(not isinstance(item, TriggerType) for item in self.allowed_trigger_types)):
            raise ValueError("one or more unique typed trigger variants required")
        _decimal(self.max_chase_atr_multiple, "max_chase_atr_multiple", allow_zero=True)
        _decimal(self.max_spread_fraction, "max_spread_fraction", allow_zero=True)
        if self.max_spread_fraction > 1:
            raise ValueError("max_spread_fraction cannot exceed one")
        if type(self.max_quote_age_seconds) is not int or self.max_quote_age_seconds < 0:
            raise ValueError("nonnegative max quote age required")
        if type(self.ratio_decimal_places) is not int or not 0 <= self.ratio_decimal_places <= 12:
            raise ValueError("ratio_decimal_places must be in [0,12]")
        object.__setattr__(self, "variant_hash", canonical_sha256((
            self.source_commit, self.policy_id, self.rule_version,
            self.zone_timeframe, self.trigger_timeframe,
            self.allowed_trigger_types, self.max_chase_atr_multiple,
            self.max_spread_fraction, self.max_quote_age_seconds,
            self.ratio_decimal_places, self.numeric_policy_id,
        )).lower())


@dataclass(frozen=True, slots=True)
class EntryQualification:
    status: EntryGateStatus
    reason: EntryGateReason | None
    thesis_id: ThesisId
    side: Side
    zone_id: str
    trigger_evidence_id: str
    regime_context_id: str
    quote_id: str
    executable_price: Decimal | None
    chase_distance: Decimal | None
    maximum_chase_distance: Decimal | None
    spread: Decimal | None
    spread_fraction: Decimal | None
    policy_variant_hash: str
    decision_at: datetime
    qualification_id: str = field(init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, EntryGateStatus) or type(self.thesis_id) is not ThesisId or not isinstance(self.side, Side):
            raise TypeError("typed entry result, thesis, and side required")
        for name in ("zone_id", "trigger_evidence_id", "regime_context_id", "quote_id"):
            _text(getattr(self, name), name)
        _digest(self.policy_variant_hash, "policy_variant_hash")
        require_utc(self.decision_at, "decision_at")
        if self.status is EntryGateStatus.READY:
            if self.reason is not None or any(value is None for value in (
                self.executable_price, self.chase_distance,
                self.maximum_chase_distance, self.spread, self.spread_fraction,
            )):
                raise ValueError("ready entry qualification needs complete geometry")
        elif not isinstance(self.reason, EntryGateReason):
            raise ValueError("non-ready entry qualification needs typed reason")
        object.__setattr__(self, "qualification_id", "enq_" + canonical_sha256((
            self.status, self.reason, self.thesis_id, self.side, self.zone_id,
            self.trigger_evidence_id, self.regime_context_id, self.quote_id,
            self.executable_price, self.chase_distance,
            self.maximum_chase_distance, self.spread, self.spread_fraction,
            self.policy_variant_hash,
        )).lower())


def qualify_entry(
    *,
    thesis: ThesisSnapshot,
    regime: RegimeContextSnapshot,
    zone: EntryZone,
    trigger: ClosedTriggerEvidence,
    volatility: VolatilityEvidence,
    quote: ExecutableQuote,
    policy: EntryPolicySpec,
    decision_at: datetime,
) -> EntryQualification:
    require_utc(decision_at, "decision_at")
    if not all(isinstance(value, kind) for value, kind in (
        (thesis, ThesisSnapshot), (regime, RegimeContextSnapshot),
        (zone, EntryZone), (trigger, ClosedTriggerEvidence),
        (volatility, VolatilityEvidence), (quote, ExecutableQuote),
        (policy, EntryPolicySpec),
    )):
        raise TypeError("typed causal entry inputs required")
    common = dict(
        thesis_id=thesis.identity.thesis_id, side=thesis.identity.side,
        zone_id=zone.zone_id, trigger_evidence_id=trigger.evidence_id,
        regime_context_id=regime.context_id, quote_id=quote.quote_id,
        policy_variant_hash=policy.variant_hash, decision_at=decision_at,
    )
    def result(status, reason, **values):
        geometry = dict(
            executable_price=None, chase_distance=None,
            maximum_chase_distance=None, spread=None, spread_fraction=None,
        )
        geometry.update(values)
        return EntryQualification(status, reason, **common, **geometry)
    if thesis.status is not ThesisStatus.WAIT_TRIGGER:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.THESIS_NOT_WAITING_TRIGGER)
    if thesis.events[-1].decision_at > decision_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.THESIS_EVIDENCE_FUTURE)
    identity_matches = (
        regime.symbol == zone.symbol == trigger.symbol == volatility.symbol == quote.symbol == thesis.identity.symbol
        and regime.contract_epoch_id == zone.contract_epoch_id == trigger.contract_epoch_id == volatility.contract_epoch_id == quote.contract_epoch_id
        and zone.thesis_id == trigger.thesis_id == thesis.identity.thesis_id
        and zone.side is trigger.side is thesis.identity.side
        and trigger.zone_id == zone.zone_id
        and zone.timeframe == policy.zone_timeframe
        and trigger.timeframe == policy.trigger_timeframe
        and volatility.timeframe == policy.zone_timeframe
    )
    if not identity_matches:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.IDENTITY_MISMATCH)
    if regime.available_at > decision_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.REGIME_EVIDENCE_FUTURE)
    required_regime = RegimeLabel.TREND_UP if thesis.identity.side is Side.LONG else RegimeLabel.TREND_DOWN
    if regime.label is not required_regime:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.REGIME_DIRECTION_BLOCKED)
    if zone.available_at > decision_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.ZONE_EVIDENCE_FUTURE)
    if decision_at >= zone.expires_at:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.ZONE_EXPIRED)
    if trigger.available_at > decision_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.TRIGGER_EVIDENCE_FUTURE)
    if not trigger.is_final:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.TRIGGER_NOT_FINAL)
    if trigger.trigger_type not in policy.allowed_trigger_types:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.TRIGGER_POLICY_BLOCKED)
    if trigger.bar_closed_at < zone.available_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.TRIGGER_PRECEDES_ZONE)
    if volatility.available_at > decision_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.VOLATILITY_EVIDENCE_FUTURE)
    if quote.available_at > decision_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.QUOTE_EVIDENCE_FUTURE)
    if quote.observed_at < trigger.available_at:
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.QUOTE_PRECEDES_TRIGGER)
    age_seconds = (decision_at - quote.available_at).total_seconds()
    if age_seconds > policy.max_quote_age_seconds:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.QUOTE_STALE)
    try:
        bid = _fraction(quote.bid)
        ask = _fraction(quote.ask)
        executable = ask if thesis.identity.side is Side.LONG else bid
        spread_f = ask - bid
        midpoint = (ask + bid) / 2
        spread_fraction_f = spread_f / midpoint
        max_chase = _fraction(volatility.atr) * _fraction(policy.max_chase_atr_multiple)
        lower = _fraction(zone.lower_price)
        upper = _fraction(zone.upper_price)
        if thesis.identity.side is Side.LONG:
            if executable < lower:
                return result(EntryGateStatus.NO_TRADE, EntryGateReason.EXECUTABLE_PRICE_OUTSIDE_ZONE_POLICY)
            chase = max(Fraction(), executable - upper)
        else:
            if executable > upper:
                return result(EntryGateStatus.NO_TRADE, EntryGateReason.EXECUTABLE_PRICE_OUTSIDE_ZONE_POLICY)
            chase = max(Fraction(), lower - executable)
        values = dict(
            executable_price=_exact_decimal(executable),
            chase_distance=_exact_decimal(chase),
            maximum_chase_distance=_exact_decimal(max_chase),
            spread=_exact_decimal(spread_f),
            spread_fraction=_floor_ratio(spread_fraction_f, policy.ratio_decimal_places),
        )
    except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
        return result(EntryGateStatus.UNRESOLVED, EntryGateReason.NUMERIC_UNCOMPUTABLE)
    if spread_fraction_f > _fraction(policy.max_spread_fraction):
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.SPREAD_TOO_WIDE, **values)
    if chase > max_chase:
        return result(EntryGateStatus.NO_TRADE, EntryGateReason.CHASE_DISTANCE_EXCEEDED, **values)
    return result(EntryGateStatus.READY, None, **values)
