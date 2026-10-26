"""Immutable price, provenance, identity, regime, and reward contracts."""

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal

from .causality import CausalTimes, require_utc
from .enums import (DirectionState, InvalidationRelation, InvalidationScope,
                    Side, VolatilityState)


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")


def _price(value: Decimal, name: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'}")


def _ids(values: tuple[str, ...], name: str, *, nonempty: bool = False) -> None:
    if not isinstance(values, tuple) or (nonempty and not values):
        raise TypeError(f"{name} must be a {'nonempty ' if nonempty else ''}tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{name} must contain only nonempty strings")


@dataclass(frozen=True, slots=True)
class _TypedId:
    value: str

    def __post_init__(self) -> None:
        _text(self.value, type(self).__name__)


class ThesisId(_TypedId):
    pass


class SetupId(_TypedId):
    pass


class AttemptId(_TypedId):
    pass


class ZoneId(_TypedId):
    pass


class TriggerId(_TypedId):
    pass


class QuoteId(_TypedId):
    pass


class ProposalId(_TypedId):
    pass


class OrderIntentId(_TypedId):
    pass


class OrderId(_TypedId):
    pass


class FillId(_TypedId):
    pass


@dataclass(frozen=True, slots=True)
class IdentityRefs:
    thesis_id: ThesisId | None = None
    setup_id: SetupId | None = None
    attempt_id: AttemptId | None = None
    zone_id: ZoneId | None = None
    trigger_id: TriggerId | None = None
    quote_id: QuoteId | None = None
    proposal_id: ProposalId | None = None
    order_intent_id: OrderIntentId | None = None
    order_id: OrderId | None = None
    fill_id: FillId | None = None

    def __post_init__(self) -> None:
        values = []
        for field in fields(self):
            value = getattr(self, field.name)
            expected = globals()["".join(part.capitalize() for part in field.name.split("_")[:-1]) + "Id"]
            if value is not None and type(value) is not expected:
                raise TypeError(f"{field.name} requires {expected.__name__}")
            if value is not None:
                values.append(value.value)
        if len(values) != len(set(values)):
            raise ValueError("IDs cannot be reused across entity kinds")


@dataclass(frozen=True, slots=True)
class EntryZone:
    zone_id: ZoneId
    symbol: str
    side: Side
    lower: Decimal
    upper: Decimal
    timeframe: str
    source_level_ids: tuple[str, ...]
    times: CausalTimes
    expires_at: datetime | None
    rule_version: str
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.zone_id) is not ZoneId or not isinstance(self.side, Side):
            raise TypeError("typed zone_id and side are required")
        _text(self.symbol, "symbol")
        _text(self.timeframe, "timeframe")
        _text(self.rule_version, "rule_version")
        _price(self.lower, "lower")
        _price(self.upper, "upper")
        if self.lower > self.upper:
            raise ValueError("zone lower cannot exceed upper")
        _ids(self.source_level_ids, "source_level_ids", nonempty=True)
        _ids(self.source_event_ids, "source_event_ids")
        if not isinstance(self.times, CausalTimes):
            raise TypeError("times must be CausalTimes")
        if self.expires_at is not None:
            require_utc(self.expires_at, "expires_at")
            if self.expires_at <= self.times.available_at:
                raise ValueError("zone expiry must follow availability")

    @property
    def midpoint(self) -> Decimal:
        return (self.lower + self.upper) / Decimal(2)


@dataclass(frozen=True, slots=True)
class TriggerEvidence:
    trigger_id: TriggerId
    zone_id: ZoneId
    trigger_rule_id: str
    candle_id: str
    trigger_price: Decimal
    candle_close_at: datetime
    times: CausalTimes

    def __post_init__(self) -> None:
        if type(self.trigger_id) is not TriggerId or type(self.zone_id) is not ZoneId:
            raise TypeError("typed trigger_id and zone_id are required")
        _text(self.trigger_rule_id, "trigger_rule_id")
        _text(self.candle_id, "candle_id")
        _price(self.trigger_price, "trigger_price")
        require_utc(self.candle_close_at, "candle_close_at")
        if not isinstance(self.times, CausalTimes):
            raise TypeError("times must be CausalTimes")
        if self.candle_close_at > self.times.confirmed_at:
            raise ValueError("trigger cannot confirm before candle close")


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    quote_id: QuoteId
    symbol: str
    best_bid: Decimal
    best_ask: Decimal
    mark_price: Decimal | None
    event_at: datetime
    received_at: datetime
    available_at: datetime
    source: str
    update_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.quote_id) is not QuoteId:
            raise TypeError("quote_id must be QuoteId")
        _text(self.symbol, "symbol")
        _text(self.source, "source")
        _price(self.best_bid, "best_bid")
        _price(self.best_ask, "best_ask")
        if self.best_bid > self.best_ask:
            raise ValueError("bid cannot exceed ask")
        if self.mark_price is not None:
            _price(self.mark_price, "mark_price")
        for name in ("event_at", "received_at", "available_at"):
            require_utc(getattr(self, name), name)
        if not self.event_at <= self.received_at <= self.available_at:
            raise ValueError("quote time ordering invalid")


@dataclass(frozen=True, slots=True)
class OrderPlanPrice:
    planned_price: Decimal
    order_type: str
    price_basis: str
    quote_id: QuoteId
    planned_at: datetime
    time_in_force: str | None = None

    def __post_init__(self) -> None:
        _price(self.planned_price, "planned_price")
        _text(self.order_type, "order_type")
        _text(self.price_basis, "price_basis")
        if type(self.quote_id) is not QuoteId:
            raise TypeError("quote_id must be QuoteId")
        require_utc(self.planned_at, "planned_at")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: FillId
    order_id: OrderId
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    filled_at: datetime
    exchange_trade_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.fill_id) is not FillId or type(self.order_id) is not OrderId:
            raise TypeError("typed fill_id and order_id are required")
        _price(self.price, "price")
        _price(self.quantity, "quantity")
        _price(self.fee, "fee", allow_zero=True)
        _text(self.fee_asset, "fee_asset")
        require_utc(self.filled_at, "filled_at")


