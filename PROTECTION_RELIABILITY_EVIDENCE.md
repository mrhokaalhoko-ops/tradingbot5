# Protection reliability candidate

The `codex/protection-reliability-fix` branch is a clean-history import of source commit:

`db806eb7dea2527a91e24c0d3adae1427c28ba3e`

Scope is limited to durable stop-protection recovery:

- no blind POST retry after ambiguous protection submission;
- exchange-confirmed protection is authoritative;
- same-stop replacement is a protected NOOP;
- old valid protection remains authoritative while an upgrade is uncertain;
- restart recovery is idempotent;
- duplicate lifecycle events cannot create duplicate protection;
- multiple-stop and restoration-failure states fail closed.

Verified in the source worktree before migration:

- targeted protection/safety tests: `151 passed`;
- complete project suite: `867 passed`, `0 failed`, `0 skipped`;
- one existing pandas `FutureWarning` only;
- secret scan: `PASS`, `0 findings`.

Reverified from this clean migration repository before push:

- targeted protection/safety/runtime-log tests: `178 passed`;
- complete project suite: `867 passed`, `0 failed`, `0 skipped`;
- the same pre-existing pandas `FutureWarning` only;
- full repository secret scan: `PASS`, `0 findings`.

This evidence does not replace the disabled Railway preflight and Demo reconciliation required before enabling execution.
