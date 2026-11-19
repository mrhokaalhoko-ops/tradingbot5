"""Fail-closed startup policy for every TradingBot5 runtime.

This module intentionally imports no exchange, Telegram, database, or network
client.  It is safe to evaluate before the application creates external
connections and is kept dependency-injected so tests do not need to reload the
environment-backed :mod:`core.config` module.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any


LIVE_CONFIRMATION_ENV = "LIVE_TRADING_CONFIRMATION"
LIVE_CONFIRMATION_VALUE = "I_ACCEPT_REAL_MONEY_RISK"


def validate_live_trading_confirmation(
    config: Any,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Reject mainnet mode unless the operator supplies the exact opt-in.

    The confirmation is deliberately an environment-only control: it cannot
    be enabled by persisted bot state, a Telegram command, or a strategy.
    """

    environment = os.environ if environ is None else environ
    if (
        not bool(config.USE_TESTNET)
        and environment.get(LIVE_CONFIRMATION_ENV) != LIVE_CONFIRMATION_VALUE
    ):
        raise RuntimeError(
            "Live trading blocked: LIVE_TRADING_CONFIRMATION is missing or incorrect."
        )
    return True


def validate_telegram_proposal_demo_mode(config: Any) -> dict[str, Any]:
    """Validate the Telegram-to-Demo bridge before clients are constructed."""

    if not bool(config.TELEGRAM_PROPOSAL_DEMO_MODE):
        return {"enabled": False}
    if not bool(config.USE_TESTNET):
        raise RuntimeError("TELEGRAM_PROPOSAL_DEMO_MODE_REQUIRES_USE_TESTNET_TRUE")
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        raise RuntimeError("TELEGRAM_PROPOSAL_DEMO_CREDENTIALS_MISSING")
    if int(config.LEVERAGE) != 5:
        raise RuntimeError("TELEGRAM_PROPOSAL_DEMO_REQUIRES_5X_LEVERAGE")

    execution_enabled = bool(config.DEMO_EXECUTION_ENABLED)
    return {
        "enabled": True,
        "execution_enabled": execution_enabled,
        "effective_position_cap": (
            min(
                int(config.TESTNET_MAX_OPEN_TRADES),
                int(config.HARD_MAX_OPEN_TRADES),
            )
            if execution_enabled
            else 0
        ),
        "telegram_polling_enabled": bool(config.TELEGRAM_POLLING_ENABLED),
        "telegram_outbound_enabled": bool(config.TELEGRAM_OUTBOUND_ENABLED),
        "account_management_enabled": bool(config.ACCOUNT_MANAGEMENT_ENABLED),
    }


def validate_startup(config: Any, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run all mode checks and return the validated runtime mode."""

    validate_live_trading_confirmation(config, environ)
    return validate_telegram_proposal_demo_mode(config)
