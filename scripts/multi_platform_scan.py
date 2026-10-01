"""
Multi-Platform-Scan (für GitHub Actions)
===========================================

Erweitert den reinen Polymarket-Scan (polymarket_snapshot.py) um Kalshi und
PredictIt:
  1. Lädt offene Märkte aller drei Plattformen (normalisiertes Schema,
     siehe scripts/platforms/).
  2. Schreibt eine plattformübergreifende Übersicht (data/platform_overview.json) -
     Marktzahlen je Plattform, Basis für die größere "Übersicht" im Dashboard.
  3. Sucht plattformübergreifende Matches (cross_platform.py) und filtert
     daraus Yes/No-Arbitrage-Gelegenheiten (Kombi-Summe < 1 nach grob
     geschätzten Gebühren) - geschrieben nach data/cross_platform_hits.csv.
  4. Lässt die drei Cross-Platform-Fonds (cross_platform_fund.py, 100/1.000/
     10.000 USD Start - identische Strategie, nur Startkapital unterschiedlich)
     je einen Schritt laufen: offene Positionen bewerten/auflösen, neue
     Treffer ggf. kaufen.
  5. Lässt zusätzlich zwei Single-Platform-Fonds (single_platform_fund.py)
     je einen Schritt laufen - dieselbe Yes+No<1-Strategie wie beim
     Polymarket-"conservative"-Fonds, aber auf Kalshis bzw. PredictIts
     EIGENEN Märkten (keine Cross-Platform-Matches nötig), um zu sehen, ob
     sich die Idee auch innerhalb jeder einzelnen Plattform lohnt.

Läuft unabhängig vom bestehenden Polymarket-only-Scan (polymarket_snapshot.py)
und dessen drei Fonds (fund_simulator.py) - deren Daten/State bleiben
unberührt. Das Commit + Push übernimmt der GitHub-Actions-Workflow.
"""

import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import cross_platform
import cross_platform_fund
import single_platform_fund
from platforms import kalshi, polymarket, predictit

TIMEZONE = "Europe/Berlin"
DATA_DIR = "data"
OVERVIEW_JSON = os.path.join(DATA_DIR, "platform_overview.json")
CROSS_HITS_CSV = os.path.join(DATA_DIR, "cross_platform_hits.csv")


def fetch_all_platforms():
    """Lädt alle drei Plattformen. Ein Fehler auf einer Plattform (z.B.
    Kalshi oder PredictIt temporär nicht erreichbar) darf den gesamten Lauf
    nicht abbrechen - die jeweiligen Connectoren fangen Netzwerkfehler ab
    und liefern dann einfach eine leere Liste zurück."""
    markets_by_platform = {
        "polymarket": polymarket.fetch_all_normalized(),
        "kalshi": kalshi.fetch_all_normalized(),
        "predictit": predictit.fetch_all_normalized(),
    }
    for name, markets in markets_by_platform.items():
        print(f"{name}: {len(markets)} offene Märkte geladen.")
    return markets_by_platform


def write_overview(markets_by_platform, now):
    os.makedirs(DATA_DIR, exist_ok=True)
    overview = {
        "timestamp": now.isoformat(),
        "platforms": {
            name: {"open_markets": len(markets)}
            for name, markets in markets_by_platform.items()
        },
        "total_open_markets": sum(len(m) for m in markets_by_platform.values()),
    }
    with open(OVERVIEW_JSON, "w", encoding="utf-8") as f:
        json.dump(overview, f, ensure_ascii=False, indent=2)


def write_cross_platform_hits(matches, now):
    """Schreibt JEDEN gefundenen Cross-Platform-Match mit Arbitrage-Summe in
    eine CSV an (Rohdaten, ein Eintrag pro Markt-Paar pro Run) - unabhängig
    davon, ob der Fonds daraus tatsächlich eine Position eröffnet hat."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fieldnames = [
        "timestamp", "plattform_a", "frage_a", "plattform_b", "frage_b",
        "seite_a", "seite_b", "summe", "score", "url_a", "url_b",
    ]
    file_exists = os.path.isfile(CROSS_HITS_CSV)
    rows = []
    for match in matches:
        ma, mb = match["market_a"], match["market_b"]
        side_a, side_b, summe = cross_platform.best_arbitrage_combo(ma, mb)
        if summe >= 1.0:
            continue  # kein Arbitrage-Vorteil (vor Gebühren) - für die Übersicht uninteressant
        rows.append({
            "timestamp": now.isoformat(),
            "plattform_a": ma["platform"], "frage_a": ma["question"],
            "plattform_b": mb["platform"], "frage_b": mb["question"],
            "seite_a": side_a, "seite_b": side_b,
            "summe": round(summe, 4), "score": round(match["score"], 3),
            "url_a": ma.get("url", ""), "url_b": mb.get("url", ""),
        })
    if not rows:
        return
    with open(CROSS_HITS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} Cross-Platform-Arbitrage-Treffer in diesem Lauf.")


def main():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    print(f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')}: Multi-Platform-Scan startet...")

    markets_by_platform = fetch_all_platforms()
    write_overview(markets_by_platform, now)

    matches = cross_platform.find_cross_platform_matches(
        markets_by_platform,
        threshold=cross_platform_fund.MATCH_THRESHOLD,
        max_days=cross_platform_fund.MATCH_MAX_DAYS,
    )
    print(f"{len(matches)} plattformübergreifende Markt-Matches gefunden.")
    write_cross_platform_hits(matches, now)

    cross_platform_fund.run_all_fund_steps(matches, now)
    single_platform_fund.run_all_fund_steps(markets_by_platform, now)


if __name__ == "__main__":
    main()
