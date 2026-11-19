# Maintainer Guide

## Triage

Review new issues for security sensitivity before public discussion. Label confirmed bugs, enhancements, documentation, dependencies, research, and security-adjacent work. Ask for a minimal sanitized reproduction and close duplicates with a link. Do not accept screenshots/logs containing credentials or account identifiers.

Prioritize: unprotected position or unauthorized execution; secret exposure/auth bypass; state corruption/reconciliation failure; incorrect risk/order behavior; data/research correctness; reliability; documentation/enhancement.

## Pull request review

Require a linked problem, scoped diff, deterministic tests, exact verification results, and updated docs. For trading lifecycle changes, inspect authority, endpoint, idempotency, unknown outcomes, protection, crash recovery, state compatibility, and audit events. Require negative-path tests. Avoid combining formatting with behavior changes.

Recommended `main` rules: pull requests only, at least one approval, resolved conversations, no force pushes/deletion, and required quality/dependency/secret checks. Confirm actual check names after the first hosted run.

## Dependency updates

Dependabot opens bounded weekly updates. Review upstream changelogs, Python support, license changes, vulnerabilities, API behavior, and transitive changes. Run the full suite and audits. Exchange/client updates require response-schema and order-path regression review.

## Security reports

Follow `SECURITY.md`. Keep reports private, reproduce with synthetic data, assess affected branches/credentials, prepare tests/fix/advisory, coordinate disclosure, and rotate secrets if exposure is plausible. Never ask a reporter to prove impact with real funds.

## Versioning and breaking changes

After licensing, use Semantic Versioning: patch for compatible fixes, minor for compatible features, major for public API/state/config breaks. Before 1.0, clearly flag instability. A breaking state/config change needs migration, rollback, and compatibility documentation.

## Release checklist

1. Resolve or explicitly defer release-blocking issues.
2. Freeze scope and update `CHANGELOG.md` and version metadata.
3. Run the full tests, compile, Ruff, formatting, Bandit, pip-audit, and both secret scanners.
4. Review dependency licenses and generate an SBOM when release infrastructure exists.
5. Verify testnet/Demo defaults, live confirmation, endpoint guard, and recovery behavior.
6. Build the Docker image and smoke-test startup with execution disabled.
7. Review documentation and migration/rollback instructions.
8. Create a signed tag and GitHub release only with owner authorization.
9. Deploy with execution disabled, reconcile, then follow the Demo enablement gate.
10. Monitor and record sanitized post-deployment evidence.

## Documentation maintenance

Behavioral changes update README/operator docs in the same PR. Research changes update provenance and assumptions. Environment variables must appear in `.env.example`; deprecated variables need a migration window. Diagrams must match actual authority and data flow.

## How Codex can assist

Codex can inspect diffs, generate failure-injection tests, trace dependency/call paths, refactor isolated interfaces, update docs, run quality checks, and summarize review risks. Maintainers remain responsible for validating exchange semantics, research claims, legal rights, security disclosure, merge decisions, releases, and any execution enablement. Do not use generated activity or content to simulate adoption.
