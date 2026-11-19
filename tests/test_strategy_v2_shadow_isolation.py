import copy
import inspect
import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.config import Config
from strategy.strategy_v2_shadow_storage import ShadowEvidenceStore
from strategy.trailing_stop import TrailingStopManager


def _bars():
    index = pd.date_range("2026-10-20T00:00:00Z", periods=25, freq="15min")
    return pd.DataFrame({
        "high": [101 + (i % 4) for i in range(25)],
        "low": [99 - (i % 4) for i in range(25)],
        "close": [100 + (i % 3) for i in range(25)],
    }, index=index)


def _state():
    state = MagicMock()
    state.get_trailing_trades.return_value = {}
    state.get_entry_metadata.return_value = {"trade_id": "trade-1"}
    return state


def _client():
    client = MagicMock()
    client.fetch_ohlcv.return_value = _bars()
    client.get_price_tick_size.return_value = 0.1
    return client


def _manager_with_trade():
    state = _state()
    manager = TrailingStopManager(state_manager=state)
    manager._trades["BTC/USDT"] = {
        "side": "buy",
        "entry": 100.0,
        "best_price": 100.0,
        "current_sl": 95.0,
        "initial_sl": 95.0,
        "amount": 0.1,
        "full_amount": 0.1,
        "step_trail_active": False,
        "trailing_active": False,
    }
    return manager, state


