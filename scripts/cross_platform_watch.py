"""
Hochfrequenter Watcher für den Cross-Platform-Fonds (für GitHub Actions)
============================================================================

Analog zu fund_watch.py, aber für den 4. Fonds (cross_platform_fund.py).
Kauft NICHTS Neues - prüft nur, ob eine der schon gehaltenen Positionen
inzwischen (auf beiden Beinen) aufgelöst wurde. Bei leerem Portfolio
beendet sich das Script sofort.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import cross_platform_fund
import fund_simulator


def main():
    now = datetime.now(ZoneInfo(fund_simulator.TIMEZONE))
    cross_platform_fund.watch_step(now)


if __name__ == "__main__":
    main()
