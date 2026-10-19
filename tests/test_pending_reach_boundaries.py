from scripts.diagnose_pending_reach_20260909 import measure


def row(t, low=102, high=103):
    return [t, low, high, low, high, 1]


def test_boundary_crossings_are_uncertain_and_never_counted_as_active_touch():
    bars = [row(0, 99, 101), row(60000), row(120000, 99, 101)]
    r = measure(bars, start=1000, end=150000, entry=100, tolerance=0.5)
    assert r["status"] == "NO_WHOLE_MINUTE_INTERSECTION"
    assert r["complete_minutes"] == 1
    assert len(r["boundary_intersections_uncertain"]) == 2
    assert r["closest_distance_bps"] == 150


def test_missing_middle_minute_prevents_a_claim_of_no_touch():
    r = measure([row(0), row(120000)], start=0, end=180000, entry=100, tolerance=0.5)
    assert r["status"] == "INCOMPLETE_OR_DUPLICATE_DATA"


def test_duplicate_minutes_rejected():
    r = measure([row(0), row(0)], start=0, end=60000, entry=100, tolerance=0.5)
    assert r["status"] == "INCOMPLETE_OR_DUPLICATE_DATA"


def test_exact_window_end_includes_only_the_bar_that_closed_then():
    r = measure([row(0), row(60000, 99, 101)], start=0, end=60000, entry=100, tolerance=0.5)
    assert r["status"] == "NO_WHOLE_MINUTE_INTERSECTION"
    r = measure([row(0, 99, 101)], start=0, end=60000, entry=100, tolerance=0.5)
    assert r["status"] == "OHLC_INTERSECTION"


def test_subminute_lifetime_does_not_turn_into_no_touch_claim():
    r = measure([row(0, 99, 101)], start=1000, end=59000, entry=100, tolerance=0.5)
    assert r["status"] == "NO_COMPLETE_MINUTES"


def test_invalid_ohlcv_is_not_usable_evidence():
    r = measure([[0, 99, 98, 97, 100, 1]], start=0, end=60000, entry=100, tolerance=0.5)
    assert r["status"] == "INVALID_OHLCV"


def test_submillisecond_creation_never_admits_a_predecision_minute():
    from scripts.diagnose_pending_reach_20260909 import ms
    start = ms("1970-01-01T00:00:00.000001Z", ceiling=True)
    end = ms("1970-01-01T00:01:00.000001Z")
    assert (start, end) == (1, 60000)
    r = measure([row(0, 99, 101)], start=start, end=end, entry=100, tolerance=0.5)
    assert r["status"] == "NO_COMPLETE_MINUTES"
