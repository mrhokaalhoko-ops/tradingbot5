"""Read-only Binance Demo credential preflight; this script cannot place orders."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_execution.client import DemoBinanceClient
from demo_execution.config import DemoExecutionConfig


def main() -> int:
    try:
        config = DemoExecutionConfig.from_env()
        if not config.enabled:
            raise RuntimeError("PREFLIGHT_REQUIRES_TEMPORARY_LOCAL_ENABLE")
        client = DemoBinanceClient(config)
        proof = client.prove_environment()
        position_mode = client.position_mode()
        account = client.account()
        positions = client.position_risk()
        open_positions = [
            row for row in positions
            if abs(float(row.get("positionAmt") or 0.0)) > 0.0
        ]
        open_orders = client.open_orders()
        open_algo_orders = client.open_algo_orders()
        result = {
            "authenticated": True,
            "can_trade": bool(account.get("canTrade")),
            "endpoint": {
                "host": proof["host"],
                "is_demo_or_testnet": proof["is_demo_or_testnet"],
                "is_mainnet": proof["is_mainnet"],
                "server_time_utc": proof["server_time_utc"],
                "symbols_reported": proof["symbols_reported"],
            },
            "hedge_mode": bool(position_mode.get("dualSidePosition")),
            "open_position_count": len(open_positions),
            "open_order_count": len(open_orders) + len(open_algo_orders),
            "open_regular_order_count": len(open_orders),
            "open_algo_order_count": len(open_algo_orders),
            "read_only_calls_only": True,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        result = {
            "authenticated": False,
            "read_only_calls_only": True,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
