"""
Polymarket Snapshot (für GitHub Actions)
==========================================

Wird rund um die Uhr von einem Cron-Workflow aufgerufen (siehe
INTERVAL_MINUTES / die .yml für den genauen Takt):
  1. Lädt ALLE offenen Yes/No-Märkte von der Polymarket Gamma-API
  2. Filtert auf Yes+No < THRESHOLD
  3. Hängt jeden Treffer als Zeile an data/snapshots.csv an (Rohdaten,
     ein Eintrag pro Markt pro Run)
  4. Aggregiert data/snapshots.csv neu zu data/summary.csv (pro Markt:
     erster/letzter Treffer, geschätzte Dauer, Volumen, Zeit bis
     Terminierung)
  5. Lässt den simulierten Arbitrage-Fonds (fund_simulator.py) auf den
     schon geladenen Marktdaten einen Schritt laufen: offene Positionen
     glattstellen/auszahlen, neue Treffer ggf. kaufen.

Das Commit + Push übernimmt der GitHub-Actions-Workflow, nicht dieses
Script.

WICHTIG:
  - Polymarket notiert in USD/USDC, nicht EUR ("1 Euro" = "1 USD" hier).
  - "Geschätzte Dauer unter Schwelle" ist eine Sample-Näherung
    (Anzahl Treffer-Runs * INTERVAL_MINUTES). Cron-Trigger können sich
    laut GitHub-Doku um ein paar Minuten verzögern, besonders bei hoher
    Last - die Näherung wird dadurch etwas ungenauer, bleibt aber
    brauchbar.
  - Yes+No < 1 ist keine garantierte risikofreie Arbitrage (Spread,
    Slippage, Settlement-Timing).
"""

import csv
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import fund_simulator
import pricing

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_KEYSET_URL = GAMMA_URL + "/keyset"

# ---------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------
THRESHOLD = 1.0
INTERVAL_MINUTES = 180               # muss zum Cron-Takt in der .yml passen (alt: 15; Ist-Takt wird in rebuild_summary gemessen)
TIMEZONE = "Europe/Berlin"
BATCH_SIZE = 100
MIN_VOLUME = 0.0

DATA_DIR = "data"
SNAPSHOTS_CSV = os.path.join(DATA_DIR, "snapshots.csv")
SUMMARY_CSV = os.path.join(DATA_DIR, "summary.csv")
LAST_RUN_STATS_JSON = os.path.join(DATA_DIR, "last_run_stats.json")


def fetch_all_open_markets(batch_size=BATCH_SIZE, min_volume=MIN_VOLUME):
    """Kompatibilitaets-Wrapper: nur die Marktliste (siehe fetch_all_open_markets_checked)."""
    return fetch_all_open_markets_checked(batch_size, min_volume)[0]


def fetch_all_open_markets_checked(batch_size=BATCH_SIZE, min_volume=MIN_VOLUME):
    """Wie oben, liefert aber (markets, complete). complete=False heisst: die Pagination
    ist mittendrin an einem Fehler abgebrochen, die Liste ist unvollstaendig. Das ist fuer
    den Fonds wichtig: fehlende Maerkte duerfen NICHT als 'aufgeloest' gelten."""
    # Die alte Offset-Pagination auf /markets wird von Polymarket nicht mehr
    # unterstützt (liefert ab einem gewissen Offset HTTP 422). Die Gamma-API
    # verlangt inzwischen Keyset-Pagination über /markets/keyset mit
    # after_cursor statt offset - "active" lässt sich dort nicht serverseitig
    # filtern, daher wird das Feld unten pro Markt geprüft.
    markets = []
    cursor = None
    complete = True
    while True:
        params = {"closed": "false", "limit": batch_size}
        if cursor:
            params["after_cursor"] = cursor
        try:
            resp = requests.get(GAMMA_KEYSET_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[Warnung] Fehler bei Cursor {cursor!r}: {e}")
            complete = False
            break
        batch = data.get("markets", [])
        for m in batch:
            if not m.get("active", True):
                continue
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            if vol >= min_volume:
                markets.append(m)
        cursor = data.get("next_cursor")
        if not cursor:
            break
        time.sleep(0.05)
    return markets, complete


def format_timedelta(td):
    if td is None:
        return "-"
    total_seconds = int(td.total_seconds())
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}min")
    return sign + " ".join(parts)


SNAPSHOT_FIELDS = [
    "timestamp", "market_id", "frage", "slug", "yes_preis",
    "no_preis", "summe", "volumen", "liquiditaet", "end_date", "preisbasis",
]


