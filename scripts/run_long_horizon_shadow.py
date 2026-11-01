"""Standalone BTC/ETH long-horizon research shadow.

This process is intentionally independent from the trading bot.  It uses only
Binance's public USD-M Futures kline endpoint, owns separate append-only
storage, and never imports execution, exchange, strategy-runtime, or position
management modules.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zlib

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_v2.long_horizon_shadow import advance_symbol_state, new_symbol_state
from strategy_v2.long_horizon_trend import LongHorizonTrendPolicy, aggregate_hourly_to_4h


PUBLIC_KLINE_ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
FORBIDDEN_CREDENTIAL_VARIABLES = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_DEMO_API_KEY",
    "BINANCE_DEMO_API_SECRET",
    "BINANCE_SECRET_KEY",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "FUTURES_API_KEY",
    "FUTURES_API_SECRET",
)


class PublicMarketDataError(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def assert_no_trading_credentials(environment: Mapping[str, str]) -> None:
    present = sorted(
        name for name in FORBIDDEN_CREDENTIAL_VARIABLES if environment.get(name, "").strip()
    )
    if present:
        raise RuntimeError(
            "standalone shadow refuses to start with trading credentials present: "
            + ", ".join(present)
        )


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_public_klines(payload: object, *, received_at) -> pd.DataFrame:
    if not isinstance(payload, list):
        raise ValueError("public kline response must be a list")
    observed = pd.Timestamp(received_at)
    observed = observed.tz_localize("UTC") if observed.tzinfo is None else observed.tz_convert("UTC")
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 7:
            raise ValueError("malformed public kline row")
        open_time = pd.to_datetime(int(item[0]), unit="ms", utc=True)
        exchange_close_time = pd.to_datetime(int(item[6]), unit="ms", utc=True)
        if exchange_close_time > observed:
            continue
        numeric = [float(item[index]) for index in (1, 2, 3, 4, 5)]
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("non-finite public kline numeric value")
        rows.append(
            {
                "open_time": open_time,
                "open": numeric[0],
                "high": numeric[1],
                "low": numeric[2],
                "close": numeric[3],
                "volume": numeric[4],
            }
        )
    return pd.DataFrame(rows, columns=("open_time", "open", "high", "low", "close", "volume"))


def fetch_completed_hourly(
    symbol: str,
    *,
    limit: int = 600,
    opener: Callable = urlopen,
    clock: Callable = lambda: pd.Timestamp.now(tz="UTC"),
) -> tuple[pd.DataFrame, dict]:
    if symbol not in SYMBOLS:
        raise ValueError(f"unsupported shadow symbol: {symbol}")
    query = urlencode({"symbol": symbol, "interval": "1h", "limit": int(limit)})
    request = Request(
        f"{PUBLIC_KLINE_ENDPOINT}?{query}",
        headers={"User-Agent": "tradingbot-long-horizon-shadow/1"},
        method="GET",
    )
    request_started_at = pd.Timestamp(clock())
    try:
        with opener(request, timeout=20) as response:
            body = response.read()
            status = int(getattr(response, "status", 200))
            response_date = (
                response.headers.get("Date") if getattr(response, "headers", None) else None
            )
    except HTTPError as exc:
        raise PublicMarketDataError(f"HTTP_{exc.code}", "public kline endpoint rejected request") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PublicMarketDataError("NETWORK_UNAVAILABLE", "public kline endpoint unavailable") from exc
    received_at = pd.Timestamp(clock())
    if status != 200:
        raise PublicMarketDataError(f"HTTP_{status}", "public kline endpoint returned non-success")
    try:
        payload = json.loads(body.decode("utf-8"))
        frame = parse_public_klines(payload, received_at=received_at)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PublicMarketDataError("MALFORMED_PUBLIC_RESPONSE", "invalid public kline response") from exc
    evidence = {
        "source": "BINANCE_USDM_PUBLIC_TRADE_PRICE_KLINES",
        "endpoint": PUBLIC_KLINE_ENDPOINT,
        "symbol": symbol,
        "interval": "1h",
        "collector_received_at": received_at.isoformat(),
        "request_started_at": request_started_at.isoformat(),
        "response_date_header": response_date,
        "response_sha256": sha256(body).hexdigest(),
        "response_bytes": len(body),
        "response_payload_encoding": "zlib+base64",
        "response_payload_zlib_base64": base64.b64encode(
            zlib.compress(body, level=9)
        ).decode("ascii"),
        "completed_rows": len(frame),
    }
    return frame, evidence


class ShadowJournal:
    """Durable append-only state/event commits with a hash chain."""

    def __init__(self, root: Path, *, build_id: str, policy: LongHorizonTrendPolicy):
        self.root = Path(root)
        self.path = self.root / "long_horizon_shadow.journal.jsonl"
        self.snapshot_path = self.root / "long_horizon_shadow.state.json"
        self.build_id = str(build_id).strip()
        self.policy = policy
        self._record_cache: list[dict] | None = None
        if not self.build_id:
            raise ValueError("build_id is required")

    def _repair_incomplete_tail(self) -> int:
        """Remove only an uncommitted partial final frame after a crash."""
        if not self.path.exists():
            return 0
        content = self.path.read_bytes()
        if not content or content.endswith((b"\n", b"\r")):
            return 0
        boundary = content.rfind(b"\n") + 1
        tail = content[boundary:]
        try:
            json.loads(tail)
        except json.JSONDecodeError:
            with self.path.open("r+b", buffering=0) as handle:
                handle.truncate(boundary)
                os.fsync(handle.fileno())
            return len(tail)
        with self.path.open("ab", buffering=0) as handle:
            handle.write(b"\n")
            os.fsync(handle.fileno())
        return 0

    def _records(self) -> list[dict]:
        if self._record_cache is not None:
            return list(self._record_cache)
        if not self.path.exists():
            self._record_cache = []
            return []
        content = self.path.read_bytes()
        lines = content.splitlines(keepends=True)
        records: list[dict] = []
        previous_hash = None
        for index, raw in enumerate(lines):
            complete = raw.endswith((b"\n", b"\r"))
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not complete:
                    break
                raise ValueError(f"corrupt shadow journal JSON at line {index + 1}")
            claimed = record.pop("record_hash", None)
            calculated = sha256(_canonical(record)).hexdigest()
            if claimed != calculated:
                raise ValueError(f"shadow journal checksum mismatch at line {index + 1}")
            if record.get("previous_record_hash") != previous_hash:
                raise ValueError(f"shadow journal hash-chain mismatch at line {index + 1}")
            record["record_hash"] = claimed
            records.append(record)
            previous_hash = claimed
        self._record_cache = records
        return list(records)

    def load_states(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for record in self._records():
            if record.get("record_type") != "STATE_EVENT_COMMIT":
                continue
            state = record["state"]
            if state.get("policy_hash") != self.policy.policy_hash:
                raise ValueError("journal contains a different policy hash")
            states[str(record["symbol"])] = state
        return states

    def append(self, *, symbol: str, state: dict, events: tuple[dict, ...], evidence: dict) -> dict:
        recovered_tail_bytes = self._repair_incomplete_tail()
        if recovered_tail_bytes:
            self._record_cache = None
        existing = self._records()
        previous_hash = existing[-1]["record_hash"] if existing else None
        record = {
            "schema_version": 1,
            "record_type": "STATE_EVENT_COMMIT",
            "sequence": len(existing) + 1,
            "previous_record_hash": previous_hash,
            "committed_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "build_id": self.build_id,
            "policy": asdict(self.policy),
            "policy_hash": self.policy.policy_hash,
            "execution_authority": False,
            "symbol": symbol,
            "evidence": evidence,
            "recovery": {"discarded_partial_tail_bytes": recovered_tail_bytes},
            "events": list(events),
            "state": state,
        }
        record_hash = sha256(_canonical(record)).hexdigest()
        durable = {**record, "record_hash": record_hash}
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab", buffering=0) as handle:
            handle.write(_canonical(durable) + b"\n")
            os.fsync(handle.fileno())
        if self._record_cache is not None:
            self._record_cache.append(durable)
        return durable

    def write_snapshot(self, states: Mapping[str, dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "authoritative_source": self.path.name,
            "build_id": self.build_id,
            "policy_hash": self.policy.policy_hash,
            "execution_authority": False,
            "states": states,
        }
        temporary = self.snapshot_path.with_suffix(".json.tmp")
        with temporary.open("wb", buffering=0) as handle:
            handle.write(_canonical(payload) + b"\n")
            os.fsync(handle.fileno())
        os.replace(temporary, self.snapshot_path)


class SingleInstanceLock:
    """Portable non-blocking process lock for one writer per data directory."""

    def __init__(self, root: Path):
        self.path = Path(root) / "long_horizon_shadow.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another long-horizon shadow writer is already active") from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def run_cycle(
    journal: ShadowJournal,
    *,
    fetcher: Callable = fetch_completed_hourly,
    maximum_signal_delay_seconds: float = 120.0,
) -> tuple[dict, tuple[dict, ...]]:
    states = journal.load_states()
    all_events: list[dict] = []
    for symbol in SYMBOLS:
        try:
            hourly, evidence = fetcher(symbol)
        except PublicMarketDataError as exc:
            observed_at = pd.Timestamp.now(tz="UTC").isoformat()
            identity = {
                "event_type": "OBSERVATION_ERROR",
                "symbol": symbol,
                "reason": exc.reason_code,
                "observed_at": observed_at,
                "build_id": journal.build_id,
            }
            all_events.append(
                {
                    "event_id": "lhts_" + sha256(_canonical(identity)).hexdigest()[:24],
                    **identity,
                    "execution_authority": False,
                }
            )
            continue
        bars = aggregate_hourly_to_4h(hourly)
        state = states.get(symbol, new_symbol_state(symbol, journal.policy))
        observed_at = evidence["collector_received_at"]
        updated, events = advance_symbol_state(
            state,
            bars,
            observed_at=observed_at,
            policy=journal.policy,
            maximum_signal_delay_seconds=maximum_signal_delay_seconds,
        )
        if updated != state or events:
            journal.append(symbol=symbol, state=updated, events=events, evidence=evidence)
            states[symbol] = updated
            all_events.extend(events)
    journal.write_snapshot(states)
    return states, tuple(all_events)


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(sys.argv[1:] if argv is None else argv)
    assert_no_trading_credentials(os.environ)
    if args.poll_seconds < 5:
        raise ValueError("poll interval must be at least five seconds")
    policy = LongHorizonTrendPolicy()
    journal = ShadowJournal(args.data_dir, build_id=args.build_id, policy=policy)
    with SingleInstanceLock(args.data_dir):
        while True:
            _, events = run_cycle(journal)
            for event in events:
                print(json.dumps(event, sort_keys=True), flush=True)
            if args.once:
                return 0
            failed_observation = any(
                event.get("event_type") == "OBSERVATION_ERROR" for event in events
            )
            time.sleep(max(args.poll_seconds, 60.0) if failed_observation else args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
