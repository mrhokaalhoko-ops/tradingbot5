## Summary

Describe the problem, the outcome, and why this scope is appropriate.

## Risk and authority review

- [ ] No live trade, real balance, or production credential was used.
- [ ] Mainnet/Testnet/Demo authority and endpoint behavior are unchanged, or the change is explained below.
- [ ] Duplicate/unknown order outcomes, protection, recovery, concurrency, and state compatibility were considered where relevant.
- [ ] Research changes document timestamps, causality, costs, splits, provenance, and rejected variants.
- [ ] No profitability, reliability, or adoption claim is unsupported.

## Verification

List exact commands and results. Do not write “tests pass” without counts.

```text
python -m pytest -q
ruff check .
python -m pip_audit -r requirements.txt
python scripts/secret_scan.py
```

## Documentation and compatibility

- [ ] Tests cover the change and important failure paths.
- [ ] README/operator/contributor documentation is updated.
- [ ] `.env.example` is updated for configuration changes.
- [ ] Migration and rollback are documented for state/config/API changes.
- [ ] Changelog entry added when user/operator behavior changes.

## Linked issue

Closes #
