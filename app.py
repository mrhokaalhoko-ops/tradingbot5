"""TradingBot5 process entry point.

Keeping process startup separate from ``main.py`` lets the legacy orchestration
module remain importable for tests while deployment has one explicit launcher.
"""

from main import main


if __name__ == "__main__":
    main()
