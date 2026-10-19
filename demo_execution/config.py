"""Fail-closed configuration for the isolated Binance Demo B executor."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_DEMO_REST_HOSTS = frozenset({
    "demo-fapi.binance.com",
    "testnet.binancefuture.com",
})
FORBIDDEN_MAINNET_HOSTS = frozenset({
    "fapi.binance.com",
    "api.binance.com",
    "www.binance.com",
})
DEFAULT_DEMO_REST_URL = "https://demo-fapi.binance.com"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class DemoEndpointGuard:
    """Exact-host guard checked at startup and again before every signed call."""

    @staticmethod
    def validate(base_url: str) -> dict[str, str | bool]:
        parsed = urlparse(str(base_url).strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https":
            raise RuntimeError("DEMO_ENDPOINT_REJECTED:HTTPS_REQUIRED")
        if host in FORBIDDEN_MAINNET_HOSTS or host not in ALLOWED_DEMO_REST_HOSTS:
            raise RuntimeError(f"DEMO_ENDPOINT_REJECTED:HOST={host or 'EMPTY'}")
        if parsed.port not in (None, 443):
            raise RuntimeError("DEMO_ENDPOINT_REJECTED:NONSTANDARD_PORT")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise RuntimeError("DEMO_ENDPOINT_REJECTED:BASE_URL_MUST_NOT_HAVE_PATH")
        return {
            "base_url": f"https://{host}",
            "host": host,
            "is_demo_or_testnet": True,
            "is_mainnet": False,
        }


@dataclass(frozen=True)
class DemoExecutionConfig:
    enabled: bool = False
    base_url: str = DEFAULT_DEMO_REST_URL
    api_key: str = ""
    api_secret: str = ""
    leverage: int = 5
    frozen_max_open_positions: int = 10
    initial_validation_cap: int = 1
    target_initial_margin_fraction: float = 0.01
    base_stop_risk_fraction: float = 0.0025
    hard_stop_risk_fraction: float = 0.01
    min_actual_rr: float = 1.5
    recv_window_ms: int = 5_000
    poll_seconds: int = 60
    fill_timeout_seconds: float = 10.0
    database_path: Path = Path("/app/data/demo_b_r4.sqlite3")
    output_dir: Path = Path("/app/data/demo_b_r4")

    @classmethod
    def from_env(cls) -> "DemoExecutionConfig":
        config = cls(
            enabled=_bool_env("DEMO_EXECUTION_ENABLED", False),
            base_url=os.getenv("BINANCE_DEMO_BASE_URL", DEFAULT_DEMO_REST_URL).strip(),
            api_key=os.getenv("BINANCE_DEMO_API_KEY", "").strip(),
            api_secret=os.getenv("BINANCE_DEMO_API_SECRET", "").strip(),
            poll_seconds=max(10, int(os.getenv("DEMO_POLL_SECONDS", "60"))),
            fill_timeout_seconds=max(
                1.0, float(os.getenv("DEMO_FILL_TIMEOUT_SECONDS", "10"))
            ),
            database_path=Path(
                os.getenv("DEMO_DATABASE_PATH", "/app/data/demo_b_r4.sqlite3")
            ),
            output_dir=Path(os.getenv("DEMO_OUTPUT_DIR", "/app/data/demo_b_r4")),
        )
        config.validate()
        return config

    def validate(self) -> dict[str, str | bool]:
        proof = DemoEndpointGuard.validate(self.base_url)
        frozen = {
            "leverage": self.leverage,
            "frozen_max_open_positions": self.frozen_max_open_positions,
            "initial_validation_cap": self.initial_validation_cap,
            "target_initial_margin_fraction": self.target_initial_margin_fraction,
            "base_stop_risk_fraction": self.base_stop_risk_fraction,
            "hard_stop_risk_fraction": self.hard_stop_risk_fraction,
            "min_actual_rr": self.min_actual_rr,
        }
        expected = {
            "leverage": 5,
            "frozen_max_open_positions": 10,
            "initial_validation_cap": 1,
            "target_initial_margin_fraction": 0.01,
            "base_stop_risk_fraction": 0.0025,
            "hard_stop_risk_fraction": 0.01,
            "min_actual_rr": 1.5,
        }
        if frozen != expected:
            raise RuntimeError("DEMO_FROZEN_RISK_CONFIG_MISMATCH")
        if self.enabled and (not self.api_key or not self.api_secret):
            raise RuntimeError("DEMO_EXECUTION_ENABLED_BUT_CREDENTIALS_MISSING")
        return proof

