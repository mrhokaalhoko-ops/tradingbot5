"""Credential-free Binance USD-M public market gateway.

The gateway deliberately exposes only HTTP GET over a small endpoint allowlist.
It cannot accept an API key, secret, account identifier, or order payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any

import pandas as pd
import requests

from strategy.active_altcoin_shadow import detect_active_event
from strategy.prospective_shadow_spec import ACTIVE_CANDIDATE_CONTRACT

from .models import Candidate, Candle, MarketSnapshot, Quote


DEMO_USDM_PUBLIC_URL = "https://demo-fapi.binance.com"
MAINNET_USDM_PUBLIC_URL = "https://fapi.binance.com"
ALLOWED_PATHS = frozenset({
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/klines",
})
TIMEFRAME_LIMITS = {
    "1w": 40, "1d": 90, "4h": 120, "1h": 300,
    "15m": 300, "5m": 120, "1m": 180,
}


@dataclass(frozen=True)
class ScanResult:
    candidates: tuple[tuple[Candidate, list], ...]
    symbols_scanned: int
    raw_active_events: int
    qualified_candidates: int
    api_errors: int
    latency_ms: float


def _utc_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)


def _canonical_hash(prefix: str, payload: Any, size: int = 24) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:size]}"


class PublicBinanceDemoMarket:
    """Read-only Binance USD-M public data with an explicit environment."""

    def __init__(
        self,
        *,
        base_url: str = DEMO_USDM_PUBLIC_URL,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
    ):
        self.base_url = str(base_url).rstrip("/")
        if self.base_url == DEMO_USDM_PUBLIC_URL:
            self.market_environment = "DEMO"
            self.snapshot_source = "BINANCE_DEMO_PUBLIC_USDM"
        elif self.base_url == MAINNET_USDM_PUBLIC_URL:
            self.market_environment = "MAINNET"
            self.snapshot_source = "BINANCE_MAINNET_PUBLIC_USDM"
        else:
            raise RuntimeError(f"UNAPPROVED_BINANCE_PUBLIC_ENDPOINT:{self.base_url}")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)

    def _get(self, path: str, params: dict | None = None) -> Any:
        if path not in ALLOWED_PATHS:
            raise PermissionError(f"public endpoint not allowed: {path}")
        response = self.session.get(
            f"{self.base_url}{path}", params=params or {}, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        return response.json()

    def server_now(self) -> datetime:
        payload = self._get("/fapi/v1/time")
        return _utc_from_ms(int(payload["serverTime"]))

    def exchange_filters(self) -> dict[str, dict[str, Any]]:
        payload = self._get("/fapi/v1/exchangeInfo")
        result = {}
        for row in payload.get("symbols", []):
            if row.get("contractType") != "PERPETUAL" or row.get("quoteAsset") != "USDT":
                continue
            filters = {item.get("filterType"): item for item in row.get("filters", [])}
            price = filters.get("PRICE_FILTER", {})
            lot = filters.get("LOT_SIZE", {})
            notional = filters.get("MIN_NOTIONAL", {})
            result[str(row["symbol"])] = {
                "symbol": str(row["symbol"]),
                "status": str(row.get("status") or ""),
                "contract_type": str(row.get("contractType") or ""),
                "base_asset": str(row.get("baseAsset") or ""),
                "quote_asset": str(row.get("quoteAsset") or ""),
                "margin_asset": str(row.get("marginAsset") or ""),
                "tick_size": float(price.get("tickSize") or 0),
                "step_size": float(lot.get("stepSize") or 0),
                "min_qty": float(lot.get("minQty") or 0),
                "max_qty": float(lot.get("maxQty") or 0),
                "min_notional": float(notional.get("notional") or 0),
            }
        return result

    def exchange_contracts(self) -> dict[str, dict[str, Any]]:
        """Return all USD-M/USDT contract identities for cross-env auditing."""
        payload = self._get("/fapi/v1/exchangeInfo")
        result = {}
        for row in payload.get("symbols", []):
            if row.get("quoteAsset") != "USDT":
                continue
            filters = {
                item.get("filterType"): item for item in row.get("filters", [])
            }
            price = filters.get("PRICE_FILTER", {})
            lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE", {})
            notional = filters.get("MIN_NOTIONAL", {})
            result[str(row["symbol"])] = {
                "symbol": str(row["symbol"]),
                "status": str(row.get("status") or ""),
                "contract_type": str(row.get("contractType") or ""),
                "base_asset": str(row.get("baseAsset") or ""),
                "quote_asset": str(row.get("quoteAsset") or ""),
                "margin_asset": str(row.get("marginAsset") or ""),
                "tick_size": float(price.get("tickSize") or 0),
                "step_size": float(lot.get("stepSize") or 0),
                "min_qty": float(lot.get("minQty") or 0),
                "max_qty": float(lot.get("maxQty") or 0),
                "min_notional": float(notional.get("notional") or 0),
            }
        return result

    def liquid_symbols(self, limit: int = 50) -> list[str]:
        payload = self._get("/fapi/v1/ticker/24hr")
        excluded = set(ACTIVE_CANDIDATE_CONTRACT["exclude_majors"]) | {
            "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT",
        }
        eligible = [
            row for row in payload
            if str(row.get("symbol", "")).endswith("USDT")
            and str(row.get("symbol")) not in excluded
            and float(row.get("quoteVolume") or 0) > 0
        ]
        eligible.sort(key=lambda row: float(row.get("quoteVolume") or 0), reverse=True)
        return [str(row["symbol"]) for row in eligible[: max(1, min(50, int(limit)))]]

    def raw_klines(self, symbol: str, timeframe: str, limit: int) -> list:
        payload = self._get(
            "/fapi/v1/klines",
            {"symbol": str(symbol).upper(), "interval": timeframe, "limit": int(limit)},
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected kline payload")
        return payload

    @staticmethod
    def _closed_candles(payload: list, timeframe: str, as_of: datetime) -> tuple[Candle, ...]:
        rows = []
        for item in payload:
            if not isinstance(item, list) or len(item) < 11:
                continue
            close_time = _utc_from_ms(int(item[6]))
            if close_time > as_of:
                continue
            rows.append(Candle(
                timeframe=timeframe,
                open_time=_utc_from_ms(int(item[0])),
                close_time=close_time,
                open=float(item[1]), high=float(item[2]), low=float(item[3]),
                close=float(item[4]), volume=float(item[5]),
                quote_volume=float(item[7]),
                taker_buy_quote_volume=float(item[10]),
            ))
        return tuple(rows)

    @staticmethod
    def _one_minute_frame(payload: list) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "open_time": pd.to_datetime(int(row[0]), unit="ms", utc=True),
                "close_time": pd.to_datetime(int(row[6]), unit="ms", utc=True),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]), "quote_volume": float(row[7]),
                "trades": int(row[8]), "taker_buy_quote_volume": float(row[10]),
            }
            for row in payload if isinstance(row, list) and len(row) >= 11
        ])

    def scan_candidates(self, *, as_of: datetime | None = None) -> ScanResult:
        started = time.monotonic()
        as_of = as_of or self.server_now()
        candidates = []
        raw_active = 0
        errors = 0
        symbols = self.liquid_symbols(ACTIVE_CANDIDATE_CONTRACT["candidate_limit_per_poll"])
        for symbol in symbols:
            try:
                payload = self.raw_klines(symbol, "1m", TIMEFRAME_LIMITS["1m"])
                event = detect_active_event(symbol, self._one_minute_frame(payload), as_of)
            except Exception:
                errors += 1
                continue
            if event.get("qualified"):
                raw_active += 1
            if not event.get("qualified") or event.get("side") != "LONG":
                continue
            candidate = Candidate(
                candidate_id=str(event["event_id"]),
                symbol=symbol,
                side="LONG",
                emitted_at=as_of,
                event={
                    **event,
                    "signal_market_environment": self.market_environment,
                    "signal_market_base_url": self.base_url,
                },
            )
            candidates.append((candidate, payload))
        candidates.sort(key=lambda item: float(item[0].event.get("event_score", 0)), reverse=True)
        accepted = candidates[: ACTIVE_CANDIDATE_CONTRACT["cross_section_max_per_5m_close"]]
        return ScanResult(
            candidates=tuple(accepted), symbols_scanned=len(symbols),
            raw_active_events=raw_active, qualified_candidates=len(candidates),
            api_errors=errors, latency_ms=(time.monotonic() - started) * 1000.0,
        )

    def discover_candidates(self, *, as_of: datetime | None = None) -> list[tuple[Candidate, list]]:
        return list(self.scan_candidates(as_of=as_of).candidates)

    def quote(self, symbol: str) -> Quote:
        book = self._get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        premium = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        observed_at = self.server_now()
        next_funding = premium.get("nextFundingTime")
        return Quote(
            observed_at=observed_at,
            best_bid=float(book["bidPrice"]),
            best_ask=float(book["askPrice"]),
            mark_price=float(premium["markPrice"]),
            index_price=float(premium["indexPrice"]),
            funding_rate=float(premium.get("lastFundingRate") or 0),
            next_funding_time=_utc_from_ms(int(next_funding)) if next_funding else None,
        )

    def snapshot(self, candidate: Candidate, *, cached_1m: list | None = None) -> MarketSnapshot:
        raw_by_timeframe = {}
        for timeframe, limit in TIMEFRAME_LIMITS.items():
            raw = cached_1m if timeframe == "1m" and cached_1m is not None else self.raw_klines(
                candidate.symbol, timeframe, limit
            )
            raw_by_timeframe[timeframe] = raw
        # Analysis quote is deliberately fetched only after the slow candle
        # collection.  It is diagnostic; fills use the independent stream.
        quote = self.quote(candidate.symbol)
        candles = {
            timeframe: self._closed_candles(raw, timeframe, quote.observed_at)
            for timeframe, raw in raw_by_timeframe.items()
        }
        fingerprint = {
            "candidate_id": candidate.candidate_id,
            "as_of_ms": int(quote.observed_at.timestamp() * 1000),
            "bid": quote.best_bid, "ask": quote.best_ask,
            "closed": {
                timeframe: rows[-1].candle_id if rows else None
                for timeframe, rows in candles.items()
            },
        }
        return MarketSnapshot(
            snapshot_id=_canonical_hash("snapshot", fingerprint),
            symbol=candidate.symbol,
            as_of=quote.observed_at,
            candidate=candidate,
            quote=quote,
            candles=candles,
            source=self.snapshot_source,
        )
