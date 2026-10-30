"""
Multi-Platform-Scan (für GitHub Actions)
===========================================

Erweitert den reinen Polymarket-Scan (polymarket_snapshot.py) um Kalshi,
PredictIt und SX Bet (siehe platforms/sxbet.py - real-money On-Chain-
Sportwetten-Börse, zusätzliche Plattform um den Vergleichsradius auf mehr
Nischenmärkte auszuweiten):
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import arbitrage_check
import cross_platform
import cross_platform_fund
import single_platform_fund
from platforms import kalshi, polymarket, predictit, sxbet

TIMEZONE = "Europe/Berlin"
DATA_DIR = "data"
OVERVIEW_JSON = os.path.join(DATA_DIR, "platform_overview.json")
CROSS_HITS_CSV = os.path.join(DATA_DIR, "cross_platform_hits.csv")
# GitHub lehnt Dateien > 100 MiB ab (Push wird mit "pre-receive hook declined" abgewiesen). Die Treffer-
# Datei wuchs unbegrenzt (Stand 13.10.: 95,8 MiB, ~2 MB/Tag). Ab dieser Groesse beginnt eine neue Teildatei
# (cross_platform_hits_002.csv, ...); die alte bleibt unveraendert im Repo.
HITS_MAX_BYTES = 50 * 1024 * 1024
# Datenwachstum begrenzen (30.10.2026): Zwei Laeufe am 29.10. schrieben je ~25.000 Zeilen (fast nur abgelehnte
# Fussball-Genauergebnis-Paare), das sind ~12 MB/Tag. Zeilen mit Ergebnis "ok" werden immer geschrieben; abgelehnte
# Zeilen nur bis zu dieser Zahl je Lauf (bevorzugt mit hohem Aehnlichkeitswert). Der Rest wird nur gezaehlt.
HITS_MAX_REJECTED_PER_RUN = 1500


HITS_FIELDNAMES = [
    "timestamp", "plattform_a", "frage_a", "plattform_b", "frage_b",
    "seite_a", "seite_b", "summe", "score", "url_a", "url_b",
    # seit 27.10.2026: Markt-IDs, Beinpreise, Preisbasis, Seitenzuordnung und Ergebnis der Strategie-Pruefung,
    # damit spaetere Neuberechnungen nicht mehr aus der Kombi-Summe zurueckrechnen muessen
    "market_id_a", "market_id_b", "preis_a", "preis_b", "basis_a", "basis_b", "zuordnung", "pruefung",
]


def _header(path):
    with open(path, "r", newline="", encoding="utf-8") as f:
        return next(csv.reader(f), None)


def hits_target_path(base=None, max_bytes=None, fieldnames=None):
    """Aktive Treffer-Datei: die erste Teildatei (base, base_002, base_003, ...), die noch unter dem
    Groessenlimit liegt und (falls fieldnames angegeben) denselben Spaltenkopf hat. Eine Datei mit altem
    Spaltenkopf bleibt unveraendert; neue Zeilen kommen dann in die naechste Teildatei."""
    base = base or CROSS_HITS_CSV
    max_bytes = HITS_MAX_BYTES if max_bytes is None else max_bytes
    root, ext = os.path.splitext(base)
    path, n = base, 1
    while os.path.isfile(path) and (os.path.getsize(path) >= max_bytes
                                    or (fieldnames is not None and _header(path) != list(fieldnames))):
        n += 1
        path = f"{root}_{n:03d}{ext}"
    return path


def fetch_all_platforms():
    """Lädt alle drei Plattformen PARALLEL (ThreadPoolExecutor - die
    Fetches sind I/O-gebunden, nicht CPU-gebunden, daher funktioniert das
    trotz Python-GIL gut). Sequentiell hintereinander würde allein
    Polymarkts ~10-Minuten-Vollscan den Start von Kalshi/PredictIt
    unnötig verzögern und zusammen mit Kalshis Retry-Handling bei
    Rate-Limiting leicht das Zeitbudget des GitHub-Actions-Laufs sprengen.
    Ein Fehler auf einer Plattform (z.B. Kalshi oder PredictIt temporär
    nicht erreichbar) darf den gesamten Lauf nicht abbrechen - die
    jeweiligen Connectoren fangen Netzwerkfehler ab und liefern dann
    einfach eine leere Liste zurück."""
    fetchers = {
        "polymarket": polymarket.fetch_all_normalized,
        "kalshi": kalshi.fetch_all_normalized,
        "predictit": predictit.fetch_all_normalized,
        "sxbet": sxbet.fetch_all_normalized,
    }
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {name: pool.submit(fn) for name, fn in fetchers.items()}
        markets_by_platform = {name: future.result() for name, future in futures.items()}
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
    fieldnames = HITS_FIELDNAMES
    target = hits_target_path(fieldnames=fieldnames)
    file_exists = os.path.isfile(target)
    rows = []
    for match in matches:
        ma, mb = match["market_a"], match["market_b"]
        # Seitenzuordnung (Reparatur 27.10.2026): bei vertauschter Teamreihenfolge sichern Yes+Yes bzw. No+No ab.
        # Unklare Zuordnung: Kombination wie frueher ("same"), aber als "unklar" gekennzeichnet (nie handelbar).
        alignment = cross_platform.outcome_alignment(ma, mb)
        side_a, side_b, summe = cross_platform.best_arbitrage_combo(ma, mb, alignment or "same")
        if summe >= 1.0:
            continue  # kein Arbitrage-Vorteil (vor Gebühren) - für die Übersicht uninteressant
        chk = arbitrage_check.check_pair(ma, mb, now, alignment) if alignment else None
        rows.append({
            "timestamp": now.isoformat(),
            "plattform_a": ma["platform"], "frage_a": ma["question"],
            "plattform_b": mb["platform"], "frage_b": mb["question"],
            "seite_a": side_a, "seite_b": side_b,
            "summe": round(summe, 4), "score": round(match["score"], 3),
            "url_a": ma.get("url", ""), "url_b": mb.get("url", ""),
            "market_id_a": ma.get("market_id", ""), "market_id_b": mb.get("market_id", ""),
            "preis_a": round(ma["yes_price"] if side_a == "yes" else ma["no_price"], 4),
            "preis_b": round(mb["yes_price"] if side_b == "yes" else mb["no_price"], 4),
            "basis_a": ma.get("price_basis", ""), "basis_b": mb.get("price_basis", ""),
            "zuordnung": alignment or "unklar",
            "pruefung": ("ok" if chk is not None and chk.ok else
                         "Seitenzuordnung unklar" if chk is None else "; ".join(chk.reasons))[:300],
        })
    if not rows:
        return
    rejected = [r for r in rows if r["pruefung"] != "ok"]
    if len(rejected) > HITS_MAX_REJECTED_PER_RUN:
        keep = sorted(rejected, key=lambda r: -r["score"])[:HITS_MAX_REJECTED_PER_RUN]
        keep_ids = {id(r) for r in keep}
        omitted = len(rejected) - len(keep)
        rows = [r for r in rows if r["pruefung"] == "ok" or id(r) in keep_ids]
        print(f"{omitted} abgelehnte Treffer nicht geschrieben (Limit {HITS_MAX_REJECTED_PER_RUN} je Lauf).")
    with open(target, "a", newline="", encoding="utf-8") as f:
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
