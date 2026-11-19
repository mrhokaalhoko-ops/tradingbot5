# Engineering Audit

Audit date: 2026-11-19. Baseline: `origin/main` at `a02add209e151e3eb3a23e34eee2f912b3380084`.

## Scope and baseline

The audit covered tracked Python modules, tests, Docker/Railway configuration, environment examples, documentation, Git history, and all visible `origin` branches. The repository was cloned from `https://github.com/mrhokaalhoko-ops/tradingbot5.git`; the surrounding local workspace pointed at a different repository and was left untouched.

The baseline full suite ran with external execution disabled: 820 tests passed in 28.07 seconds with one pandas deprecation warning. `origin/codex/runtime-safety-review-fixes` was a linear descendant of `main` containing protection, recovery, isolation, and test work; it was fast-forwarded into this feature branch before the new readiness changes.

## Initial findings and treatment

| Area | Finding | Severity | Treatment |
| --- | --- | --- | --- |
| Repository | Important safety work lived only on descendant branches | High | Integrated the complete linear safety series |
| Startup | Deployment entered an ~8,000-line orchestration module directly | High | Added `app.py`; extracted a dependency-free startup policy |
| Live authority | Explicit confirmation existed inside the composition module | Critical if regressed | Preserved and directly tested the pre-client fail-closed guard |
| Demo isolation | Multiple execution surfaces required endpoint/authority review | Critical | Integrated allowlisted endpoint, idempotency, protection and recovery tests |
| State | Crash recovery and ambiguous writes are complex | High | Retained durable intents, IDs, writer lease, backup recovery, reconciliation tests |
| Architecture | Main, Telegram, exchange, and state modules are oversized | Medium | Documented debt; limited extraction to startup to reduce lifecycle risk |
| Configuration | Environment values are import-time and distributed | Medium | Canonical docs and policy validation; immutable settings remain roadmap work |
| Dependencies | Packages were mostly unbounded; Docker installed an undeclared package | High | Added ranges, declared `jsonschema`, added audit automation |
| CI | No workflow, lint config, dependency review, or secret scan | High | Added matrix tests, compile, Ruff, Bandit, pip-audit, Gitleaks, Dependabot |
| Tests | Strong suite lacked centralized policy; one warning | Medium | Added `pyproject.toml`, warning handling, startup/secret-scan tests |
| Documentation | README was migration-only; core project docs absent | High | Added operator, contributor, research, security, and governance docs |
| Licensing | No license or verified provenance for imported code/assets | OSS blocker | Recommended Apache-2.0; intentionally did not add `LICENSE` |
| Research | Many scripts/results but no single reproducibility contract | High | Added provenance, causality, splits, costs, and negative-result protocol |
| Web/auth | Dashboard is not documented as a multi-tenant control plane | Medium | Made limitation and deployment restriction explicit |

## Security and trading review

Reviewed paths default to testnet and disabled Demo execution. Credentials come from environment variables, known secrets are redacted, mainnet requires an exact operator confirmation, Demo REST endpoints are allowlisted, client IDs are deterministic, exchange responses are validated, ambiguous writes enter reconciliation, protective failures block recovery, and single-writer/state recovery controls have tests.

No real order, live balance, or production credential was used. Public network market-data collectors and isolated Demo/Testnet adapters do not grant live trading authority.

## Duplication and dependency risks

Lifecycle checks remain across `main.py`, `core/exchange.py`, `demo_execution`, state management, and Telegram rendering. Some is intentional defense in depth; some is legacy coupling. Consolidation must preserve independent endpoint, authority, risk, and state gates.

No circular import prevented test collection, but local imports in large modules indicate coupling and import-order sensitivity. The next architecture step is typed interfaces and constructor injection for exchange, state, notification, and audit services.

## Evidence limits

Local tests cannot establish hosted GitHub Actions success until the branch is pushed. They also cannot prove exchange availability, profitability, production reliability, legal ownership, or community adoption.
