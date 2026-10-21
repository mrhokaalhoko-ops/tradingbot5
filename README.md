# tradingbot5

Clean migration repository for the `trversion4` Telegram trading bot.

## Safety boundary

- Market/account execution is Binance USD-M **Demo/Testnet only**.
- Keep `USE_TESTNET=true`.
- Never commit `.env`, API keys, Telegram tokens, runtime databases, logs, ZIP exports, or trade journals.
- Runtime state belongs on a Railway persistent volume mounted at `/app/data`.
- Railway starts the service with `python -u main.py` using the included `Dockerfile` and `railway.json`.

### Mainnet safety boundary

The shared codebase retains a deliberately explicit legacy live-trading escape
hatch for other deployments. It remains blocked unless both
`USE_TESTNET=false` and the exact `LIVE_TRADING_CONFIRMATION` acknowledgement
are supplied. That capability is **not authorized for this repository's
`tradingbot5` Railway service**: its supported runtime contract is
`USE_TESTNET=true`, Binance Demo endpoints, and `mainnet_order_route=false`.
Do not set the live acknowledgement in this service.

## Branches

- `main`: clean rollback/reference snapshot of version `1.6.18`.
- `codex/protection-reliability-fix`: tested `1.6.19` candidate with durable stop-protection recovery. Use this branch for the new Demo deployment after the disabled preflight gate.

## Railway migration gate

1. Connect Railway to `codex/protection-reliability-fix`, not `main`.
2. Mount a fresh persistent volume at `/app/data`.
3. Add secrets directly in Railway Variables. Never paste them into GitHub.
4. Start with execution disabled and complete reconciliation of Demo positions, regular orders, Algo orders, and local state.
5. Verify `demo-fapi.binance.com`, `mainnet_order_route=false`, and a clean reconciliation before enabling Demo execution.
6. On the first natural trade, verify one entry, confirmed protection, idempotent stop replacement, exit, and final reconciliation.

See `.env.example` for variable names and non-secret defaults.
