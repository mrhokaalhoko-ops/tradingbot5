"""Minimal signed Binance USD-M Demo/Testnet client with hard endpoint guards."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import random
import time
from typing import Any
from urllib.parse import urlencode

import requests

from .config import DemoEndpointGuard, DemoExecutionConfig


SIGNED_PATHS = frozenset({
    "/fapi/v1/algoOrder",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/order",
    "/fapi/v1/openOrders",
    "/fapi/v1/leverage",
    "/fapi/v1/positionSide/dual",
    "/fapi/v1/userTrades",
    "/fapi/v1/income",
    "/fapi/v2/account",
    "/fapi/v2/positionRisk",
})
PUBLIC_PATHS = frozenset({
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
})
RECONCILIATION_GET_PATHS = frozenset({
    "/fapi/v1/algoOrder",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/order",
    "/fapi/v1/openOrders",
    "/fapi/v1/positionSide/dual",
    "/fapi/v1/userTrades",
    "/fapi/v1/income",
    "/fapi/v2/account",
    "/fapi/v2/positionRisk",
})


class DemoBinanceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        binance_code: int | None = None,
        retryable: bool = False,
        retry_state: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.binance_code = binance_code
        self.retryable = bool(retryable)
        self.retry_state = retry_state


class DemoBinanceClient:
    """No endpoint can be changed after construction; guard runs per request."""

    def __init__(
        self,
        config: DemoExecutionConfig,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
        reconciliation_retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0),
        reconciliation_retry_total_seconds: float = 60.0,
        sleep=time.sleep,
        monotonic=time.monotonic,
        random_uniform=random.uniform,
    ):
        self.config = config
        self.endpoint_proof = DemoEndpointGuard.validate(config.base_url)
        self.base_url = str(self.endpoint_proof["base_url"])
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.reconciliation_retry_delays = tuple(
            max(0.0, float(value)) for value in reconciliation_retry_delays
        )
        self.reconciliation_retry_total_seconds = max(
            self.timeout_seconds, float(reconciliation_retry_total_seconds)
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._random_uniform = random_uniform
        self._server_offset_ms = 0
        self.reconciliation_retry_state: dict[str, Any] | None = None
        self.reconciliation_retry_history: list[dict[str, Any]] = []

    def _record_retry_state(self, state: dict[str, Any]) -> None:
        self.reconciliation_retry_state = state
        if state["status"] != "SUCCESS_FIRST_ATTEMPT":
            self.reconciliation_retry_history.append(state)
            self.reconciliation_retry_history = self.reconciliation_retry_history[-50:]

    def _guard(self, *, signed: bool) -> None:
        current = DemoEndpointGuard.validate(self.base_url)
        if current["host"] != self.endpoint_proof["host"]:
            raise DemoBinanceError("DEMO_ENDPOINT_CHANGED_AFTER_STARTUP")
        if signed:
            if not self.config.enabled:
                raise DemoBinanceError("DEMO_EXECUTION_DISABLED")
            if not self.config.api_key or not self.config.api_secret:
                raise DemoBinanceError("DEMO_CREDENTIALS_MISSING")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        method = method.upper()
        allowed = SIGNED_PATHS if signed else PUBLIC_PATHS
        if path not in allowed:
            raise DemoBinanceError(f"DEMO_PATH_NOT_ALLOWLISTED:{path}")
        retryable_read = (
            signed and method == "GET" and path in RECONCILIATION_GET_PATHS
        )
        if not retryable_read:
            return self._request_once(
                method, path, params=params, signed=signed,
                timeout_seconds=self.timeout_seconds,
            )

        started = self._monotonic()
        deadline = started + self.reconciliation_retry_total_seconds
        failures: list[dict[str, Any]] = []
        attempts = 0
        max_attempts = 1 + len(self.reconciliation_retry_delays)
        while attempts < max_attempts:
            attempts += 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            try:
                result = self._request_once(
                    method, path, params=params, signed=signed,
                    timeout_seconds=min(self.timeout_seconds, max(0.001, remaining)),
                )
            except (requests.Timeout, requests.ConnectionError, DemoBinanceError) as exc:
                transient = self._is_transient_failure(exc)
                failures.append({
                    "attempt": attempts,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "transient": transient,
                })
                if not transient:
                    state = {
                        "status": "NOT_RETRYABLE",
                        "method": method,
                        "path": path,
                        "attempts": attempts,
                        "max_attempts": max_attempts,
                        "total_timeout_seconds": self.reconciliation_retry_total_seconds,
                        "elapsed_seconds": max(0.0, self._monotonic() - started),
                        "failures": failures,
                    }
                    self._record_retry_state(state)
                    if isinstance(exc, DemoBinanceError):
                        exc.retry_state = state
                    raise
                if attempts >= max_attempts:
                    break
                base_delay = self.reconciliation_retry_delays[attempts - 1]
                jitter = self._random_uniform(-0.20, 0.20)
                delay = max(0.0, base_delay * (1.0 + float(jitter)))
                remaining = deadline - self._monotonic()
                if delay >= remaining:
                    break
                failures[-1]["backoff_seconds"] = delay
                self._sleep(delay)
                continue

            state = {
                "status": "RECOVERED" if attempts > 1 else "SUCCESS_FIRST_ATTEMPT",
                "method": method,
                "path": path,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "total_timeout_seconds": self.reconciliation_retry_total_seconds,
                "elapsed_seconds": max(0.0, self._monotonic() - started),
                "failures": failures,
            }
            self._record_retry_state(state)
            return result

        state = {
            "status": "EXHAUSTED",
            "method": method,
            "path": path,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "total_timeout_seconds": self.reconciliation_retry_total_seconds,
            "elapsed_seconds": max(0.0, self._monotonic() - started),
            "failures": failures,
        }
        self._record_retry_state(state)
        last_error = failures[-1]["error"] if failures else "TOTAL_TIMEOUT_EXHAUSTED"
        raise DemoBinanceError(
            f"RECONCILIATION_READ_RETRY_EXHAUSTED:{path}:"
            f"ATTEMPTS={attempts}:LAST={last_error}",
            retryable=True,
            retry_state=state,
        )

    @staticmethod
    def _is_transient_failure(exc: Exception) -> bool:
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if not isinstance(exc, DemoBinanceError):
            return False
        if exc.retryable or exc.binance_code == -1007:
            return True
        status = exc.status_code
        return status in {408, 429} or bool(status and 500 <= status <= 599)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        signed: bool,
        timeout_seconds: float,
    ) -> Any:
        self._guard(signed=signed)
        payload = dict(params or {})
        headers = {}
        if signed:
            payload["timestamp"] = int(time.time() * 1000) + self._server_offset_ms
            payload["recvWindow"] = self.config.recv_window_ms
            query = urlencode(payload, doseq=True)
            payload["signature"] = hmac.new(
                self.config.api_secret.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.config.api_key
        response = self.session.request(
            method, f"{self.base_url}{path}", params=payload,
            headers=headers, timeout=timeout_seconds,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": getattr(response, "text", "")[:500]}
        if not 200 <= int(response.status_code) < 300:
            code = data.get("code") if isinstance(data, dict) else None
            message = data.get("msg") if isinstance(data, dict) else str(data)
            try:
                normalized_code = int(code) if code is not None else None
            except (TypeError, ValueError):
                normalized_code = None
            raise DemoBinanceError(
                f"BINANCE_DEMO_HTTP_{response.status_code}:CODE={code}:MSG={message}",
                status_code=int(response.status_code),
                binance_code=normalized_code,
                retryable=(
                    int(response.status_code) in {408, 429}
                    or int(response.status_code) >= 500
                    or normalized_code == -1007
                ),
            )
        return data

    def prove_environment(self) -> dict[str, Any]:
        server = self._request("GET", "/fapi/v1/time")
        info = self._request("GET", "/fapi/v1/exchangeInfo")
        server_ms = int(server["serverTime"])
        self._server_offset_ms = server_ms - int(time.time() * 1000)
        return {
            **self.endpoint_proof,
            "server_time_utc": datetime.fromtimestamp(
                server_ms / 1000.0, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "exchange_timezone": info.get("timezone"),
            "symbols_reported": len(info.get("symbols") or []),
            "signed_order_path": f"{self.base_url}/fapi/v1/order",
            "signed_algo_order_path": f"{self.base_url}/fapi/v1/algoOrder",
        }

    def exchange_filters(self) -> dict[str, dict[str, Any]]:
        return {
            symbol: contract
            for symbol, contract in self.exchange_contracts().items()
            if contract.get("contract_type") == "PERPETUAL"
        }

    def exchange_contracts(self) -> dict[str, dict[str, Any]]:
        """Public contract catalogue used by the cross-environment guard."""
        payload = self._request("GET", "/fapi/v1/exchangeInfo")
        result = {}
        for row in payload.get("symbols", []):
            if row.get("quoteAsset") != "USDT":
                continue
            filters = {item.get("filterType"): item for item in row.get("filters", [])}
            price = filters.get("PRICE_FILTER") or {}
            lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
            notional = filters.get("MIN_NOTIONAL") or {}
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

    def account(self) -> dict:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def position_risk(self) -> list[dict]:
        result = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return list(result or [])

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        result = self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)
        return list(result or [])

    def open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        result = self._request(
            "GET", "/fapi/v1/openAlgoOrders", params=params, signed=True,
        )
        return list(result or [])

    def position_mode(self) -> dict:
        return self._request("GET", "/fapi/v1/positionSide/dual", signed=True)

    def set_one_way_position_mode(self) -> dict:
        """Switch the Demo account to one-way mode; caller must preflight flat state."""
        return self._request(
            "POST",
            "/fapi/v1/positionSide/dual",
            params={"dualSidePosition": "false"},
            signed=True,
        )

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        result = self._request(
            "POST", "/fapi/v1/leverage",
            params={"symbol": symbol, "leverage": int(leverage)}, signed=True,
        )
        if int(result.get("leverage") or 0) != int(leverage):
            raise DemoBinanceError("DEMO_LEVERAGE_NOT_CONFIRMED")
        return result

    def create_market_entry(self, symbol: str, quantity: str, client_id: str) -> dict:
        return self._request(
            "POST", "/fapi/v1/order", signed=True,
            params={
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": quantity, "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
        )

    def create_protective(
        self,
        symbol: str,
        quantity: str,
        trigger_price: str,
        *,
        kind: str,
        client_id: str,
    ) -> dict:
        if kind not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError(kind)
        return self._request(
            "POST", "/fapi/v1/algoOrder", signed=True,
            params={
                "algoType": "CONDITIONAL",
                "symbol": symbol, "side": "SELL", "type": kind,
                "quantity": quantity, "triggerPrice": trigger_price,
                "reduceOnly": "true", "workingType": "MARK_PRICE",
                "priceProtect": "true", "clientAlgoId": client_id,
                "newOrderRespType": "RESULT",
            },
        )

    def query_algo_order(
        self, *, algo_id: str | int | None = None,
        client_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {}
        if algo_id is not None:
            params["algoId"] = algo_id
        elif client_id:
            params["clientAlgoId"] = client_id
        else:
            raise ValueError("algo_id or client_id required")
        return self._request(
            "GET", "/fapi/v1/algoOrder", params=params, signed=True,
        )

    def cancel_algo_order(
        self, *, algo_id: str | int | None = None,
        client_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {}
        if algo_id is not None:
            params["algoId"] = algo_id
        elif client_id:
            params["clientAlgoId"] = client_id
        else:
            raise ValueError("algo_id or client_id required")
        return self._request(
            "DELETE", "/fapi/v1/algoOrder", params=params, signed=True,
        )

    def create_reduce_only_market_exit(
        self, symbol: str, quantity: str, client_id: str
    ) -> dict:
        return self._request(
            "POST", "/fapi/v1/order", signed=True,
            params={
                "symbol": symbol, "side": "SELL", "type": "MARKET",
                "quantity": quantity, "reduceOnly": "true",
                "newClientOrderId": client_id, "newOrderRespType": "RESULT",
            },
        )

    def query_order(
        self, symbol: str, *, order_id: str | int | None = None,
        client_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_id:
            params["origClientOrderId"] = client_id
        else:
            raise ValueError("order_id or client_id required")
        return self._request("GET", "/fapi/v1/order", params=params, signed=True)

    def cancel_order(
        self, symbol: str, *, order_id: str | int | None = None,
        client_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_id:
            params["origClientOrderId"] = client_id
        else:
            raise ValueError("order_id or client_id required")
        return self._request("DELETE", "/fapi/v1/order", params=params, signed=True)

    def user_trades(self, symbol: str, start_time_ms: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"symbol": symbol, "limit": 1000}
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        return list(self._request("GET", "/fapi/v1/userTrades", params=params, signed=True) or [])

    def funding_income(self, symbol: str, start_time_ms: int | None = None) -> list[dict]:
        params: dict[str, Any] = {
            "symbol": symbol, "incomeType": "FUNDING_FEE", "limit": 1000,
        }
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        return list(self._request("GET", "/fapi/v1/income", params=params, signed=True) or [])
