"""Pure R4 aggregate portfolio gate tests."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from itertools import combinations
from pathlib import Path

import pytest

from strategy_v2.enums import Side
from strategy_v2.events import canonical_sha256
from strategy_v2.models import ThesisId
from strategy_v2.r4_portfolio import (
    DependencySnapshot,
    ExposureState,
    PairCorrelation,
    PortfolioGateReason,
    PortfolioGateStatus,
    PortfolioPolicySpec,
    PositionExposure,
    SymbolBeta,
    evaluate_portfolio_gate,
)
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
BASE = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
COMMIT = "562c5201426ab0ac31215448bd0a2a840d9c7a53"


def candidate(symbol="ABCUSDT", side=Side.LONG):
    source_hash = canonical_sha256((symbol, "filters")).lower()
    filters = VenueFilterSnapshot(
        symbol, f"{symbol}-PERP-E1", "filters-1", source_hash,
        QuantityFilterKind.MARKET_LOT_SIZE, D("0.001"), D("0.001"),
        D("1000"), D("5"), D("1000000"), BASE - timedelta(days=1),
        BASE + timedelta(days=1), BASE - timedelta(minutes=2),
        BASE - timedelta(minutes=1),
        MetadataProvenance.EXACT_EFFECTIVE_INTERVAL,
    )
    costs = CostModelSpec("costs", D("0.0005"), D("0.0005"), D("0.0005"),
                          D("0.001"), D("0.0002"), D("0.0003"))
    policy = RiskPolicySpec(
        COMMIT, "risk", "r4", D("0.0025"), D("0.01"), D("0.01"),
        D("0.70"), D("5"), 8, "exact",
    )
    return build_risk_plan(
        thesis_id=ThesisId(f"thesis-{symbol}"), side=side, symbol=symbol,
        contract_epoch_id=f"{symbol}-PERP-E1", entry_price=D("100"),
        hard_stop_price=D("95") if side is Side.LONG else D("105"),
        account_equity=D("10000"), available_initial_margin=D("10000"),
        quantity_filter_kind=QuantityFilterKind.MARKET_LOT_SIZE,
        filters=filters, costs=costs, policy=policy, decision_at=BASE,
    )


def exposure(exposure_id, symbol, *, side=Side.LONG, cluster="ALTS",
             state=ExposureState.OPEN_PROTECTED, notional="200",
             loss="10", available=BASE - timedelta(seconds=1)):
    if state in (ExposureState.PENDING_PROTECTION, ExposureState.UNKNOWN):
        loss = notional
    return PositionExposure(
        exposure_id, symbol, f"{symbol}-PERP-E1", cluster, side, state,
        ThesisId(f"thesis-{exposure_id}"),
        D(notional), D(loss), D("40"), available - timedelta(seconds=1),
        available, (f"fill-{exposure_id}", f"protection-{exposure_id}"),
    )


def dependencies(symbols=("ABCUSDT",), *, observations=200,
                 available=BASE - timedelta(seconds=1), missing_pair=None,
                 missing_beta=None):
    ordered = tuple(sorted(set(symbols)))
    pairs = tuple(
        PairCorrelation(a, b, D("0.75"))
        for a, b in combinations(ordered, 2)
        if (a, b) != missing_pair
    )
    betas = tuple(SymbolBeta(symbol, D("1.2")) for symbol in ordered
                  if symbol != missing_beta)
    return DependencySnapshot(
        "deps-1", "BTCUSDT", BASE - timedelta(days=30),
        BASE - timedelta(minutes=1), BASE - timedelta(seconds=2),
        available, observations, pairs, betas,
        canonical_sha256((ordered, observations, "raw-deps")).lower(),
    )


def policy(**overrides):
    values = dict(
        source_commit=COMMIT, policy_id="PORTFOLIO_RISK_V1", rule_version="r4",
        max_positions=5, max_aggregate_stop_risk_fraction=D("0.02"),
        max_gross_exposure_fraction=D("1"),
        max_absolute_net_exposure_fraction=D("0.75"),
        max_cluster_stop_risk_fraction=D("0.015"),
        max_absolute_beta_exposure_fraction=D("1"),
        minimum_dependency_observations=100,
        block_on_unprotected_exposure=True, numeric_policy_id="EXACT_FRACTION_V1",
    )
    values.update(overrides)
    return PortfolioPolicySpec(**values)


def gate(*, positions=(), deps=None, risk=None, cluster="ALTS", gate_policy=None):
    risk = risk or candidate()
    symbols = (risk.symbol, *(item.symbol for item in positions))
    return evaluate_portfolio_gate(
        candidate=risk, candidate_cluster_id=cluster, existing=positions,
        account_equity=D("10000"),
        dependencies=deps if deps is not None else dependencies(symbols),
        policy=gate_policy or policy(), decision_at=BASE,
    )


def test_empty_portfolio_with_point_in_time_dependencies_is_approved():
    result = gate()
    assert result.status is PortfolioGateStatus.APPROVED
    assert result.position_count == 1
    assert result.gross_exposure > 0
    assert result.aggregate_stop_risk > 0
    assert result.execution_authority is False


def test_long_short_netting_changes_net_not_gross_or_stop_risk():
    long = gate(positions=(exposure("one", "XYZUSDT", side=Side.LONG),))
    short = gate(positions=(exposure("one", "XYZUSDT", side=Side.SHORT),))
    assert long.gross_exposure == short.gross_exposure
    assert long.aggregate_stop_risk == short.aggregate_stop_risk
    assert abs(short.net_exposure) < abs(long.net_exposure)


def test_unknown_and_unprotected_exposures_block_new_risk():
    unknown = gate(positions=(exposure("one", "XYZUSDT", state=ExposureState.UNKNOWN),))
    assert unknown.reason is PortfolioGateReason.UNKNOWN_EXPOSURE_PRESENT
    unprotected = gate(positions=(
        exposure("one", "XYZUSDT", state=ExposureState.PENDING_PROTECTION),
    ))
    assert unprotected.reason is PortfolioGateReason.UNPROTECTED_EXPOSURE_PRESENT


def test_unprotected_contract_cannot_understate_worst_case_loss():
    with pytest.raises(ValueError, match="full notional"):
        PositionExposure(
            "one", "XYZUSDT", "XYZ-E1", "ALTS", Side.LONG,
            ExposureState.PENDING_PROTECTION, ThesisId("thesis-one"),
            D("200"), D("10"), D("40"),
            BASE - timedelta(minutes=2), BASE - timedelta(minutes=1),
            ("fill",),
        )


def test_duplicate_exposure_identity_and_same_thesis_fail_closed():
    one = exposure("one", "XYZUSDT")
    duplicate_record = gate(positions=(one, one))
    assert duplicate_record.status is PortfolioGateStatus.UNRESOLVED
    assert duplicate_record.reason is PortfolioGateReason.EXPOSURE_IDENTITY_CONFLICT

    same_thesis = replace(one, exposure_id="different",
                          thesis_id=ThesisId("thesis-ABCUSDT"))
    duplicate_thesis = gate(positions=(same_thesis,))
    assert duplicate_thesis.status is PortfolioGateStatus.BLOCKED
    assert duplicate_thesis.reason is PortfolioGateReason.DUPLICATE_THESIS_EXPOSURE


def test_dependency_snapshot_is_mandatory_and_causal():
    assert gate(deps=None).status is PortfolioGateStatus.APPROVED
    missing = evaluate_portfolio_gate(
        candidate=candidate(), candidate_cluster_id="ALTS", existing=(),
        account_equity=D("10000"), dependencies=None, policy=policy(),
        decision_at=BASE,
    )
    assert missing.reason is PortfolioGateReason.DEPENDENCY_EVIDENCE_MISSING
    future = gate(deps=dependencies(available=BASE + timedelta(seconds=1)))
    assert future.reason is PortfolioGateReason.DEPENDENCY_EVIDENCE_FUTURE


def test_dependency_coverage_and_observation_count_fail_closed():
    positions = (exposure("one", "XYZUSDT"),)
    weak = gate(positions=positions, deps=dependencies(
        ("ABCUSDT", "XYZUSDT"), observations=20,
    ))
    assert weak.reason is PortfolioGateReason.DEPENDENCY_HISTORY_INSUFFICIENT
    no_pair = gate(positions=positions, deps=dependencies(
        ("ABCUSDT", "XYZUSDT"), missing_pair=("ABCUSDT", "XYZUSDT"),
    ))
    assert no_pair.reason is PortfolioGateReason.CORRELATION_MATRIX_INCOMPLETE
    no_beta = gate(positions=positions, deps=dependencies(
        ("ABCUSDT", "XYZUSDT"), missing_beta="XYZUSDT",
    ))
    assert no_beta.reason is PortfolioGateReason.BETA_VECTOR_INCOMPLETE


@pytest.mark.parametrize("override,expected", [
    ({"max_positions": 1}, PortfolioGateReason.POSITION_COUNT_LIMIT),
    ({"max_aggregate_stop_risk_fraction": D("0.003")}, PortfolioGateReason.AGGREGATE_STOP_RISK_LIMIT),
    ({"max_gross_exposure_fraction": D("0.03")}, PortfolioGateReason.GROSS_EXPOSURE_LIMIT),
    ({"max_absolute_net_exposure_fraction": D("0.03")}, PortfolioGateReason.NET_EXPOSURE_LIMIT),
    ({"max_cluster_stop_risk_fraction": D("0.003")}, PortfolioGateReason.CLUSTER_RISK_LIMIT),
    ({"max_absolute_beta_exposure_fraction": D("0.03")}, PortfolioGateReason.BETA_WEIGHTED_EXPOSURE_LIMIT),
])
def test_each_portfolio_limit_has_a_typed_block_reason(override, expected):
    positions = (exposure("one", "XYZUSDT", notional="200", loss="10"),)
    result = gate(positions=positions, gate_policy=policy(**override))
    assert result.status is PortfolioGateStatus.BLOCKED
    assert result.reason is expected


def test_non_ready_candidate_cannot_enter_portfolio_gate():
    invalid = candidate()
    invalid = replace(invalid, status=RiskPlanStatus.SKIPPED,
                      reason=RiskPlanReason.ZERO_SIZED)
    result = gate(risk=invalid)
    assert result.status is PortfolioGateStatus.UNRESOLVED
    assert result.reason is PortfolioGateReason.CANDIDATE_RISK_NOT_READY


def test_portfolio_equity_must_match_the_equity_used_for_candidate_sizing():
    result = evaluate_portfolio_gate(
        candidate=candidate(), candidate_cluster_id="ALTS", existing=(),
        account_equity=D("9000"), dependencies=dependencies(),
        policy=policy(), decision_at=BASE,
    )
    assert result.status is PortfolioGateStatus.UNRESOLVED
    assert result.reason is PortfolioGateReason.ACCOUNT_EQUITY_MISMATCH


def test_portfolio_metrics_are_decimal_context_independent():
    positions = (exposure("one", "XYZUSDT", notional="123.456789", loss="7.654321"),)
    observed = []
    for precision in (6, 9, 28, 50):
        with localcontext() as context:
            context.prec = precision
            result = gate(positions=positions)
        observed.append((
            result.status, result.reason, result.gross_exposure,
            result.net_exposure, result.aggregate_stop_risk,
            result.beta_weighted_net_exposure, result.result_id,
        ))
    assert len(set(observed)) == 1


def test_portfolio_module_has_no_runtime_or_exchange_surface():
    path = Path(__file__).parents[1] / "strategy_v2" / "r4_portfolio.py"
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
