from pathlib import Path


def test_mini_app_exposes_separate_conditional_watch_plans():
    html = Path("webapp/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'id="watchPlansView"' in html
    assert 'id="watchPlansContent"' in html
    assert "خطط المراقبة المشروطة" in html
    assert "لا تُفعّل الخطة إلا بعد تحقق الآتي" in html
    assert "تُلغى الفكرة فوراً إذا" in html
    assert "منطقة الدخول" in html
    assert "الهدف ${index + 1}" in html
    assert "وقف الخسارة / الإبطال" in html
    assert "لماذا ينتظر البوت؟" in html
    assert "درجة الأدلة" in html
    assert "متوسط الأدلة" in html
    assert "Veto أمان" in html
    assert "analysis_summary_ar" in html
    assert "maximum-scale=1.0" not in html


def test_plan_view_is_not_replaced_during_automatic_refresh():
    html = Path("webapp/templates/dashboard.html").read_text(encoding="utf-8")

    assert "renderedPlansScanTime" in html
    assert "pendingPlansSnapshot = scan" in html
    assert "وصل مسح أحدث" in html
    assert "applyPendingPlanUpdate" in html
    assert "activeView !== 'plans'" in html
    assert "setInterval(() => loadDashboard({ showLoading: false }), 30_000)" in html
    assert 'min-height: 44px' in html
