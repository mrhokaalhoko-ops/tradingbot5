from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

import pytest

from scripts.analyze_active_altcoin_shadow import (
    load_shadow_events,
    summarize_shadow_events,
)


def _event(event_id, timestamp, status, *, side=None, net_r=None, plan_id=None):
    details = {"execution_enabled": False}
    if status.startswith("OUTCOME_"):
        details.update(
            {
                "plan": {"plan_id": plan_id, "side": side},
                "outcome": {"status": status.removeprefix("OUTCOME_"), "net_r": net_r},
            }
        )
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "stage": "ACTIVE_ALTCOIN_SHADOW",
        "status": status,
        "symbol": "AAAUSDT",
        "virtual_order_id": plan_id,
        "details": details,
    }


def test_loader_reads_zip_and_deduplicates_event_ids(tmp_path: Path):
    event = _event("same", "2026-09-07T00:00:00Z", "SETUP_CREATED")
    archive_path = tmp_path / "runtime.zip"
    payload = json.dumps(event) + "\n" + json.dumps(event) + "\nnot-json\n"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("decision_audit_24h.jsonl", payload)

    events, diagnostics = load_shadow_events([archive_path])

    assert len(events) == 1
    assert diagnostics["duplicates"] == 1
    assert diagnostics["invalid_json_lines"] == 1


def test_summary_reports_performance_sides_and_drawdown():
    events = [
        _event("s", "2026-09-07T00:00:00Z", "SETUP_CREATED"),
        _event(
            "a",
            "2026-09-07T00:01:00Z",
            "OUTCOME_TP_FIRST",
            side="LONG",
            net_r=1.5,
            plan_id="p1",
        ),
        _event(
            "b",
            "2026-09-07T00:02:00Z",
            "OUTCOME_SL_FIRST",
            side="SHORT",
            net_r=-1.0,
            plan_id="p2",
        ),
        _event(
            "c",
            "2026-09-07T00:03:00Z",
            "OUTCOME_TIME_EXIT",
            side="SHORT",
            net_r=0.25,
            plan_id="p3",
        ),
    ]

    report = summarize_shadow_events(events)

    assert report["unique_terminal_outcomes"] == 3
    assert report["performance"]["expectancy_r"] == pytest.approx(0.25)
    assert report["performance"]["profit_factor"] == pytest.approx(1.75)
    assert report["performance"]["max_drawdown_r"] == pytest.approx(1.0)
    assert report["side_performance"]["LONG"]["expectancy_r"] == pytest.approx(1.5)
    assert report["side_performance"]["SHORT"]["expectancy_r"] == pytest.approx(-0.375)
    assert report["reference_gate"]["eligible_for_human_promotion_review"] is False


def test_cutoff_and_duplicate_terminal_plan_are_not_counted_twice():
    events = [
        _event(
            "old",
            "2026-09-06T23:59:00Z",
            "OUTCOME_SL_FIRST",
            side="LONG",
            net_r=-1,
            plan_id="old",
        ),
        _event(
            "one",
            "2026-09-07T00:01:00Z",
            "OUTCOME_TP_FIRST",
            side="LONG",
            net_r=1.5,
            plan_id="p1",
        ),
        _event(
            "two",
            "2026-09-07T00:02:00Z",
            "OUTCOME_TP_FIRST",
            side="LONG",
            net_r=1.5,
            plan_id="p1",
        ),
    ]

    report = summarize_shadow_events(
        events, not_before=datetime(2026, 9, 7, tzinfo=timezone.utc)
    )

    assert report["unique_terminal_outcomes"] == 1
    assert report["duplicate_terminal_outcomes"] == 1
    assert report["performance"]["total_r"] == pytest.approx(1.5)
