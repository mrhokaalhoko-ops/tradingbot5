"""Pure timestamp validation; the caller supplies every observation time."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .enums import NoTradeReason


def require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CausalTimes:
    source_at: datetime
    confirmed_at: datetime
    available_at: datetime
    received_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("source_at", "confirmed_at", "available_at"):
            require_utc(getattr(self, name), name)
        if self.received_at is not None:
            require_utc(self.received_at, "received_at")
        if not self.source_at <= self.confirmed_at <= self.available_at:
            raise ValueError("source_at <= confirmed_at <= available_at required")
        if self.received_at is not None and not (
            self.source_at <= self.received_at <= self.available_at
        ):
            raise ValueError("source_at <= received_at <= available_at required")


@dataclass(frozen=True, slots=True)
class CausalValidation:
    valid: bool
    reason_code: NoTradeReason | None
    failed_invariant: str | None


def validate_available(times: CausalTimes, *, decision_at: datetime) -> CausalValidation:
    require_utc(decision_at, "decision_at")
    if times.available_at > decision_at:
        return CausalValidation(False, NoTradeReason.FUTURE_EVIDENCE,
                                "available_at <= decision_at")
    return CausalValidation(True, None, None)
