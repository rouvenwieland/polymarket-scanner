"""
Polymarket Snapshot (für GitHub Actions)
==========================================

Wird alle 10 Minuten von einem Cron-Workflow aufgerufen. Prüft zuerst,
ob die aktuelle Uhrzeit (Europe/Berlin) im konfigurierten Nachtfenster
liegt - falls nicht, beendet sich das Script sofort (kein API-Call,
kein Commit).

Innerhalb des Fensters:
  1. Lädt ALLE offenen Yes/No-Märkte von der Polymarket Gamma-API
  2. Filtert auf Yes+No < THRESHOLD
  3. Hängt jeden Treffer als Zeile an data/snapshots.csv an (Rohdaten,
     ein Eintrag pro Markt pro Run)
  4. Aggregiert data/snapshots.csv neu zu data/summary.csv (pro Markt:
     erster/letzter Treffer, geschätzte Dauer, Volumen, Zeit bis
     Terminierung)

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

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

# ---------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------
THRESHOLD = 1.0
INTERVAL_MINUTES = 10                # muss zum Cron-Takt in der .yml passen
WINDOW_START = (22, 0)               # Nachtfenster Start (Stunde, Minute), lokal
WINDOW_STOP = (8, 30)                # Nachtfenster Ende (Stunde, Minute), lokal
TIMEZONE = "Europe/Berlin"
BATCH_SIZE = 100
MIN_VOLUME = 0.0

DATA_DIR = "data"
SNAPSHOTS_CSV = os.path.join(DATA_DIR, "snapshots.csv")
SUMMARY_CSV = os.path.join(DATA_DIR, "summary.csv")


def in_night_window(now):
    start_h, start_m = WINDOW_START
    stop_h, stop_m = WINDOW_STOP
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    stop = now.replace(hour=stop_h, minute=stop_m, second=0, microsecond=0)
    if start <= stop:
        return start <= now <= stop
    # Fenster geht über Mitternacht (z.B. 22:00 - 08:30)
    return now >= start or now <= stop


def fetch_all_open_markets(batch_size=BATCH_SIZE, min_volume=MIN_VOLUME):
    markets = []
    offset = 0
    while True:
        params = {"closed": "false", "active": "true", "limit": batch_size, "offset": offset}
        try:
            resp = requests.get(GAMMA_URL, params=params, timeout=20)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as e:
            print(f"[Warnung] Fehler bei Offset {offset}: {e}")
            break
        if not batch:
            break
        for m in batch:
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            if vol >= min_volume:
                markets.append(m)
        offset += batch_size
        if len(batch) < batch_size:
            break
        time.sleep(0.1)
    return markets


def parse_prices(market):
    try:
        outcomes = json.loads(market.get("outcomes", "[]"))
        prices = json.loads(market.get("outcomePrices", "[]"))
    except (json.JSONDecodeError, TypeError):
        return None
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    idx_map = {str(o).strip().lower(): i for i, o in enumerate(outcomes)}
    if "yes" not in idx_map or "no" not in idx_map:
        return None
    yi, ni = idx_map["yes"], idx_map["no"]
    try:
        return float(prices[yi]), float(prices[ni])
    except (ValueError, TypeError):
        return None


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


def append_snapshot_rows(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(SNAPSHOTS_CSV)
    with open(SNAPSHOTS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "market_id", "frage", "slug", "yes_preis",
            "no_preis", "summe", "volumen", "liquiditaet", "end_date",
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def rebuild_summary(tz):
    if not os.path.isfile(SNAPSHOTS_CSV):
        return
    df = pd.read_csv(SNAPSHOTS_CSV, parse_dates=["timestamp"])
    if df.empty:
        return

    now = datetime.now(tz)
    out_rows = []
    for market_id, g in df.groupby("market_id"):
        first_t = g["timestamp"].min()
        last_t = g["timestamp"].max()
        n_samples = len(g)
        dauer_minuten = n_samples * INTERVAL_MINUTES

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


def main():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    if not in_night_window(now):
        print(f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')}: außerhalb des Nachtfensters, überspringe.")
        return

    print(f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')}: im Nachtfenster, lade Märkte...")
    markets = fetch_all_open_markets()

    rows = []
    for m in markets:
        parsed = parse_prices(m)
        if parsed is None:
            continue
        yes_p, no_p = parsed
        total = yes_p + no_p
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
            })

    print(f"{len(markets)} Märkte geprüft, {len(rows)} Treffer unter Schwelle {THRESHOLD}.")

    if rows:
        append_snapshot_rows(rows)
        rebuild_summary(tz)
    else:
        print("Keine Treffer in diesem Run.")


if __name__ == "__main__":
    main()
              
