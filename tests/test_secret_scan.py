from scripts.secret_scan import scan_text


def test_secret_scan_reports_location_without_secret_value():
    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
    findings = scan_text("sample.txt", f"token={secret}\n")
    assert [(item.path, item.line, item.rule) for item in findings] == [
        ("sample.txt", 1, "github-token")
    ]
    assert secret not in repr(findings)


def test_secret_scan_ignores_documented_placeholders():
    assert scan_text(".env.example", "BINANCE_API_KEY=your_binance_api_key_here") == []
