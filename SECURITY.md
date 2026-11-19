# Security Policy

## Reporting a vulnerability

Do not open a public issue for vulnerabilities, credential exposure, exchange/account identifiers, or a method that could place/cancel orders. Use the repository's **Security → Report a vulnerability** private advisory flow. If private advisories are unavailable, contact the repository owner privately and request a secure channel before sharing details.

Include affected commit/version, impact, prerequisites, a minimal sanitized reproduction, and suggested mitigation. Never include real API keys, Telegram tokens, balances, order IDs, or unredacted logs.

The maintainer should acknowledge a report within 3 business days when possible, triage severity, agree on disclosure timing, and credit the reporter if requested. No bounty is promised.

## Supported versions

Until formal releases exist, only the current `main` branch and an explicitly named active release-candidate branch receive security fixes. Historical research branches and artifacts are unsupported.

## Credential handling

- Secrets belong in environment variables or a managed secret store.
- Use distinct Demo and production credentials; development/CI must never receive production credentials.
- Grant the minimum exchange permissions and do not enable withdrawals.
- Rotate potentially exposed credentials immediately. Removing a value from the current tree does not remove Git history, logs, caches, or artifacts.
- Logging and export code must pass content through redaction, and tests must use synthetic tokens.

## Execution threat model

Primary risks are unauthorized live mode, endpoint confusion, duplicate writes after timeouts, missing protection, stale/invalid prices, compromised Telegram authorization, concurrent writers, state corruption, dependency compromise, and secret leakage.

Controls include testnet defaults, exact live confirmation, Demo hostname allowlists, explicit execution flags, deterministic order IDs, durable intents, exchange reconciliation, validated responses, protection recovery, writer leases, authorized Telegram user checks, secret scanning, dependency review, and vulnerability auditing.

## Incident response

1. Disable new order authority and isolate the affected runtime.
2. Revoke/rotate credentials and invalidate Telegram/web access if relevant.
3. Inspect positions and protective orders directly through the trusted exchange interface.
4. Preserve sanitized logs, state, decision audits, commit IDs, and timestamps.
5. Identify whether outcomes are known, failed, partial, or ambiguous; never assume a timeout means no order.
6. Patch with regression tests, review, and Demo verification.
7. Publish an advisory and remediation guidance after coordinated disclosure.

## Scope limitations

Automated checks reduce risk but do not constitute a security certification. The Flask dashboard is not a hardened multi-tenant control plane. Exchange, hosting, Telegram, AI-provider, and dependency security remain shared responsibilities.
