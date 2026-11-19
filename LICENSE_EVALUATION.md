# License Evaluation

## Current status

No `LICENSE` file is present and no open-source permission is granted. This is a blocker for genuine open-source distribution and outside contribution. A license must not be added until the owner confirms copyright authority and compatibility of imported code, generated/third-party assets, and historical contributions.

This is an engineering assessment, not legal advice.

## Options

| License | Benefits | Obligations/tradeoffs | Fit for TradingBot5 |
| --- | --- | --- | --- |
| MIT | Short, permissive, familiar; allows commercial and closed-source reuse | Preserve copyright/license notice; no explicit patent grant | Simple, but weaker patent clarity for exchange/automation software |
| Apache-2.0 | Permissive; explicit patent license/termination; clear contribution and NOTICE mechanics | Preserve license/notices, mark modified files, manage NOTICE if used; longer text | Best balance for broad use and patent clarity |
| GPL-3.0 | Strong reciprocal source availability for distributed derivatives; explicit patents | Derivative distributions must use GPL-3.0 and provide corresponding source; can reduce proprietary adoption; network use alone is not AGPL | Valid if copyleft is an intentional owner goal, not the default recommendation |

## Dependency review

The declared runtime/development packages inspected locally reported MIT, BSD-family, or Apache-family licenses, including CCXT, pandas, NumPy, TA, Requests, python-dotenv, Groq, psutil, Flask, websockets, OpenAI, Google Generative AI, cachetools, jsonschema, pytest, PyArrow, scikit-learn, Ruff, Bandit, and pip-audit. These are generally compatible with MIT, Apache-2.0, and GPL-3.0 aggregation/use.

The developer machine contains unrelated packages under GPL/LGPL/MPL licenses, but they are not declared TradingBot5 dependencies and are not part of this assessment. A clean-environment SBOM and license scan must be repeated for a release. Dependency licenses do not resolve ownership of this repository's source or assets.

## Recommendation

**Recommend Apache License 2.0**, assuming the provenance review passes and the owner explicitly authorizes it. The project is a reusable financial automation/research platform; Apache-2.0 preserves permissive adoption while providing an explicit patent grant and contributor patent protection that MIT lacks.

Choose MIT instead if minimal text and maximum simplicity outweigh patent clarity. Choose GPL-3.0 only if the owner deliberately wants reciprocal licensing for distributed derivatives and accepts the ecosystem tradeoff.

## Required authorization/provenance checklist

- Confirm the repository owner owns or has relicensing permission for all imported source history.
- Identify every contributor and confirm their contribution terms.
- Review `assets/icons/` and other media for source, author, license, and redistribution rights; replace or remove uncertain assets.
- Review copied/generated snippets and research code for notices or upstream licenses.
- Produce a clean-environment dependency/SBOM/license report.
- Decide copyright holder/year and whether a `NOTICE` file is needed.
- Record the owner's explicit choice (Apache-2.0, MIT, or GPL-3.0).
- Only then add the exact OSI license text and update package metadata/headers/docs.

## Approval status

Pending owner authorization and provenance verification. No `LICENSE` file was added.
