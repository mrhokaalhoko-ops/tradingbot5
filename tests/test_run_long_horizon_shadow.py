import importlib.util
import base64
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest
import zlib

from strategy_v2.long_horizon_trend import LongHorizonTrendPolicy


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_long_horizon_shadow.py"
SPEC = importlib.util.spec_from_file_location("run_long_horizon_shadow", SCRIPT)
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


def _hourly(count=600):
    times = pd.date_range("2026-01-01", periods=count, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": times,
            "open": [100.0] * count,
            "high": [101.0] * count,
            "low": [99.0] * count,
            "close": [100.0] * count,
            "volume": [1.0] * count,
        }
    )


@pytest.mark.parametrize("variable", RUNNER.FORBIDDEN_CREDENTIAL_VARIABLES)
def test_refuses_any_configured_trading_credential(variable):
    RUNNER.assert_no_trading_credentials({})
    with pytest.raises(RuntimeError, match=variable):
        RUNNER.assert_no_trading_credentials({variable: "secret"})


def test_public_kline_parser_excludes_unfinished_rows():
    payload = [
        [0, "1", "2", "0.5", "1.5", "10", 3_599_999],
        [3_600_000, "1", "2", "0.5", "1.5", "10", 7_199_999],
    ]
    frame = RUNNER.parse_public_klines(
        payload, received_at=pd.Timestamp(4_000_000, unit="ms", tz="UTC")
    )
    assert len(frame) == 1
    assert frame.iloc[0].close == 1.5


def test_public_fetch_retains_compressed_raw_response():
    body = json.dumps([[0, "1", "2", "0.5", "1.5", "10", 3_599_999]]).encode()

    class Response:
        status = 200
        headers = {"Date": "Thu, 01 Jan 1970 01:00:01 GMT"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return body

    moments = iter(
        (
            pd.Timestamp("1970-01-01T01:00:00Z"),
            pd.Timestamp("1970-01-01T01:00:01Z"),
        )
    )
    frame, evidence = RUNNER.fetch_completed_hourly(
        "BTCUSDT", opener=lambda *args, **kwargs: Response(), clock=lambda: next(moments)
    )
    recovered = zlib.decompress(
        base64.b64decode(evidence["response_payload_zlib_base64"])
    )
    assert recovered == body
    assert evidence["response_sha256"] == sha256(body).hexdigest()
    assert len(frame) == 1


def test_http_rejection_becomes_typed_fail_closed_error():
    from urllib.error import HTTPError

    def rejected(*args, **kwargs):
        raise HTTPError("https://example.invalid", 451, "blocked", {}, None)

    with pytest.raises(RUNNER.PublicMarketDataError) as captured:
        RUNNER.fetch_completed_hourly("BTCUSDT", opener=rejected)
    assert captured.value.reason_code == "HTTP_451"


def test_journal_commit_recovers_state_and_detects_tampering(tmp_path):
    policy = LongHorizonTrendPolicy()
    journal = RUNNER.ShadowJournal(tmp_path, build_id="test-build", policy=policy)
    state = RUNNER.new_symbol_state("BTCUSDT", policy)
    state["last_processed_open_time"] = "2026-01-01T00:00:00+00:00"
    journal.append(symbol="BTCUSDT", state=state, events=(), evidence={"source": "test"})
    assert journal.load_states()["BTCUSDT"] == state

    line = json.loads(journal.path.read_text(encoding="utf-8"))
    line["state"]["completed_trades"] = 99
    journal.path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    reopened = RUNNER.ShadowJournal(tmp_path, build_id="test-build", policy=policy)
    with pytest.raises(ValueError, match="checksum mismatch"):
        reopened.load_states()


def test_partial_final_journal_record_is_ignored(tmp_path):
    policy = LongHorizonTrendPolicy()
    journal = RUNNER.ShadowJournal(tmp_path, build_id="test-build", policy=policy)
    state = RUNNER.new_symbol_state("ETHUSDT", policy)
    journal.append(symbol="ETHUSDT", state=state, events=(), evidence={"source": "test"})
    with journal.path.open("ab") as handle:
        handle.write(b'{"partial":')
    assert journal.load_states()["ETHUSDT"] == state
    second = dict(state)
    second["completed_trades"] = 1
    committed = journal.append(
        symbol="ETHUSDT", state=second, events=(), evidence={"source": "test"}
    )
    assert committed["recovery"]["discarded_partial_tail_bytes"] == len(b'{"partial":')
    assert journal.load_states()["ETHUSDT"] == second


def test_complete_final_record_without_newline_is_preserved(tmp_path):
    policy = LongHorizonTrendPolicy()
    journal = RUNNER.ShadowJournal(tmp_path, build_id="test-build", policy=policy)
    state = RUNNER.new_symbol_state("BTCUSDT", policy)
    journal.append(symbol="BTCUSDT", state=state, events=(), evidence={"source": "test"})
    journal.path.write_bytes(journal.path.read_bytes().rstrip(b"\n"))
    second = dict(state)
    second["completed_trades"] = 1
    journal.append(symbol="BTCUSDT", state=second, events=(), evidence={"source": "test"})
    assert len(journal._records()) == 2
    assert journal.load_states()["BTCUSDT"] == second


def test_cycle_writes_independent_states_without_execution_authority(tmp_path):
    policy = LongHorizonTrendPolicy()
    journal = RUNNER.ShadowJournal(tmp_path, build_id="test-build", policy=policy)

    def fetcher(symbol):
        return _hourly(), {
            "source": "synthetic",
            "symbol": symbol,
            "collector_received_at": "2026-01-26T00:00:30+00:00",
        }

    states, events = RUNNER.run_cycle(journal, fetcher=fetcher)
    assert set(states) == {"BTCUSDT", "ETHUSDT"}
    assert len(events) == 2
    assert all(event["execution_authority"] is False for event in events)
    snapshot = json.loads(journal.snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["execution_authority"] is False
    assert len(journal._records()) == 2


def test_cycle_keeps_symbols_isolated_when_public_feed_fails(tmp_path):
    policy = LongHorizonTrendPolicy()
    journal = RUNNER.ShadowJournal(tmp_path, build_id="test-build", policy=policy)

    def fetcher(symbol):
        if symbol == "BTCUSDT":
            raise RUNNER.PublicMarketDataError("HTTP_451", "blocked")
        return _hourly(), {
            "source": "synthetic",
            "symbol": symbol,
            "collector_received_at": "2026-01-26T00:00:30+00:00",
        }

    states, events = RUNNER.run_cycle(journal, fetcher=fetcher)
    assert set(states) == {"ETHUSDT"}
    assert [event["event_type"] for event in events] == [
        "OBSERVATION_ERROR",
        "WARMUP_ESTABLISHED",
    ]
    assert events[0]["reason"] == "HTTP_451"
    assert all(event["execution_authority"] is False for event in events)


def test_runner_has_no_execution_or_private_client_imports():
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden = (
        "core.exchange",
        "exchange_client",
        "position_manager",
        "state_manager",
        "main.py",
        "create_order",
        "place_order",
    )
    assert all(term not in source for term in forbidden)


def test_single_instance_lock_rejects_second_writer(tmp_path):
    with RUNNER.SingleInstanceLock(tmp_path):
        with pytest.raises(RuntimeError, match="already active"):
            with RUNNER.SingleInstanceLock(tmp_path):
                pass
