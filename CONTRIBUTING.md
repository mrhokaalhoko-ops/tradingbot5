# Contributing

TradingBot5 welcomes review and contributions after the licensing decision is completed. Until a license is added, source availability does not grant redistribution rights; discuss substantial work with the maintainer first.

## Before coding

- Search existing issues and read `ARCHITECTURE.md`, `SECURITY.md`, and `TESTING.md`.
- For execution, risk, state schema, or research methodology changes, open an issue describing invariants and evidence.
- Never use live credentials or real orders. Never include secrets or private account data.
- Preserve strategy identity and do not tune risk/strategy parameters merely to improve a performance report.

## Development workflow

1. Branch from current `main` with a focused name.
2. Create a clean virtual environment and install `requirements-dev.txt`.
3. Make the smallest coherent change with type hints and injected dependencies where practical.
4. Add deterministic regression tests, including failure/unknown outcomes for exchange writes.
5. Run every command in `TESTING.md` and update relevant documentation.
6. Use conventional commit subjects such as `fix:`, `feat:`, `test:`, `docs:`, `refactor:`, or `ci:`.
7. Open a pull request using the template; link the issue and state exact test results.

## Code standards

- Keep business policy independent of network/framework adapters.
- Validate external inputs and exchange response shapes; do not silently catch broad exceptions.
- Log actionable context without secrets. Preserve exception causes when wrapping errors.
- A timeout after a write is an unknown outcome requiring idempotent reconciliation.
- Persist state deliberately and keep migrations backward-compatible or explicitly versioned.
- Use UTC timestamps and causal data cutoffs in research.
- Avoid large mechanical formatting changes mixed with behavior changes.

## Pull request review

Reviewers verify scope, safety invariants, authorization boundaries, backward compatibility, deterministic tests, documentation, and research causality. Changes to execution/protection/recovery should include at least one negative-path or failure-injection test. Authors must resolve discussions rather than dismiss CI failures.

## Good first contributions

Documentation corrections, isolated type annotations, deterministic tests for pure research functions, and small adapter/interface extractions are lower risk. Live execution, authentication, state migration, and protection changes require maintainer coordination.