def test_shadow_on_before_v1_activation_does_not_mutate_trade_beyond_v1():
    client_off, client_on = _client(), _client()
    manager_off, _ = _manager_with_trade()
    manager_on, _ = _manager_with_trade()
    with tempfile.TemporaryDirectory() as directory, patch.object(
        Config, "TRADE_MANAGEMENT_V2_ENABLED", False
    ), patch.object(Config, "STRATEGY_V2_SHADOW_DIR", directory), patch.object(
        Config, "STRATEGY_V2_SHADOW_MAX_BYTES", 4 * 1024 * 1024
    ):
        with patch.object(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", False):
            assert manager_off.update("BTC/USDT", 101.0, client_off) is None
        with patch.object(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", True):
            assert manager_on.update("BTC/USDT", 101.0, client_on) is None
    assert manager_on._trades["BTC/USDT"] == manager_off._trades["BTC/USDT"]
    assert manager_on._trades["BTC/USDT"]["trailing_active"] is False
    client_on.create_order.assert_not_called()
    client_on.close_position.assert_not_called()


def test_shadow_storage_failure_leaves_v1_trade_unchanged():
    manager, _ = _manager_with_trade()
    before = copy.deepcopy(manager._trades["BTC/USDT"])
    client = _client()
    with patch.object(Config, "TRADE_MANAGEMENT_V2_ENABLED", False), patch.object(
        Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", True
    ), patch.object(
        manager, "_record_v2_management_shadow", side_effect=OSError("disk full")
    ):
        assert manager.update("BTC/USDT", 101.0, client) is None
    expected = copy.deepcopy(before)
    expected["best_price"] = 101.0  # the exact authoritative V1 mutation
    assert manager._trades["BTC/USDT"] == expected
    assert manager._trades["BTC/USDT"]["trailing_active"] is False
    client.create_order.assert_not_called()
    client.close_position.assert_not_called()


def test_proposal_collector_failure_does_not_mutate_v1_inputs():
    import main
    proposal = {"proposal_id": "proposal-1", "symbol": "BTC/USDT"}
    coin_data = {"_ta_context": {"setup_anchor": None}, "decision": "BUY"}
    before = copy.deepcopy((proposal, coin_data))
    with patch.object(Config, "STRATEGY_V2_SHADOW_EVIDENCE_ENABLED", True), patch.object(
        main, "collect_authoritative_proposal_evidence", side_effect=OSError("disk full")
    ), patch.object(main, "audit_decision_event", return_value=True):
        assert main._collect_strategy_v2_proposal_shadow(
            proposal=proposal, coin_data=coin_data, audit_ids={"trade_id": "trade-1"}
        ) is False
    assert (proposal, coin_data) == before


def test_global_retention_is_enforced_across_trades():
    with tempfile.TemporaryDirectory() as directory:
        store = ShadowEvidenceStore(
            directory,
            limits={"decisions": 3, "management": 5, "outcomes": 3,
                    "theses": 3, "resets": 3},
            max_bytes=4 * 1024 * 1024,
        )
        for index in range(12):
            assert store.append_strategy_v2_management_shadow(
                f"trade-{index % 4}",
                {
                    "execution_authority": False,
                    "timestamp": f"2026-10-20T12:{index:02d}:00Z",
                    "structure_confirmed_at": f"2026-10-20T11:{index:02d}:00Z",
                    "candidate_stop": 100 + index,
                    "reason": "TEST",
                },
            )
        stats = store.stats()
        assert stats["counts"]["management"] == 5
        assert stats["metrics"]["records_pruned"] >= 7
        assert stats["disk_bytes"] < stats["max_bytes"]


def test_restart_recovery_and_duplicate_management_are_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        record = {
            "execution_authority": False,
            "timestamp": "2026-10-20T12:00:00Z",
            "structure_confirmed_at": "2026-10-20T11:45:00Z",
            "candidate_stop": 101.0,
            "reason": "MEANINGFUL_MOVE",
        }
        first = ShadowEvidenceStore(directory)
        assert first.append_strategy_v2_management_shadow("trade-1", record)
        assert first.append_strategy_v2_management_shadow("trade-1", record)
        first.close()
        restarted = ShadowEvidenceStore(directory)
        assert restarted.stats()["quick_check"] == "ok"
        assert restarted.stats()["counts"]["management"] == 1
        assert restarted.get_latest_strategy_v2_management_shadow("trade-1") == record
        restarted.close()


def test_shadow_state_and_thesis_survive_restart_and_outcome_is_separate():
    temporary = tempfile.TemporaryDirectory()
    try:
        first = ShadowEvidenceStore(temporary.name)
        decision = {
            "proposal_id": "restart-proposal",
            "decision_timestamp": "2026-10-20T12:00:00Z",
            "symbol": "BTC/USDT",
            "decision_record_hash": "RESTART-HASH",
            "execution_authority": False,
        }
        assert first.save_strategy_v2_shadow_decision(decision)
        assert first.record_closed_thesis(
            "BTC/USDT", "restart-thesis", side="BUY",
            closed_at="2026-10-20T12:30:00Z",
        )
        assert first.record_strategy_v2_shadow_outcome(
            "restart-proposal",
            {
                "trade_id": "restart-trade",
                "recorded_at": "2026-10-20T13:00:00Z",
                "realized_R": 1.2,
                "execution_authority": False,
            },
        )
        first.close()

        restarted = ShadowEvidenceStore(temporary.name)
        assert restarted.get_strategy_v2_shadow_decision(
            "restart-proposal"
        )["decision_record_hash"] == "RESTART-HASH"
        assert restarted.get_latest_closed_thesis(
            "BTC/USDT", "BUY"
        )["thesis_id"] == "restart-thesis"
        with restarted._connection() as connection:
            outcome = json.loads(connection.execute(
                "SELECT payload FROM outcomes WHERE proposal_id=?",
                ("restart-proposal",),
            ).fetchone()[0])
        assert outcome["realized_R"] == 1.2
        assert "realized_R" not in restarted.get_strategy_v2_shadow_decision(
            "restart-proposal"
        )
        restarted.close()
    finally:
        temporary.cleanup()


def test_close_releases_sqlite_file_and_tracked_connection_on_windows():
    temporary = tempfile.TemporaryDirectory()
    database_path = os.path.join(temporary.name, "strategy_v2_shadow.sqlite3")
    try:
        store = ShadowEvidenceStore(temporary.name)
        direct_connection = store._connect()
        direct_connection.execute("SELECT 1").fetchone()
        store.close()

        assert store.closed is True
        with pytest.raises(sqlite3.ProgrammingError):
            direct_connection.execute("SELECT 1")

        moved_path = database_path + ".released"
        os.replace(database_path, moved_path)
        os.replace(moved_path, database_path)
    finally:
        temporary.cleanup()


def test_close_twice_is_safe_and_same_database_can_reopen():
    with tempfile.TemporaryDirectory() as directory:
        store = ShadowEvidenceStore(directory)
        assert store.stats()["quick_check"] == "ok"
        store.close()
        store.close()

        with ShadowEvidenceStore(directory) as reopened:
            assert reopened.stats()["quick_check"] == "ok"
        assert reopened.closed is True


def test_temporary_directory_and_wal_sidecars_release_immediately_after_close():
    temporary = tempfile.TemporaryDirectory()
    try:
        store = ShadowEvidenceStore(temporary.name)
        with store._connection() as connection:
            connection.execute(
                "INSERT INTO metrics(key,value) VALUES('wal-release',1)"
            )
        store.close()

        base = os.path.join(temporary.name, "strategy_v2_shadow.sqlite3")
        for candidate in (base, base + "-wal", base + "-shm"):
            if not os.path.exists(candidate):
                continue
            moved = candidate + ".released"
            os.replace(candidate, moved)
            os.replace(moved, candidate)
    finally:
        temporary.cleanup()


def test_storage_hard_ceiling_prunes_globally():
    with tempfile.TemporaryDirectory() as directory:
        store = ShadowEvidenceStore(
            directory,
            limits={"management": 5_000},
            max_bytes=768 * 1024,
        )
        for index in range(400):
            assert store.append_strategy_v2_management_shadow(
                f"trade-{index % 20}",
                {
                    "execution_authority": False,
                    "timestamp": f"2026-10-20T12:{index % 60:02d}:{index % 60:02d}Z",
                    "structure_confirmed_at": "2026-10-20T11:45:00Z",
                    "candidate_stop": 100 + index / 1000,
                    "reason": "CEILING_TEST",
                    "payload": "x" * 4096,
                },
            )
        stats = store.stats()
        assert stats["disk_bytes"] <= stats["max_bytes"]
        assert stats["counts"]["management"] < 400
        assert stats["metrics"].get("size_ceiling_pruned", 0) > 0


def test_shadow_store_has_no_exchange_or_v1_state_write_surface():
    import strategy.strategy_v2_shadow_storage as storage_module
    source = inspect.getsource(storage_module)
    for forbidden in (
        "create_order(", "cancel_order(", "set_leverage(",
        "save_order_intent(", "replace_sl_order(", "close_position(",
        "utils.state_manager", "StateManager(",
    ):
        assert forbidden not in source


def test_shadow_store_writes_only_under_configured_directory():
    with tempfile.TemporaryDirectory() as directory:
        store = ShadowEvidenceStore(directory)
        assert os.path.commonpath([store.path, directory]) == os.path.abspath(directory)
        assert os.path.basename(store.path) == "strategy_v2_shadow.sqlite3"
        assert store.stats()["counts"] == {
            "decisions": 0, "management": 0, "outcomes": 0,
            "theses": 0, "resets": 0,
        }


def test_no_strategy_v2_runtime_authority_flags_were_added():
    # Shadow-only cleanup introduces one collection flag; runtime V2 flags in
    # the base remain unchanged and are never consulted by the collector.
    import strategy.strategy_v2_evidence as evidence_module
    source = inspect.getsource(evidence_module)
    for forbidden in (
        "CAUSAL_TARGET_ENGINE_V2", "STRUCTURAL_STOP_V2_ENABLED",
        "THESIS_REENTRY_V2", "TRADE_MANAGEMENT_V2_ENABLED",
    ):
        assert forbidden not in source


def test_pure_research_helpers_have_no_exchange_write_surface():
    import strategy.strategy_v2_research as research_module
    source = inspect.getsource(research_module)
    for forbidden in (
        "create_order(", "cancel_order(", "set_leverage(",
        "save_order_intent(", "replace_sl_order(", "close_position(",
    ):
        assert forbidden not in source


def test_shadow_collection_flag_defaults_off():
    assert Config.STRATEGY_V2_SHADOW_EVIDENCE_ENABLED is False
