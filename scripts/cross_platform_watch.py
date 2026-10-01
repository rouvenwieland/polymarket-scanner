"""
Hochfrequenter Watcher für den Cross-Platform-Fonds (für GitHub Actions)
============================================================================

Analog zu fund_watch.py, aber für die drei Cross-Platform-Fonds
(cross_platform_fund.py, 100/1.000/10.000 USD Start). Kauft NICHTS Neues -
prüft nur, ob eine der schon gehaltenen Positionen inzwischen (auf beiden
Beinen) aufgelöst wurde. Ein Fonds mit leerem Portfolio verursacht dabei
keinen API-Call.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import cross_platform_fund
import fund_simulator


def main():
    now = datetime.now(ZoneInfo(fund_simulator.TIMEZONE))
    cross_platform_fund.watch_all_positions_step(now)


if __name__ == "__main__":
    main()
