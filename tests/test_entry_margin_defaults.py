import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Config
from strategy.risk_manager import RiskManager, entry_sizing_balances


@pytest.fixture
def risk(monkeypatch):
    monkeypatch.setattr(Config, "RISK_PER_TRADE_PERCENT", 0.25)
    monkeypatch.setattr(Config, "LEVERAGE", 5)
    monkeypatch.setattr(Config, "MAX_NOTIONAL_BALANCE_PCT", 0.30)
    monkeypatch.setattr(Config, "MIN_SL_DISTANCE_PCT", 0.003)
    return RiskManager()


def size(risk, **overrides):
    args = dict(balance=100, current_price=10, custom_risk_percent=.0025,
                sl=9.8, leverage=5, entry_margin_percent=1, available_balance=100)
    args.update(overrides)
    return risk.calculate_position_size(**args)


def test_one_percent_means_margin_not_loss_at_stop(risk):
    amount = size(risk)
    assert amount == pytest.approx(.5)
    assert amount * 10 / 5 == pytest.approx(1)
    assert amount * (10 - 9.8) == pytest.approx(.1)


def test_cash_already_reserved_does_not_change_capital_basis(risk):
    equity, free = entry_sizing_balances({"USDT": {"total": 100, "free": 90}})
    assert size(risk, balance=equity, available_balance=free) == pytest.approx(.5)


def test_wider_stop_can_reduce_size_under_existing_loss_budget(risk):
    amount = size(risk, sl=9)
    assert amount == pytest.approx(.25)
    assert amount * (10 - 9) == pytest.approx(.25)


def test_insufficient_free_margin_cannot_use_reserved_capital(risk):
    assert size(risk, available_balance=.1) == pytest.approx(.0475)
    assert size(risk, available_balance=0) == 0


@pytest.mark.parametrize("field,value", [
    ("balance", float("nan")), ("current_price", float("inf")),
    ("entry_margin_percent", -1), ("entry_margin_percent", 101),
    ("entry_margin_percent", float("nan")), ("leverage", 0),
    ("sl", None), ("sl", 10), ("available_balance", float("inf")),
    ("custom_risk_percent", 0),
])
def test_invalid_inputs_do_not_produce_entry_quantity(risk, field, value):
    assert size(risk, **{field: value}) == 0


def test_legacy_research_sizing_remains_explicitly_available(risk):
    amount = size(risk, entry_margin_percent=None)
    assert amount == pytest.approx(1.25)


def test_balance_parser_prefers_actual_equity_and_preserves_zero_available():
    payload = {"info": {"totalMarginBalance": "101", "availableBalance": "0"},
               "USDT": {"total": 100, "free": 90}}
    assert entry_sizing_balances(payload) == (101, 0)
    assert entry_sizing_balances({"USDT": {"free": 90}}) == (0, 0)
    assert entry_sizing_balances(None) == (0, 0)
    assert entry_sizing_balances({"USDT": {"total": "nan", "free": 90}}) == (0, 0)


def test_clean_configuration_defaults_are_ten_five_and_one():
    fields = {"MAX_OPEN_TRADES", "TESTNET_MAX_OPEN_TRADES", "HARD_MAX_OPEN_TRADES",
              "LEVERAGE", "ENTRY_MARGIN_PERCENT", "RISK_PER_TRADE_PERCENT"}
    tree = ast.parse(Path("core/config.py").read_text(encoding="utf-8"))
    config = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Config")
    assignments = [n for n in config.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id in fields for t in n.targets)]
    scope = {"os": SimpleNamespace(getenv=lambda name, default=None: default)}
    exec(compile(ast.Module(body=assignments, type_ignores=[]), "core/config.py", "exec"), scope)
    assert scope["MAX_OPEN_TRADES"] == scope["TESTNET_MAX_OPEN_TRADES"] == scope["HARD_MAX_OPEN_TRADES"] == 10
    assert scope["LEVERAGE"] == 5
    assert scope["ENTRY_MARGIN_PERCENT"] == 1
    assert scope["RISK_PER_TRADE_PERCENT"] == .25


def test_scanner_and_sniper_use_margin_policy_and_equity(risk):
    # Execute the strict scanner, Hybrid-Demo scanner, ordinary Sniper and the
    # persisted SHORT confirmation sizing calls so runtime wiring is covered.
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "calculate_position_size"]
    assert len(calls) == 4
    context = {"risk_manager": risk, "usdt_total": 100, "entry_equity": 100, "usdt_free": 90,
               "optimal_entry": 10, "current_price": 10, "trade_risk_pct": .0025,
               "atr_val": .1, "fresh_atr": .1, "temp_sl": 9.8,
               "proposed_entry": 10, "proposal_sl": 9.8, "trade_leverage": 5,
               "v_order": {"trade_risk_pct": .0025, "trade_leverage": 5},
               "Config": SimpleNamespace(ENTRY_MARGIN_PERCENT=1)}
    for call in calls:
        assert eval(compile(ast.Expression(call), "main.py", "eval"), context) == pytest.approx(.5)


def test_saved_custom_values_keep_priority_over_defaults(tmp_path, monkeypatch):
    import utils.state_manager as state_module
    monkeypatch.setattr(state_module, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(Config, "MAX_OPEN_TRADES", 10)
    monkeypatch.setattr(Config, "TESTNET_MAX_OPEN_TRADES", 10)
    monkeypatch.setattr(Config, "HARD_MAX_OPEN_TRADES", 10)
    monkeypatch.setattr(Config, "LEVERAGE", 5)
    state = state_module.StateManager()
    assert state.get_max_open_trades() == 10
    assert state.get_leverage() == 5
    assert state.set_max_open_trades(4)
    assert state.set_leverage(3)
    restored = state_module.StateManager()
    assert restored.get_max_open_trades() == 4
    assert restored.get_leverage() == 3
