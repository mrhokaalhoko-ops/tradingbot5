# Migration manifest

- Destination: `https://github.com/mrhokaalhoko-ops/tradingbot5`
- Source snapshot for `main`: `9072ffd5b855ea6a715ddb5b9743de1af422bf3d`
- Source version for `main`: `1.6.18`
- Source snapshot for `codex/protection-reliability-fix`: `db806eb7dea2527a91e24c0d3adae1427c28ba3e`
- Source version for `codex/protection-reliability-fix`: `1.6.19`
- Protection-fix source parent: `8c77b6861d47b2f44f94b1f56e797f8659bf4f61`
- Import method: clean snapshot with new Git history
- Excluded from the complete new history: `scratch/`, runtime logs, databases, ZIP files, trade journals, backup source files, generated reports, and local environment files
- Secrets: not included; configure them in Railway Variables only
- Deployment performed by this migration: no
- Latest verified source tests before migration: targeted `151 passed`; full regression `867 passed`, `0 failed`, `0 skipped`, with one pre-existing pandas `FutureWarning`
- Protection-fix secret scan before migration: PASS, `0` findings
- Clean-repository verification before push: targeted `178 passed`; full regression `867 passed`, `0 failed`, `0 skipped`
- The legitimate `scratch/test_bottom_fishing.py` test was moved to `tests/test_bottom_fishing.py`; no other `scratch` content was imported
