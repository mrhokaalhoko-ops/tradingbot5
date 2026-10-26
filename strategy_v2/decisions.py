"""Non-authoritative research decisions. No order or proposal bridge exists."""

from dataclasses import dataclass, field
from datetime import datetime

from .causality import require_utc
from .enums import DecisionStatus, NoTradeReason, Side
from .models import AttemptId, ExitPlanContract, SetupId, ThesisId


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    status: DecisionStatus
    symbol: str
    side: Side | None
    decision_at: datetime
    stage: str
    failed_invariant: str | None
    input_evidence_ids: tuple[str, ...]
    reason_code: NoTradeReason | None = None
    thesis_id: ThesisId | None = None
    setup_id: SetupId | None = None
    attempt_id: AttemptId | None = None
    exit_plan: ExitPlanContract | None = None
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, DecisionStatus):
            raise TypeError("status must be DecisionStatus")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol required")
        if self.side is not None and not isinstance(self.side, Side):
            raise TypeError("side must be Side or None")
        require_utc(self.decision_at, "decision_at")
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("decision stage required")
        if not isinstance(self.input_evidence_ids, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.input_evidence_ids
        ):
            raise TypeError("input_evidence_ids must be a tuple of nonempty strings")
        for name, expected in (("thesis_id", ThesisId), ("setup_id", SetupId),
                               ("attempt_id", AttemptId)):
            value = getattr(self, name)
            if value is not None and type(value) is not expected:
                raise TypeError(f"{name} must be {expected.__name__}")
        if self.status is DecisionStatus.NO_TRADE:
            if not isinstance(self.reason_code, NoTradeReason):
                raise ValueError("NO_TRADE requires a typed reason")
            if not self.failed_invariant:
                raise ValueError("NO_TRADE requires a failed invariant")
            if self.exit_plan is not None:
                raise ValueError("NO_TRADE cannot contain an exit plan")
        else:
            if self.reason_code is not None or self.failed_invariant is not None:
                raise ValueError("candidate cannot carry a rejection reason")
            if self.side is None or not isinstance(self.exit_plan, ExitPlanContract):
                raise ValueError("candidate requires side and reward/exit contract")
            if not self.input_evidence_ids:
                raise ValueError("candidate requires input evidence")
            if self.exit_plan.reward_reference.side is not self.side:
                raise ValueError("candidate side must match reward reference side")
            if (self.exit_plan.exit_target is not None
                    and self.exit_plan.exit_target.side is not self.side):
                raise ValueError("candidate side must match exit target side")
            if self.exit_plan.reward_reference.times.available_at > self.decision_at:
                raise ValueError("reward reference unavailable at decision")
            if (self.exit_plan.exit_target is not None
                    and self.exit_plan.exit_target.times.available_at > self.decision_at):
                raise ValueError("exit target unavailable at decision")


def no_trade(*, symbol: str, side: Side | None, decision_at: datetime,
             stage: str, reason_code: NoTradeReason, failed_invariant: str,
             input_evidence_ids: tuple[str, ...] = (),
             thesis_id: ThesisId | None = None,
             setup_id: SetupId | None = None,
             attempt_id: AttemptId | None = None) -> StrategyDecision:
    """Create a typed rejection; never create order/proposal/fill identities."""
    return StrategyDecision(
        status=DecisionStatus.NO_TRADE, symbol=symbol, side=side,
        decision_at=decision_at, stage=stage,
        failed_invariant=failed_invariant,
        input_evidence_ids=input_evidence_ids, reason_code=reason_code,
        thesis_id=thesis_id, setup_id=setup_id, attempt_id=attempt_id,
    )
