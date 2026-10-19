"""Read-only market-wide hot-list monitor.

Consumes Binance USD-M public tickers and publishes shadow observations plus
fresh activity rankings for Demo discovery. It cannot create a signal, virtual
order, order intent, or exchange order; entry prices still come from the venue.
"""

from __future__ import annotations

import os
import math
from copy import deepcopy
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import requests

from core.logger import logger
from core.scanner_universe import ACTIVITY_SOURCE
from utils.decision_audit import audit_decision_event


BINANCE_USDM_PUBLIC_URL = os.getenv(
    "BINANCE_USDM_PUBLIC_URL", "https://fapi.binance.com"
).rstrip("/")
ACTIVE_EVENT_PREFILTER_20M_PCT = 2.0
ACTIVE_EVENT_LIQUIDITY_UNIVERSE_SIZE = 50

_snapshot_lock = threading.Lock()
_latest_snapshot = {
    "mode": "SHADOW_ONLY",
    "execution_enabled": False,
    "status": "NOT_STARTED",
    "candidates": [],
    "active_event_candidates": [],
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_float(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _bounded_int_env(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float_env(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def get_latest_hot_list_snapshot():
    """Return a defensive copy of the most recent shadow snapshot."""
    with _snapshot_lock:
        return deepcopy(_latest_snapshot)


def _publish_snapshot(snapshot):
    global _latest_snapshot
    with _snapshot_lock:
        _latest_snapshot = snapshot


def _percent_change(current, previous):
    if current <= 0 or previous <= 0:
        return 0.0
    return ((current / previous) - 1.0) * 100.0


def _price_near_window(history, target_ts, tolerance_seconds=75.0):
    # All windows end well before now. Tolerate polling jitter on either side
    # of the reference time, but never substitute an observation across a gap.
    eligible = [(ts, price) for ts, price in history
                if abs(ts - target_ts) <= tolerance_seconds]
    return min(eligible, key=lambda item: (abs(item[0] - target_ts), item[0]))[1] if eligible else None


def classify_hot_candidate(
    ticker,
    *,
    change_1m=0.0,
    change_5m=0.0,
    change_20m=0.0,
    is_new_contract=False,
    min_quote_volume=2_000_000.0,
    momentum_1m_pct=0.8,
    momentum_5m_pct=2.0,
    momentum_20m_pct=2.0,
    range_edge_pct=8.0,
    reclaim_1m_pct=0.25,
):
    """Classify one public ticker for shadow observation only."""
    symbol = str(ticker.get("symbol") or "").upper()
    last = _as_float(ticker.get("lastPrice") or ticker.get("last"))
    low = _as_float(ticker.get("lowPrice") or ticker.get("low"))
    high = _as_float(ticker.get("highPrice") or ticker.get("high"))
    quote_volume = _as_float(ticker.get("quoteVolume"))
    change_24h = _as_float(ticker.get("priceChangePercent") or ticker.get("percentage"))
    if not symbol or last <= 0 or quote_volume < min_quote_volume:
        return None

    range_position = 50.0
    if high > low > 0:
        range_position = max(0.0, min(100.0, ((last - low) / (high - low)) * 100.0))

    tags = []
    if is_new_contract:
        tags.append("NEW_CONTRACT")
    if change_1m >= momentum_1m_pct or change_5m >= momentum_5m_pct:
        tags.append("MOMENTUM_UP")
    if change_1m <= -momentum_1m_pct or change_5m <= -momentum_5m_pct:
        tags.append("MOMENTUM_DOWN")
    same_active_direction = (
        change_5m != 0
        and change_20m != 0
        and (change_5m > 0) == (change_20m > 0)
    )
    # Discovery-only recall gate.  The exact strategy event is rebuilt later
    # from completed Binance 1m candles, so this deliberately broad tag cannot
    # qualify a setup or create an order.  Keeping it independent from the
    # rolling 5m direction avoids losing a valid just-closed event when price
    # retraces during the one-minute monitor hand-off.
    if abs(change_20m) >= ACTIVE_EVENT_PREFILTER_20M_PCT:
        tags.append("ACTIVE_EVENT_PREFILTER")
    if (
        abs(change_5m) >= min(1.0, momentum_5m_pct)
        and abs(change_20m) >= momentum_20m_pct
        and same_active_direction
    ):
        tags.append("ACTIVE_EVENT_WATCH")
    if range_position <= range_edge_pct and change_1m >= reclaim_1m_pct:
        tags.append("LOW_RECLAIM_WATCH")
    if range_position >= (100.0 - range_edge_pct) and change_1m <= -reclaim_1m_pct:
        tags.append("HIGH_REJECTION_WATCH")
    if not tags:
        return None

    score = (
        abs(change_1m) * 5.0
        + abs(change_5m) * 2.0
        + abs(change_20m) * 0.5
        + min(abs(change_24h), 30.0) * 0.15
        + (4.0 if is_new_contract else 0.0)
        + (2.0 if "LOW_RECLAIM_WATCH" in tags or "HIGH_REJECTION_WATCH" in tags else 0.0)
    )
    return {
        "symbol": symbol,
        "tags": tags,
        "last_price": last,
        "change_1m_pct": round(change_1m, 4),
        "change_5m_pct": round(change_5m, 4),
        "change_20m_pct": round(change_20m, 4),
        "change_24h_pct": round(change_24h, 4),
        "range_position_24h_pct": round(range_position, 2),
        "quote_volume_24h": round(quote_volume, 2),
        "score": round(score, 4),
    }


class HotListShadowMonitor:
    """Market-wide public-data observer with no execution capability."""

    def __init__(self, session=None, *, now_fn=time.time):
        self.enabled = os.getenv("HOT_LIST_SHADOW_ENABLED", "true").strip().lower() in {
            "true", "1", "yes", "on"
        }
        self.interval_seconds = _bounded_int_env(
            "HOT_LIST_INTERVAL_SECONDS", 60, 30, 900
        )
        self.top_limit = _bounded_int_env("HOT_LIST_TOP_LIMIT", 50, 5, 50)
        self.min_quote_volume = _bounded_float_env(
            "HOT_LIST_MIN_QUOTE_VOLUME_USDT", 2_000_000.0, 0.0, 1_000_000_000.0
        )
        self.momentum_1m_pct = _bounded_float_env(
            "HOT_LIST_MOMENTUM_1M_PCT", 0.8, 0.1, 20.0
        )
        self.momentum_5m_pct = _bounded_float_env(
            "HOT_LIST_MOMENTUM_5M_PCT", 2.0, 0.2, 50.0
        )
        self.momentum_20m_pct = _bounded_float_env(
            "HOT_LIST_MOMENTUM_20M_PCT", 2.0, 0.5, 100.0
        )
        self.range_edge_pct = _bounded_float_env(
            "HOT_LIST_RANGE_EDGE_PCT", 8.0, 1.0, 25.0
        )
        self.reclaim_1m_pct = _bounded_float_env(
            "HOT_LIST_RECLAIM_1M_PCT", 0.25, 0.05, 10.0
        )
        self.session = session or requests.Session()
        self.now_fn = now_fn
        self._history = defaultdict(lambda: deque(maxlen=64))
        self._known_symbols = None
        self._last_audit_at = 0.0
        self._last_candidate_fingerprint = None

    def _fetch_public_tickers(self):
        response = self.session.get(
            f"{BINANCE_USDM_PUBLIC_URL}/fapi/v1/ticker/24hr", timeout=15
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Binance USD-M ticker payload is not a list")
        return payload

    @staticmethod
    def _eligible_ticker(ticker):
        symbol = str(ticker.get("symbol") or "").upper()
        return symbol.endswith("USDT") and symbol not in {
            "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT"
        }

    def collect_once(self, tickers=None):
        tickers = self._fetch_public_tickers() if tickers is None else tickers
        now_ts = float(self.now_fn())
        eligible = [ticker for ticker in tickers if self._eligible_ticker(ticker)]
        liquidity_rank = {
            str(ticker.get("symbol") or "").upper(): rank
            for rank, ticker in enumerate(
                sorted(
                    eligible,
                    key=lambda row: _as_float(row.get("quoteVolume")),
                    reverse=True,
                ),
                start=1,
            )
        }
        current_symbols = {
            str(ticker.get("symbol") or "").upper() for ticker in eligible
        }
        new_symbols = set()
        if self._known_symbols is not None:
            new_symbols = current_symbols - self._known_symbols
        self._known_symbols = current_symbols

        candidates = []
        scanner_activity_candidates = []
        for ticker in eligible:
            symbol = str(ticker.get("symbol") or "").upper()
            last = _as_float(ticker.get("lastPrice") or ticker.get("last"))
            if last <= 0:
                continue
            history = self._history[symbol]
            previous_1m = _price_near_window(history, now_ts - 60.0, tolerance_seconds=15.0)
            previous_5m = _price_near_window(history, now_ts - 300.0)
            previous_20m = _price_near_window(history, now_ts - 1200.0)
            change_1m = _percent_change(last, previous_1m) if previous_1m else 0.0
            change_5m = _percent_change(last, previous_5m) if previous_5m else 0.0
            change_20m = _percent_change(last, previous_20m) if previous_20m else 0.0
            history.append((now_ts, last))
            # Ranking only: no direction, entry geometry, or trade authorization.
            activity_score = abs(change_1m) * 5.0 + abs(change_5m) * 2.0
            if activity_score > 0 and _as_float(ticker.get("quoteVolume")) >= self.min_quote_volume:
                scanner_activity_candidates.append({
                    "symbol": symbol, "activity_score": round(activity_score, 6),
                    "change_1m_pct": round(change_1m, 6),
                    "change_5m_pct": round(change_5m, 6),
                })

            candidate = classify_hot_candidate(
                ticker,
                change_1m=change_1m,
                change_5m=change_5m,
                change_20m=change_20m,
                is_new_contract=symbol in new_symbols,
                min_quote_volume=self.min_quote_volume,
                momentum_1m_pct=self.momentum_1m_pct,
                momentum_5m_pct=self.momentum_5m_pct,
                momentum_20m_pct=self.momentum_20m_pct,
                range_edge_pct=self.range_edge_pct,
                reclaim_1m_pct=self.reclaim_1m_pct,
            )
            if candidate:
                rank = int(liquidity_rank.get(symbol, len(eligible) + 1))
                candidate["liquidity_rank_24h"] = rank
                candidate["research_universe_proxy"] = bool(
                    rank <= ACTIVE_EVENT_LIQUIDITY_UNIVERSE_SIZE
                )
                candidates.append(candidate)

        candidates.sort(key=lambda item: item["score"], reverse=True)
        active_event_candidates = [
            item for item in candidates
            if "ACTIVE_EVENT_PREFILTER" in {
                str(tag).upper() for tag in item.get("tags", [])
            }
            and item.get("research_universe_proxy") is True
        ][:ACTIVE_EVENT_LIQUIDITY_UNIVERSE_SIZE]
        scanner_activity_candidates.sort(key=lambda item: (-item["activity_score"], item["symbol"]))
        candidates = candidates[: self.top_limit]
        snapshot = {
            "mode": "SHADOW_ONLY",
            "execution_enabled": False,
            "status": "OK",
            "timestamp": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "scanner_source": ACTIVITY_SOURCE,
            "scanner_activity_candidates": scanner_activity_candidates,
            "total_market_symbols": len(current_symbols),
            "candidate_count": len(candidates),
            "active_event_candidate_count": len(active_event_candidates),
            "new_contract_count": len(new_symbols),
            "candidates": candidates,
            "active_event_candidates": active_event_candidates,
        }
        _publish_snapshot(snapshot)

        fingerprint = tuple(
            (item["symbol"], tuple(item["tags"])) for item in candidates
        )
        if fingerprint != self._last_candidate_fingerprint or now_ts - self._last_audit_at >= 300:
            audit_decision_event(
                stage="HOT_LIST_SHADOW",
                status="UPDATED",
                details={
                    "execution_enabled": False,
                    "total_market_symbols": len(current_symbols),
                    "candidate_count": len(candidates),
                    "active_event_candidate_count": len(active_event_candidates),
                    "new_contract_count": len(new_symbols),
                    "top_candidates": candidates[:10],
                },
            )
            self._last_audit_at = now_ts
            self._last_candidate_fingerprint = fingerprint
        return snapshot

    def run_forever(self, stop_event):
        if not self.enabled:
            snapshot = {
                "mode": "SHADOW_ONLY",
                "execution_enabled": False,
                "status": "DISABLED",
                "timestamp": _utc_now(),
                "candidates": [],
                "active_event_candidates": [],
            }
            _publish_snapshot(snapshot)
            logger.info("[HotListShadow] المراقبة معطلة من البيئة.")
            return

        logger.info(
            "[HotListShadow] بدأ مرصد السوق العام (shadow-only، بلا تنفيذ) كل %ss.",
            self.interval_seconds,
        )
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                snapshot = self.collect_once()
                logger.info(
                    "[HotListShadow] symbols=%s candidates=%s execution=false",
                    snapshot.get("total_market_symbols", 0),
                    snapshot.get("candidate_count", 0),
                )
            except Exception as exc:
                logger.warning("[HotListShadow] تعذر تحديث القائمة: %s", exc)
                previous = get_latest_hot_list_snapshot()
                previous.update(
                    {"status": "STALE", "last_error": str(exc), "error_at": _utc_now()}
                )
                _publish_snapshot(previous)
                audit_decision_event(
                    stage="HOT_LIST_SHADOW",
                    status="DATA_UNAVAILABLE",
                    details={"execution_enabled": False, "error": str(exc)},
                )
            elapsed = time.monotonic() - started
            stop_event.wait(max(1.0, self.interval_seconds - elapsed))
