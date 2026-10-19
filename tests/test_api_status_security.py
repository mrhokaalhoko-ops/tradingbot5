import os
from unittest.mock import patch

from utils.api_status_monitor import APIStatusMonitor


def test_gemini_health_key_never_enters_url_or_report():
    secret = "AIzaSyExampleSecretValueThatMustNeverLeak"
    monitor = APIStatusMonitor()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, "kwargs": kwargs})
        raise RuntimeError(f"transport failed for token={secret}")

    with patch.dict(os.environ, {"GEMINI_API_KEY": secret}, clear=False), patch.object(
        monitor, "_request_json", side_effect=fake_request
    ), patch.object(type(monitor), "_mask_present", return_value=True), patch(
        "utils.api_status_monitor.Config.GEMINI_API_KEYS", [secret]
    ), patch("utils.api_status_monitor.Config.GEMINI_API_KEY", secret):
        result = monitor.check_gemini()
        report = monitor.format_report({
            "summary": {
                "overall": "DOWN", "ok": 0, "warn": 0,
                "down": 1, "skip": 0, "critical_down": [],
            },
            "results": [result],
            "checked_at": "2026-08-21 00:00:00",
        })

    assert secret not in captured["url"]
    assert captured["kwargs"]["headers"]["x-goog-api-key"] == secret
    assert secret not in result["reason"]
    assert secret not in report
    assert "AIza****" in result["reason"]
