from datetime import datetime, timedelta, timezone

from scripts.validate_frozen_regime_hypothesis import validate


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(day: int, value: float, *, regime: str = "MILD_RISK_ON", side: str = "BUY") -> dict:
    return {
        "side": side,
        "timestamp": (T0 + timedelta(days=day)).isoformat(),
        "directional_net_bps": value,
        "features": {
            "global_regime": regime,
            "return_24h_pct": 12.0,
        },
    }


def test_frozen_validator_filters_rule_and_checks_calendar_thirds():
    events = [_event(day, 10.0) for day in range(30)]
    events += [_event(35, -500.0, regime="RANGE"), _event(65, -500.0, side="SELL")]
    payload = {
        "method": {
            "study_start": T0.isoformat(),
            "study_end": (T0 + timedelta(days=90)).isoformat(),
            "entry": "next 5m open",
            "exit": "mark-to-market after 4h",
            "costs_bps": 12.0,
        },
        "events": events,
    }

    result = validate(payload)

    assert result["overall"]["events"] == 30
    assert result["criteria"]["minimum_30_events"] is True
    assert result["criteria"]["at_least_two_positive_calendar_thirds"] is False
    assert result["passed"] is False
