"""Strict JSON normalization shared by journals and runtime artifacts."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone


def _utc_iso(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return None


def normalize_json_value(value):
    """Return JSON-native values without silently stringifying unknown objects."""
    temporal = _utc_iso(value)
    if temporal is not None:
        return temporal

    # pandas.Timestamp and compatible timestamp classes expose to_pydatetime.
    to_datetime = getattr(value, "to_pydatetime", None)
    if callable(to_datetime):
        return _utc_iso(to_datetime())

    # numpy scalar types expose item().
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return normalize_json_value(converted)

    if isinstance(value, dict):
        return {str(key): normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_default(value):
    converted = normalize_json_value(value)
    if converted is value:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
    return converted
