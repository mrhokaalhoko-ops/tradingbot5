"""Application release and deployment identity.

The human-readable version is intentionally stored in source control, while
Railway supplies the exact Git/deployment identity at runtime.
"""

import os


APP_VERSION = "1.6.19"

GIT_COMMIT = os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip()
GIT_BRANCH = os.getenv("RAILWAY_GIT_BRANCH", "").strip()
GIT_REPO = os.getenv("RAILWAY_GIT_REPO_NAME", "").strip()
DEPLOYMENT_ID = os.getenv("RAILWAY_DEPLOYMENT_ID", "").strip()

SHORT_COMMIT = GIT_COMMIT[:7] if GIT_COMMIT else "local"
VERSION_LABEL = f"{APP_VERSION} • {SHORT_COMMIT}"