def append_snapshot_rows(rows):
    """Haengt Zeilen an; aeltere Dateien ohne Spalte `preisbasis` werden auf den neuen Kopf migriert
    (leer = Altbestand = Schaetzung, siehe pricing.basis_of)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(SNAPSHOTS_CSV)
    if file_exists:
        with open(SNAPSHOTS_CSV, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if header is not None and header != SNAPSHOT_FIELDS:
            with open(SNAPSHOTS_CSV, newline="", encoding="utf-8") as f:
                old = list(csv.DictReader(f))
            with open(SNAPSHOTS_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
                w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k, "") for k in SNAPSHOT_FIELDS})
    with open(SNAPSHOTS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def rebuild_summary(tz):
    if not os.path.isfile(SNAPSHOTS_CSV):
        return
    df = pd.read_csv(SNAPSHOTS_CSV)
    if df.empty:
        return
    # utc=True: nach der Zeitumstellung (Europe/Berlin, 25.10.) stehen +02:00 und +01:00 in
    # derselben Spalte; ohne einheitliche Zone liefert pandas eine object-Spalte (strftime scheitert).
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").dt.tz_convert(tz)
    # Tatsaechlicher Abstand der Laeufe statt festem Cron-Takt: GitHub fuehrt Schedules real nur
    # etwa alle 4-5 h aus, nicht alle INTERVAL_MINUTES.
    run_times = df["timestamp"].drop_duplicates().sort_values()
    gaps = run_times.diff().dropna().dt.total_seconds() / 60.0
    interval_minutes = float(gaps.median()) if len(gaps) else float(INTERVAL_MINUTES)

    now = datetime.now(tz)
    out_rows = []
    for market_id, g in df.groupby("market_id"):
        first_t = g["timestamp"].min()
        last_t = g["timestamp"].max()
        n_samples = len(g)
        dauer_minuten = n_samples * interval_minutes

        end_date_raw = g["end_date"].iloc[-1]
        end_dt = None
        if isinstance(end_date_raw, str) and end_date_raw:
            try:
                end_dt = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                end_dt = None
        time_to_end = (end_dt - now) if end_dt else None

        out_rows.append({
            "Frage": g["frage"].iloc[-1],
            "Slug": g["slug"].iloc[-1],
            "Erster Treffer": first_t.strftime("%Y-%m-%d %H:%M"),
            "Letzter Treffer": last_t.strftime("%Y-%m-%d %H:%M"),
            "Geschaetzte Dauer unter Schwelle": format_timedelta(timedelta(minutes=dauer_minuten)),
            "Min. Summe beobachtet": round(g["summe"].min(), 3),
            "Zeit bis Terminierung": format_timedelta(time_to_end),
            "Max. Volumen (USD)": round(g["volumen"].max(), 2),
            "Max. Liquiditaet (USD)": round(g["liquiditaet"].max(), 2),
        })

    summary_df = pd.DataFrame(out_rows).sort_values(
        "Geschaetzte Dauer unter Schwelle", ascending=False
    )
    summary_df.to_csv(SUMMARY_CSV, index=False)
    print(f"Summary aktualisiert: {len(out_rows)} Märkte insgesamt erfasst.")


def build_hits(markets, now):
    """Treffer dieses Laufs (Yes+No < THRESHOLD auf Kaufpreisen) - genau die Zeilen, die in snapshots.csv landen und
    die der Fonds-Schritt als Kaufkandidaten bekommt."""
    rows = []
    for m in markets:
        parsed = pricing.buy_quotes(m)  # Orderbuch-Ask wenn vorhanden, sonst Schaetzung (gekennzeichnet)
        if parsed is None:
            continue
        yes_p, no_p = parsed.yes, parsed.no
        total = yes_p + no_p
        if yes_p <= 0.0 or no_p <= 0.0:
            continue  # eine Seite ohne Kurs ("kein Angebot") ist keine kaufbare Yes+No-Kombination
        if total < THRESHOLD:
            rows.append({
                "timestamp": now.isoformat(),
                "market_id": m.get("id") or m.get("slug"),
                "frage": m.get("question", "?"),
                "slug": m.get("slug", "?"),
                "yes_preis": yes_p,
                "no_preis": no_p,
                "summe": total,
                "volumen": float(m.get("volumeNum") or m.get("volume") or 0),
                "liquiditaet": float(m.get("liquidityNum") or m.get("liquidity") or 0),
                "end_date": m.get("endDate") or "",
                "preisbasis": parsed.basis,
            })
    return rows


def main():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    print(f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')}: lade Märkte...")
    markets, fetch_complete = fetch_all_open_markets_checked()

    rows = build_hits(markets, now)

    print(f"{len(markets)} Märkte geprüft, {len(rows)} Treffer unter Schwelle {THRESHOLD}.")

    # Für das Dashboard: Kennzahlen dieses Laufs maschinenlesbar ablegen, statt
    # sie aus den Actions-Logs abschreiben zu müssen.
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LAST_RUN_STATS_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": now.isoformat(),
            "markets_checked": len(markets),
            "hits_this_run": len(rows),
        }, f, ensure_ascii=False, indent=2)

    if rows:
        append_snapshot_rows(rows)
        rebuild_summary(tz)
    else:
        print("Keine Treffer in diesem Run.")

    # Fonds-Simulation läuft unabhängig davon, ob dieser Run neue Treffer
    # gebracht hat - offene Positionen müssen auch sonst auf Auflösung/
    # Konvergenz geprüft werden. Nutzt die schon geladenen Marktdaten weiter.
    if not fetch_complete:
        # Teil-Abruf: fehlende Maerkte wuerden als "aufgeloest" ausgezahlt (1,00 USD/Paar).
        # Positionen bleiben unveraendert; der Watcher bzw. der naechste volle Lauf prueft sie.
        print("[Warnung] Marktabruf unvollstaendig - Fonds-Schritt in diesem Lauf uebersprungen.")
        return
    fund_simulator.run_all_fund_steps(markets, rows, now)


if __name__ == "__main__":
    main()
