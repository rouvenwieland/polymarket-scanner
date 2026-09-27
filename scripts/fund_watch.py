"""
Hochfrequenter Fonds-Watcher (für GitHub Actions)
===================================================

Läuft viel öfter als der große Scan (siehe .github/workflows/fund_watch.yml,
typischerweise alle 2 Minuten). Kauft NICHTS Neues - der große Scan
(polymarket_snapshot.py) bleibt das "Portfolio", aus dem neue Positionen
ausgewählt werden. Dieses Script prüft nur, ob eine der schon gehaltenen
Positionen inzwischen aufgelöst wurde oder wieder auf ~1.00 konvergiert ist,
per gezieltem Einzel-Lookup pro Position (billig, kein voller Scan nötig).

Bei leerem Portfolio beendet sich das Script sofort (kein API-Call, kein
Commit) - dann übernimmt wieder der nächste große Scan.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import fund_simulator


def main():
    now = datetime.now(ZoneInfo(fund_simulator.TIMEZONE))
    fund_simulator.watch_positions_step(now)


if __name__ == "__main__":
    main()
