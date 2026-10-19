"""One-time guarded switch of a flat Binance Demo account to one-way mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo_execution.client import DemoBinanceClient
from demo_execution.config import DemoExecutionConfig


def main() -> int:
    if os.getenv("DEMO_CONFIRM_SET_ONE_WAY", "").strip().lower() != "true":
        raise RuntimeError("DEMO_ONE_WAY_SWITCH_NOT_CONFIRMED")
    config = DemoExecutionConfig.from_env()
    client = DemoBinanceClient(config)
    proof = client.prove_environment()
    if not proof.get("is_demo_or_testnet") or proof.get("is_mainnet"):
        raise RuntimeError("DEMO_ENVIRONMENT_PROOF_FAILED")
    positions = [
        row for row in client.position_risk()
        if abs(float(row.get("positionAmt") or 0.0)) > 0.0
    ]
    orders = client.open_orders()
    if positions or orders:
        raise RuntimeError("DEMO_ACCOUNT_NOT_FLAT_REFUSE_MODE_CHANGE")
    before = bool(client.position_mode().get("dualSidePosition"))
    if before:
        client.set_one_way_position_mode()
    after = bool(client.position_mode().get("dualSidePosition"))
    result = {
        "endpoint_host": proof["host"],
        "is_mainnet": proof["is_mainnet"],
        "open_position_count": len(positions),
        "open_order_count": len(orders),
        "hedge_mode_before": before,
        "hedge_mode_after": after,
        "one_way_mode": not after,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not after else 2


if __name__ == "__main__":
    raise SystemExit(main())
