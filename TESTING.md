# Testing

## Safety contract

Tests must never use production credentials, query a live balance, or place a real order. Execution tests use mocks/fakes, temporary stores, deterministic clocks, or isolated Binance Demo/Testnet endpoints only when an operator explicitly runs a separate smoke tool. CI sets testnet and disables both Demo execution flags.

## Local suite

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Use a temporary `DATA_DIR` when testing runtime initialization. Never point tests at a production volume or copy real `.env` values into fixtures.

## Quality and security checks

```bash
python -m compileall -q app.py core demo_execution engines filters shadow_runtime strategies strategy trading_v2 utils webapp
ruff check .
ruff format --check app.py core/startup.py scripts/secret_scan.py tests/test_startup_policy.py tests/test_secret_scan.py
bandit -q -r core demo_execution shadow_runtime trading_v2 strategy -lll -iii
pip-audit -r requirements.txt
python scripts/secret_scan.py
```

Ruff currently enforces correctness rules repository-wide and formatting on the actively maintained readiness surface. Expanding formatting must be a dedicated, behavior-neutral change because legacy modules are large and reviewability matters.

## Test categories

- Strategy/indicator tests cover deterministic calculations, regimes, candidate scoring, and entry quality.
- Replay/research tests cover chronology, causal features, split stability, data schemas, costs, and integrity oracles.
- Risk tests cover sizing, margin availability, loss budgets, leverage and portfolio caps.
- Execution tests cover deterministic IDs, duplicates, protective orders, ambiguous responses, retries, partial failures, and reconciliation.
- Recovery tests cover state corruption, backups, writer leases, restart state, trailing/protection recovery, and persistence failures.
- Security tests cover startup confirmation, endpoint allowlists, logging redaction, Telegram authorization, and secret patterns.

## Determinism rules

- Inject clocks/sleep/random generators rather than waiting on wall time.
- Use fixed timestamps and explicit UTC time zones.
- Never depend on current exchange data in pytest.
- Use `tmp_path`/temporary directories for databases, state, logs, and journals.
- Mock complete exchange responses, including error codes and ambiguous timeouts.
- Tests for causality must perturb future observations and prove earlier decisions do not change.
- Do not skip, weaken, or broaden tolerances just to make a failure disappear.

## Warnings and coverage

Unhandled thread and unraisable-object warnings are errors. One known pandas future warning in the historical replay concatenation is filtered centrally and remains technical debt. Coverage can be generated with `pytest --cov --cov-report=term-missing`; no global threshold is claimed until a stable module baseline is reviewed.

## Manual Demo evidence

Manual Demo smoke tests are separate from pytest. Start with execution disabled, run endpoint/preflight and reconciliation scripts, capture sanitized evidence, then enable only the Demo executor if the owner has authorized it. Never convert a Demo smoke procedure into an automatic live test.
