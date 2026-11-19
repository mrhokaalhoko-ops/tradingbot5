# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) structure. Semantic Versioning is the intended policy after the first licensed release; current version labels are pre-release engineering identifiers.

## [Unreleased]

### Added

- Fail-closed, dependency-free startup policy and explicit `app.py` launcher.
- Startup and tracked-secret scanner regression tests.
- Python 3.10/3.11 CI, compilation, Ruff, Bandit, pip-audit, Gitleaks, dependency review, and Dependabot.
- Architecture, security, testing, deployment, research, governance, maintainer, audit, and license-evaluation documentation.
- GitHub issue forms and pull request template.

### Changed

- Integrated the existing linear protection/recovery, scanner isolation, strategy-shadow, and Demo safety branch series.
- Added compatible dependency bounds and declared Docker's `jsonschema` dependency in `requirements.txt`.
- Aligned Docker and Railway startup on `app.py`.
- Replaced the migration-only README with project and operator documentation.

### Security

- Made live/Testnet/Demo startup authorization directly testable before external client construction.
- Added automated dependency and secret checks.

## Release history

The repository has no verified public release/tag history yet. Historical internal version strings and research reports must not be presented as published releases without matching signed tags and release notes.
