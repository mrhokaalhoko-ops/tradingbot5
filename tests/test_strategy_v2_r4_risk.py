"""Pure R4 risk sizing contract tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from strategy_v2.enums import Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import ThesisId
from strategy_v2.r4_risk import (
    CostModelSpec,
    MetadataProvenance,
    QuantityFilterKind,
    RiskPlanReason,
    RiskPlanStatus,
    RiskPolicySpec,
    VenueFilterSnapshot,
    build_risk_plan,
)


D = Decimal
BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
COMMIT = "562c5201426ab0ac31215448bd0a2a840d9c7a53"


def filters(**overrides):
    values = dict(
        symbol="ABCUSDT", contract_epoch_id="ABC-PERP-E1",
        snapshot_id="exchange-info-archived-1",
        source_hash=canonical_sha256(("raw-filter-payload",)).lower(),
        quantity_filter_kind=QuantityFilterKind.MARKET_LOT_SIZE,
        quantity_step=D("0.001"), minimum_quantity=D("0.001"),
        maximum_quantity=D("1000"), minimum_notional=D("5"),
        maximum_notional=D("1000000"), effective_from=BASE - timedelta(days=1),
        effective_until=BASE + timedelta(days=1),
        confirmed_at=BASE - timedelta(minutes=2),
        available_at=BASE - timedelta(minutes=1),
        provenance=MetadataProvenance.EXACT_EFFECTIVE_INTERVAL,
    )
    values.update(overrides)
    return VenueFilterSnapshot(**values)


def costs(**overrides):
    values = dict(
        policy_id="CONSERVATIVE_COST_V1", entry_fee_rate=D("0.0005"),
        exit_fee_rate=D("0.0005"), entry_slippage_rate=D("0.0005"),
        stop_slippage_rate=D("0.001"), funding_rate_abs=D("0.0002"),
        uncertainty_rate=D("0.0003"),
    )
    values.update(overrides)
    return CostModelSpec(**values)


def policy(**overrides):
    values = dict(
        source_commit=COMMIT, policy_id="RISK_FROM_FINAL_HARD_STOP_V1",
        rule_version="r4", target_equity_risk_fraction=D("0.0025"),
        hard_max_equity_risk_fraction=D("0.01"),
        target_initial_margin_fraction=D("0.01"),
        minimum_target_utilization=D("0.70"), leverage=D("5"),
        utilization_decimal_places=8,
        numeric_policy_id="EXACT_FRACTION_FLOOR_QUANTITY_V1",
    )
    values.update(overrides)
    return RiskPolicySpec(**values)


def plan(**overrides):
    values = dict(
        thesis_id=ThesisId("thesis-1"), side=Side.LONG,
        symbol="ABCUSDT", contract_epoch_id="ABC-PERP-E1",
        entry_price=D("100"), hard_stop_price=D("95"),
        account_equity=D("10000"), available_initial_margin=D("10000"),
        quantity_filter_kind=QuantityFilterKind.MARKET_LOT_SIZE,
        filters=filters(), costs=costs(), policy=policy(), decision_at=BASE,
    )
    values.update(overrides)
    return build_risk_plan(**values)


@pytest.mark.parametrize("side,stop", [(Side.LONG, "95"), (Side.SHORT, "105")])
def test_risk_plan_is_symmetric_and_uses_final_hard_stop(side, stop):
    result = plan(side=side, hard_stop_price=D(stop))
    assert result.status is RiskPlanStatus.READY
    assert result.projected_total_loss <= result.target_risk_budget
    assert result.projected_total_loss <= result.hard_risk_cap
    assert result.initial_margin <= D("100")
    assert result.target_utilization >= D("0.70")
    assert result.execution_authority is False


def test_wider_stop_reduces_quantity_without_moving_stop():
    sizing_policy = policy(target_initial_margin_fraction=D("0.10"))
    narrow = plan(hard_stop_price=D("97"), policy=sizing_policy)
    wide = plan(hard_stop_price=D("90"), policy=sizing_policy)
    assert narrow.status is wide.status is RiskPlanStatus.READY
    assert wide.quantity < narrow.quantity
    assert wide.hard_stop_price == D("90")


def test_costs_are_included_in_projected_loss():
    zero_cost = plan(costs=costs(
        entry_fee_rate=D("0"), exit_fee_rate=D("0"),
        entry_slippage_rate=D("0"), stop_slippage_rate=D("0"),
        funding_rate_abs=D("0"), uncertainty_rate=D("0"),
    ))
    conservative = plan()
    assert conservative.worst_loss_per_unit > zero_cost.worst_loss_per_unit
    assert conservative.quantity < zero_cost.quantity


def test_historical_filter_provenance_and_availability_fail_closed():
    current_only = plan(filters=filters(provenance=MetadataProvenance.CURRENT_SNAPSHOT_ONLY))
    assert current_only.status is RiskPlanStatus.UNRESOLVED
    assert current_only.reason is RiskPlanReason.METADATA_PROVENANCE_INSUFFICIENT
    future = plan(filters=filters(available_at=BASE + timedelta(seconds=1)))
    assert future.reason is RiskPlanReason.FILTER_EVIDENCE_FUTURE
    expired = plan(filters=filters(effective_until=BASE))
    assert expired.reason is RiskPlanReason.FILTER_INTERVAL_MISMATCH


def test_filter_contract_epoch_mismatch_fails_closed():
    result = plan(contract_epoch_id="ABC-PERP-E2")
    assert result.status is RiskPlanStatus.UNRESOLVED
    assert result.reason is RiskPlanReason.FILTER_INTERVAL_MISMATCH


def test_market_and_limit_quantity_filters_are_not_interchangeable():
    result = plan(quantity_filter_kind=QuantityFilterKind.LOT_SIZE)
    assert result.status is RiskPlanStatus.UNRESOLVED
    assert result.reason is RiskPlanReason.FILTER_KIND_MISMATCH


def test_minimum_quantity_and_notional_never_raise_risk_silently():
    too_large_minimum = plan(filters=filters(minimum_quantity=D("10")))
    assert too_large_minimum.status is RiskPlanStatus.SKIPPED
    assert too_large_minimum.reason is RiskPlanReason.BELOW_MIN_QUANTITY
    too_large_notional = plan(filters=filters(minimum_notional=D("1000")))
    assert too_large_notional.reason is RiskPlanReason.BELOW_MIN_NOTIONAL


def test_margin_constraint_is_explicit_not_reinterpreted_as_hard_cap_budget():
    result = plan(available_initial_margin=D("5"))
    assert result.status is RiskPlanStatus.SKIPPED
    assert result.reason is RiskPlanReason.TARGET_UTILIZATION_TOO_LOW
    assert result.projected_total_loss < result.target_risk_budget


def test_zero_available_margin_skips_without_division_or_quantity_fabrication():
    result = plan(available_initial_margin=D("0"))
    assert result.status is RiskPlanStatus.SKIPPED
    assert result.reason is RiskPlanReason.INSUFFICIENT_MARGIN
    assert result.quantity is None


def test_invalid_stop_geometry_is_skipped():
    assert plan(hard_stop_price=D("100")).reason is RiskPlanReason.INVALID_STOP_GEOMETRY
    assert plan(side=Side.SHORT, hard_stop_price=D("99")).reason is RiskPlanReason.INVALID_STOP_GEOMETRY


def test_numeric_result_is_independent_of_ambient_decimal_context():
    observed = []
    for precision in (6, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            result = plan(
                entry_price=D("100.123456789123456789"),
                hard_stop_price=D("95.987654321987654321"),
                filters=filters(quantity_step=D("0.000001")),
            )
        observed.append((
            result.status, result.reason, result.quantity, result.notional,
            result.projected_total_loss, result.target_utilization,
            result.plan_id,
        ))
    assert len(set(observed)) == 1


def test_extreme_numeric_input_returns_typed_unresolved():
    result = plan(entry_price=D("1E+1000"), hard_stop_price=D("95"))
    assert result.status is RiskPlanStatus.UNRESOLVED
    assert result.reason is RiskPlanReason.NUMERIC_UNCOMPUTABLE


def test_policy_identity_changes_with_every_risk_parameter():
    base = policy()
    for changed in (
        replace(base, target_equity_risk_fraction=D("0.003")),
        replace(base, hard_max_equity_risk_fraction=D("0.02")),
        replace(base, target_initial_margin_fraction=D("0.02")),
        replace(base, minimum_target_utilization=D("0.8")),
        replace(base, leverage=D("3")),
        replace(base, utilization_decimal_places=6),
        replace(base, numeric_policy_id="OTHER"),
    ):
        assert changed.variant_hash != base.variant_hash


def test_r4_module_has_no_runtime_or_exchange_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r4_risk.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = ("exchange", "binance", "main", "telegram", "risk_manager",
                 "requests", "httpx", "aiohttp", "socket", "sqlite3")
    forbidden_calls = ("create_order", "cancel_order", "set_leverage", "connect",
                       "write_text", "write_bytes", "now", "utcnow")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not item.name.lower().startswith(forbidden) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert not (node.module or "").lower().startswith(forbidden)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls
