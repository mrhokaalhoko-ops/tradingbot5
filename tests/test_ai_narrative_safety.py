import importlib
import sys


def _sanitize(value):
    module = importlib.import_module("engines.ai_engine")
    try:
        return module._sanitize_analysis_narrative(value)
    finally:
        # Keep the production lazy-loading contract observable by later tests.
        sys.modules.pop("engines.ai_engine", None)


def test_safe_explanation_is_labeled_non_executable():
    result = _sanitize(
        "الاتجاه الأعلى إيجابي، لكن بيانات الحجم غير مكتملة."
    )

    assert "لا تملك صلاحية تنفيذ" in result
    assert "Risk Engine" in result


def test_generated_trade_plan_fails_closed():
    assert _sanitize("القرار: LONG\nTP: 12\nSL: 9") is None
    assert _sanitize("الرافعة المناسبة 10x") is None


def test_empty_provider_response_fails_closed():
    assert _sanitize(None) is None
    assert _sanitize("   ") is None
