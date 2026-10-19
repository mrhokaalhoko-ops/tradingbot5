from __future__ import annotations

from pathlib import Path

import pytest

from datetime import datetime, timezone

from scripts.run_r4_gate_sidecar import (
    DiagnosticStore,
    GATES,
    MAINNET_USDM_PUBLIC_URL,
    closed_5m_boundary,
    gate_flags,
    scan_once,
    validate_public_endpoint,
)
from shadow_runtime.market import DEMO_USDM_PUBLIC_URL


class FakeMarket:
    base_url = DEMO_USDM_PUBLIC_URL


def test_gate_order_is_strictly_sequential():
    assert GATES == (
        "return_5m", "return_20m", "same_direction", "volume_expansion",
        "quote_volume_5m", "body_fraction", "directional_close", "atr_valid",
        "score_passed", "micro_gate_passed",
    )
    flags = gate_flags({"gates": {}, "micro": {}})
    assert list(flags) == list(GATES)


def test_mainnet_endpoint_rejected_when_demo_was_selected():
    market = FakeMarket()
    market.base_url = MAINNET_USDM_PUBLIC_URL
    with pytest.raises(RuntimeError, match="PUBLIC_ENDPOINT_MISMATCH"):
        scan_once(market, None, Path("."))  # type: ignore[arg-type]


def test_mainnet_public_endpoint_is_allowed_only_when_explicitly_selected():
    market = FakeMarket()
    market.base_url = MAINNET_USDM_PUBLIC_URL
    validate_public_endpoint(market, MAINNET_USDM_PUBLIC_URL)


def test_unknown_public_endpoint_is_rejected():
    market = FakeMarket()
    market.base_url = "https://example.invalid"
    with pytest.raises(RuntimeError, match="UNAPPROVED_PUBLIC_ENDPOINT"):
        validate_public_endpoint(market, "https://example.invalid")


def test_closed_boundary_uses_last_complete_5m_candle():
    opened, closed = closed_5m_boundary(datetime(2026, 10, 5, 12, 7, 42, tzinfo=timezone.utc))
    assert opened.isoformat() == "2026-10-05T12:00:00+00:00"
    assert closed.isoformat() == "2026-10-05T12:04:59.999000+00:00"


def test_unique_decision_is_restart_idempotent(tmp_path):
    store = DiagnosticStore(tmp_path / "sidecar.sqlite3")
    boundary = "2026-10-05T12:00:00+00:00"
    store.ensure_universe(
        boundary, "2026-10-05T12:04:59.999000+00:00", boundary, boundary, 0.0, ["AAAUSDT"]
    )
    row = {
        "decision_key": f"AAAUSDT|{boundary}", "universe_boundary": boundary,
        "symbol": "AAAUSDT", "closed_5m_open_time": boundary,
        "closed_5m_close_time": "2026-10-05T12:04:59.999000+00:00",
        "side": "LONG", "event_id": "event-1", "event_score": 3.5,
        "qualified": True, "gates": {name: True for name in GATES},
        "input_hash": "ABC", "parity_checked": True, "parity_ok": True,
        "event": {"event_id": "event-1"},
    }
    assert store.add_decision(row) is True
    assert store.add_decision(row) is False
    assert store.finalize_boundary(boundary, 1) is True
    summary = store.summary()
    assert summary["unique_funnel"]["scanned_unique"] == 1
    assert summary["unique_funnel"]["raw_LONG"] == 1
    assert summary["unique_funnel"]["B_LONG_after_top2"] == 1
    assert summary["unique_funnel"]["B_LONG_after_cooldown"] == 1
    assert summary["parity"] == {"checked": 1, "mismatches": 0}


def test_sidecar_has_no_order_or_credential_route():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "run_r4_gate_sidecar.py").read_text(encoding="utf-8").lower()
    banned = (
        "binance_api_key", "binance_api_secret", "create_order(", "place_order(",
        "new_order(", '"/fapi/v1/order"', '"/fapi/v2/account"',
        '"/fapi/v2/positionrisk"', '"/fapi/v1/openorders"',
    )
    assert all(token not in source for token in banned)


def test_candidate_replay_capture_has_only_public_get_routes():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "capture_mainnet_b_candidate_replay.py"
    ).read_text(encoding="utf-8").lower()
    banned = (
        "binance_api_key", "binance_api_secret", "requests.post",
        "requests.delete", "create_order(", '"/fapi/v1/order"',
        '"/fapi/v2/account"', '"/fapi/v2/positionrisk"',
        '"/fapi/v1/openorders"',
    )
    assert all(token not in source for token in banned)
