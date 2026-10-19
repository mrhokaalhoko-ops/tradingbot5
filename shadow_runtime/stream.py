"""Credential-free ordered Binance Demo market stream.

Only public ``bookTicker`` and ``kline_1m`` subscriptions are permitted.  The
module has no account client and exposes no order-writing method.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from queue import Empty, Queue
import threading
from typing import Iterable

from websockets.sync.client import connect

from .models import CandleOpenEvent, ExecutionQuote


DEMO_USDM_PUBLIC_WS = "wss://fstream.binancefuture.com/ws"


def _utc_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)


def parse_public_stream_message(payload: dict, *, received_at: datetime):
    """Parse only the two allowlisted public event types."""
    data = payload.get("data", payload)
    event_type = data.get("e")
    if event_type == "bookTicker":
        return ExecutionQuote(
            symbol=str(data["s"]), event_time=_utc_ms(int(data.get("E") or data.get("T"))),
            received_at=received_at, best_bid=float(data["b"]), best_ask=float(data["a"]),
            update_id=int(data["u"]),
        )
    if event_type == "kline" and str((data.get("k") or {}).get("i")) == "1m":
        row = data["k"]
        return CandleOpenEvent(
            symbol=str(data["s"]), open_time=_utc_ms(int(row["t"])),
            received_at=received_at, open_price=float(row["o"]),
        )
    return None


class PublicMarketStream:
    """Background public websocket reader with an ordered in-process queue."""

    def __init__(self, symbols: Iterable[str], *, url: str = DEMO_USDM_PUBLIC_WS):
        self.url = url
        self.symbols = tuple(sorted({str(row).upper() for row in symbols}))
        self._events: Queue = Queue()
        self._subscriptions: Queue = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.last_received_at: datetime | None = None
        self.coverage_started_at: datetime | None = None
        self._gap_started_at: datetime | None = None
        self._gaps: list[tuple[datetime, datetime]] = []
        self._symbol_coverage_started: dict[str, datetime] = {}
        self._state_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="shadow-public-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def drain(self, limit: int = 10_000) -> list:
        result = []
        for _ in range(max(0, int(limit))):
            try:
                result.append(self._events.get_nowait())
            except Empty:
                break
        return result

    def update_symbols(self, symbols: Iterable[str]) -> None:
        desired = tuple(sorted({str(row).upper() for row in symbols}))
        self.symbols = desired
        self._subscriptions.put_nowait(desired)

    def has_continuous_coverage(self, symbol: str, start: datetime, end: datetime) -> bool:
        with self._state_lock:
            symbol_start = self._symbol_coverage_started.get(str(symbol).upper())
            if symbol_start is None or symbol_start > start:
                return False
            gaps = list(self._gaps)
            if self._gap_started_at is not None:
                gaps.append((self._gap_started_at, datetime.now(timezone.utc)))
        return not any(gap_start <= end and gap_end >= start for gap_start, gap_end in gaps)

    def _run(self) -> None:
        request_id = 1
        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=10, close_timeout=3) as websocket:
                    connected_at = datetime.now(timezone.utc)
                    with self._state_lock:
                        if self.coverage_started_at is None:
                            self.coverage_started_at = connected_at
                        if self._gap_started_at is not None:
                            self._gaps.append((self._gap_started_at, connected_at))
                            self._gap_started_at = None
                    current: set[str] = set()
                    desired: set[str] = set(self.symbols)
                    while not self._stop.is_set():
                        try:
                            while True:
                                desired = set(self._subscriptions.get_nowait())
                        except Empty:
                            pass
                        additions = desired - current
                        removals = current - desired
                        if additions:
                            params = []
                            for symbol in sorted(additions):
                                lower = symbol.lower()
                                params.extend((f"{lower}@bookTicker", f"{lower}@kline_1m"))
                            websocket.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": request_id}))
                            with self._state_lock:
                                for symbol in additions:
                                    self._symbol_coverage_started[symbol] = datetime.now(timezone.utc)
                            request_id += 1
                        if removals:
                            params = []
                            for symbol in sorted(removals):
                                lower = symbol.lower()
                                params.extend((f"{lower}@bookTicker", f"{lower}@kline_1m"))
                            websocket.send(json.dumps({"method": "UNSUBSCRIBE", "params": params, "id": request_id}))
                            with self._state_lock:
                                for symbol in removals:
                                    self._symbol_coverage_started.pop(symbol, None)
                            request_id += 1
                        current = set(desired)
                        try:
                            raw = websocket.recv(timeout=0.25)
                        except TimeoutError:
                            continue
                        received = datetime.now(timezone.utc)
                        parsed = parse_public_stream_message(json.loads(raw), received_at=received)
                        if parsed is not None:
                            self.last_received_at = received
                            self._events.put_nowait(parsed)
            except Exception as exc:
                self.errors += 1
                self.reconnects += 1
                self.last_error = f"{type(exc).__name__}:{exc}"
                with self._state_lock:
                    if self._gap_started_at is None:
                        self._gap_started_at = datetime.now(timezone.utc)
                if self._stop.wait(1.0):
                    break


class StreamExecutionProvider:
    """Persists stream events and supplies only fresh prospective observations."""

    def __init__(self, store, stream: PublicMarketStream | None = None):
        self.store = store
        self.stream = stream
        self._runtime_pending: list = []

    def ingest_pending(self) -> list:
        events = self.stream.drain() if self.stream else []
        accepted = []
        for event in events:
            if isinstance(event, ExecutionQuote):
                if self.store.insert_execution_quote(event):
                    accepted.append(event)
            elif isinstance(event, CandleOpenEvent):
                if self.store.insert_candle_open(event):
                    accepted.append(event)
        self._runtime_pending.extend(accepted)
        return accepted

    def drain_for_runtime(self) -> list:
        self.ingest_pending()
        result = list(self._runtime_pending)
        self._runtime_pending.clear()
        return result

    def wait_fresh_quote(self, symbol: str, ready_at: datetime, *, timeout_seconds: float = 5.0):
        import time
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            self.ingest_pending()
            marker = ready_at.isoformat().replace("+00:00", "Z")
            quote = self.store.first_execution_quote(symbol, marker)
            now = datetime.now(timezone.utc)
            if quote and 0 <= (now - quote.event_time).total_seconds() <= 2.0:
                return quote
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.025)

    def exact_candle_open(self, symbol: str, open_time: datetime):
        self.ingest_pending()
        return self.store.candle_open(symbol, open_time.isoformat().replace("+00:00", "Z"))

    def wait_candle_open(self, symbol: str, open_time: datetime, *, timeout_seconds: float = 5.0):
        import time
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            event = self.exact_candle_open(symbol, open_time)
            if event is not None:
                return event
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.025)

    def first_quote_near(self, symbol: str, ready_at: datetime, *, window_seconds: float = 2.0):
        from datetime import timedelta
        self.ingest_pending()
        iso = lambda value: value.isoformat().replace("+00:00", "Z")
        return self.store.first_execution_quote(
            symbol, iso(ready_at), iso(ready_at + timedelta(seconds=window_seconds))
        )

    def quotes_since(self, symbol: str, not_before: datetime) -> list[ExecutionQuote]:
        self.ingest_pending()
        marker = not_before.isoformat().replace("+00:00", "Z")
        return self.store.execution_quotes_since(symbol, marker)
