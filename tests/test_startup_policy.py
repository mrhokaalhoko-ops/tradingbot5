from types import SimpleNamespace

import pytest

from core.startup import (
    LIVE_CONFIRMATION_ENV,
    LIVE_CONFIRMATION_VALUE,
    validate_live_trading_confirmation,
    validate_startup,
    validate_telegram_proposal_demo_mode,
)


def config(**overrides):
    values = {
        "USE_TESTNET": True,
        "TELEGRAM_PROPOSAL_DEMO_MODE": False,
        "BINANCE_API_KEY": None,
        "BINANCE_API_SECRET": None,
        "LEVERAGE": 5,
        "DEMO_EXECUTION_ENABLED": False,
        "TESTNET_MAX_OPEN_TRADES": 10,
        "HARD_MAX_OPEN_TRADES": 10,
        "TELEGRAM_POLLING_ENABLED": True,
        "TELEGRAM_OUTBOUND_ENABLED": True,
        "ACCOUNT_MANAGEMENT_ENABLED": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_testnet_starts_without_live_confirmation():
    assert validate_live_trading_confirmation(config(), {})


def test_mainnet_requires_exact_environment_confirmation():
    live = config(USE_TESTNET=False)
    with pytest.raises(RuntimeError, match="Live trading blocked"):
        validate_live_trading_confirmation(live, {})
    assert validate_live_trading_confirmation(
        live, {LIVE_CONFIRMATION_ENV: LIVE_CONFIRMATION_VALUE}
    )


def test_demo_bridge_rejects_mainnet_and_missing_credentials():
    with pytest.raises(RuntimeError, match="REQUIRES_USE_TESTNET_TRUE"):
        validate_telegram_proposal_demo_mode(
            config(TELEGRAM_PROPOSAL_DEMO_MODE=True, USE_TESTNET=False)
        )
    with pytest.raises(RuntimeError, match="CREDENTIALS_MISSING"):
        validate_telegram_proposal_demo_mode(config(TELEGRAM_PROPOSAL_DEMO_MODE=True))


def test_demo_bridge_reports_bounded_cap_only_when_execution_is_enabled():
    demo = config(
        TELEGRAM_PROPOSAL_DEMO_MODE=True,
        BINANCE_API_KEY="demo-key",
        BINANCE_API_SECRET="demo-secret",
        DEMO_EXECUTION_ENABLED=True,
        TESTNET_MAX_OPEN_TRADES=8,
        HARD_MAX_OPEN_TRADES=5,
    )
    assert validate_startup(demo, {}) == {
        "enabled": True,
        "execution_enabled": True,
        "effective_position_cap": 5,
        "telegram_polling_enabled": True,
        "telegram_outbound_enabled": True,
        "account_management_enabled": True,
    }


def test_observation_mode_has_no_execution_capacity():
    demo = config(
        TELEGRAM_PROPOSAL_DEMO_MODE=True,
        BINANCE_API_KEY="demo-key",
        BINANCE_API_SECRET="demo-secret",
    )
    assert validate_telegram_proposal_demo_mode(demo)["effective_position_cap"] == 0