@dataclass(frozen=True, slots=True)
class FillLedger:
    fills: tuple[Fill, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.fills, tuple) or any(type(item) is not Fill for item in self.fills):
            raise TypeError("fills must be a tuple of Fill records")
        ids = [item.fill_id.value for item in self.fills]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate fill_id")

    @property
    def total_quantity(self) -> Decimal:
        return sum((item.quantity for item in self.fills), Decimal(0))

    @property
    def average_price(self) -> Decimal | None:
        if not self.fills:
            return None
        return sum((item.price * item.quantity for item in self.fills), Decimal(0)) / self.total_quantity


@dataclass(frozen=True, slots=True)
class EntryPriceLineage:
    original_zone: EntryZone
    trigger: TriggerEvidence
    executable_quote: ExecutableQuote
    order_plan_price: OrderPlanPrice
    fills: FillLedger
    decision_at: datetime

    def __post_init__(self) -> None:
        if self.trigger.zone_id != self.original_zone.zone_id:
            raise ValueError("trigger must refer to original zone")
        if self.executable_quote.symbol != self.original_zone.symbol:
            raise ValueError("quote symbol must match zone")
        if self.order_plan_price.quote_id != self.executable_quote.quote_id:
            raise ValueError("order plan must refer to executable quote")
        if not isinstance(self.fills, FillLedger):
            raise TypeError("fills must be FillLedger")
        require_utc(self.decision_at, "decision_at")
        if not (
            self.original_zone.times.available_at
            <= self.trigger.times.available_at
            <= self.executable_quote.available_at
            <= self.order_plan_price.planned_at
            <= self.decision_at
        ):
            raise ValueError("lineage causal ordering invalid or unavailable at decision")
        if any(fill.filled_at < self.order_plan_price.planned_at for fill in self.fills.fills):
            raise ValueError("fill cannot precede order plan")


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    direction_state: DirectionState
    volatility_state: VolatilityState
    as_of: datetime
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.direction_state, DirectionState) or not isinstance(self.volatility_state, VolatilityState):
            raise TypeError("direction and volatility require separate enums")
        require_utc(self.as_of, "as_of")
        _ids(self.evidence_ids, "evidence_ids")


@dataclass(frozen=True, slots=True)
class InvalidationLevel:
    level_id: str
    scope: InvalidationScope
    price: Decimal
    relation: InvalidationRelation
    side: Side
    source: str
    timeframe: str
    times: CausalTimes
    thesis_id: ThesisId | None = None
    setup_id: SetupId | None = None

    def __post_init__(self) -> None:
        for name in ("level_id", "source", "timeframe"):
            _text(getattr(self, name), name)
        if not isinstance(self.scope, InvalidationScope) or not isinstance(self.relation, InvalidationRelation) or not isinstance(self.side, Side):
            raise TypeError("invalid scope/relation/side")
        if not isinstance(self.times, CausalTimes):
            raise TypeError("invalidation provenance is required")
        _price(self.price, "price")
        if self.thesis_id is not None and type(self.thesis_id) is not ThesisId:
            raise TypeError("thesis_id must be ThesisId")
        if self.setup_id is not None and type(self.setup_id) is not SetupId:
            raise TypeError("setup_id must be SetupId")
        if self.thesis_id is None and self.setup_id is None:
            raise ValueError("invalidation needs a thesis/setup relation")


@dataclass(frozen=True, slots=True)
class RewardReference:
    reference_id: str
    price: Decimal
    side: Side
    source_level_id: str
    times: CausalTimes

    def __post_init__(self) -> None:
        _text(self.reference_id, "reference_id")
        _text(self.source_level_id, "source_level_id")
        _price(self.price, "price")
        if not isinstance(self.side, Side) or not isinstance(self.times, CausalTimes):
            raise TypeError("reward side/provenance required")


@dataclass(frozen=True, slots=True)
class ExitTarget:
    target_id: str
    price: Decimal
    side: Side
    source_level_id: str
    times: CausalTimes

    def __post_init__(self) -> None:
        _text(self.target_id, "target_id")
        _text(self.source_level_id, "source_level_id")
        _price(self.price, "price")
        if not isinstance(self.side, Side) or not isinstance(self.times, CausalTimes):
            raise TypeError("target side/provenance required")


@dataclass(frozen=True, slots=True)
class ExitPlanContract:
    policy_id: str
    reward_reference: RewardReference
    exit_target: ExitTarget | None
    final_tp_required: bool
    exit_event_types: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        if not isinstance(self.reward_reference, RewardReference):
            raise ValueError("reward_reference is required for every policy")
        if not isinstance(self.final_tp_required, bool):
            raise TypeError("final_tp_required must be bool")
        if self.exit_target is not None and not isinstance(self.exit_target, ExitTarget):
            raise TypeError("exit_target must be ExitTarget or None")
        if self.final_tp_required and self.exit_target is None:
            raise ValueError("policy requires an exit target")
        if self.exit_target is None and not self.exit_event_types:
            raise ValueError("runner needs explicit exit event types")
        if self.exit_target is not None and self.exit_target.side != self.reward_reference.side:
            raise ValueError("target and reward sides disagree")
        _ids(self.exit_event_types, "exit_event_types")
