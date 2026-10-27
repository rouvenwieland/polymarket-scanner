"""
Polymarket-Connector für den Multi-Platform-Scan.

Wiederverwendet dieselbe Gamma-API-Keyset-Pagination wie
polymarket_snapshot.py, liefert aber im normalisierten Schema
(siehe scripts/platforms/__init__.py), statt der rohen Gamma-API-dicts.

Gebühren: Taker-Gebühr je Markt (feesEnabled/feeSchedule), siehe fee_rate und
taker_fee unten. Gas-Kosten auf Polygon gelten als vernachlässigbar.
"""

import json
import math
import time
from datetime import datetime, timezone

import requests

import pricing

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_KEYSET_URL = GAMMA_URL + "/keyset"
BATCH_SIZE = 100


def fetch_all_open_markets(batch_size=BATCH_SIZE, min_volume=0.0):
    markets = []
    cursor = None
    while True:
        params = {"closed": "false", "limit": batch_size}
        if cursor:
            params["after_cursor"] = cursor
        try:
            resp = requests.get(GAMMA_KEYSET_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[Warnung] Polymarket-Fehler bei Cursor {cursor!r}: {e}")
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
    return markets


def normalize(market):
    # Orderbuch-Asks (bestAsk / 1-bestBid) wenn vorhanden und plausibel, sonst outcomePrices als
    # gekennzeichnete SCHAETZUNG (siehe pricing.py).
    quotes = pricing.buy_quotes(market)
    if quotes is None:
        return None
    yes_price, no_price = quotes.yes, quotes.no
    yes_label, no_label = pricing.outcome_labels(market)
    token_ids = pricing._json_list(market.get("clobTokenIds")) or []

    return {
        "platform": "polymarket",
        "price_basis": quotes.basis,
        "market_id": market.get("id") or market.get("slug"),
        "question": market.get("question", "?"),
        # Was die "yes"-Seite (erster Token) bzw. "no"-Seite bedeutet: None = Ja/Nein auf die Frage, sonst der
        # Ausgangsname (Sport: Teamname). Grundlage der Seitenzuordnung in cross_platform.outcome_alignment.
        "yes_label": yes_label,
        "no_label": no_label,
        "token_ids": [str(t) for t in token_ids][:2],
        "is_sport": bool(market.get("sportsMarketType") or market.get("gameStartTime")),
        "sport_market_type": market.get("sportsMarketType") or "",
        "start_time": _iso_utc(market.get("gameStartTime")),
        "fee_rate": fee_rate(market),
        "slug": market.get("slug") or "",
        # Zusätzlicher Beschreibungstext, nur fürs Cross-Platform-Matching
        # genutzt (siehe cross_platform.py) - hilft, Märkte zu matchen, deren
        # Titel unterschiedlich formuliert ist, deren Auflösungskriterien im
        # Fließtext aber erkennbar dasselbe Ereignis beschreiben.
        "extra_text": market.get("description") or "",
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": float(market.get("volumeNum") or market.get("volume") or 0),
        "liquidity": float(market.get("liquidityNum") or market.get("liquidity") or 0),
        "end_date": market.get("endDate") or None,
        "url": f"https://polymarket.com/event/{market.get('slug','')}",
    }


def fetch_all_normalized(markets=None):
    """Nimmt optional schon geladene rohe Gamma-Märkte entgegen (damit der
    große Scan nicht zweimal laden muss), sonst wird selbst geladen."""
    if markets is None:
        markets = fetch_all_open_markets()
    out = []
    for m in markets:
        n = normalize(m)
        if n:
            out.append(n)
    return out


def _iso_utc(value):
    """gameStartTime kommt als "2026-10-17 21:00:00+00" - in ISO 8601 (UTC) umwandeln, sonst None."""
    if not value:
        return None
    s = str(value).strip().replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# Polymarket erhebt inzwischen Taker-Gebuehren (docs.polymarket.com/trading/fees, gelesen 27.10.2026):
#   fee = C x feeRate x p x (1 - p), Makers zahlen nie. Satz je Kategorie 0,04-0,07, Geopolitik 0.
# Jeder Gamma-Markt traegt "feesEnabled" und "feeSchedule" ({"rate": 0.03, "exponent": 1, "takerOnly": true}).
# Frueher rechnete der Code Polymarket gebuehrenfrei (Lamar-Markt: feesEnabled=true, rate 0,03).
UNKNOWN_FEE_RATE = 0.07  # Feld fehlt: hoechster veroeffentlichter Satz (Krypto), also vorsichtig
# Die Doku nennt fuer Sport 0,05, der Lamar-Markt traegt im feeSchedule 0,03. Bis das geklaert ist, gilt bei einem
# positiven Satz der hoehere Wert (mindestens FEE_RATE_FLOOR). Ein ausdruecklicher Satz 0 (z.B. Wahl-Maerkte im
# Oktober 2026: feesEnabled true, rate 0) bleibt 0 - der Markt erklaert sich selbst gebuehrenfrei.
FEE_RATE_FLOOR = 0.05


def fee_rate(market):
    """Taker-Gebuehrensatz eines Gamma-Markts: 0, wenn feesEnabled ausdruecklich false; sonst feeSchedule.rate;
    fehlen die Angaben, UNKNOWN_FEE_RATE (vorsichtig)."""
    enabled = market.get("feesEnabled")
    if enabled is False:
        return 0.0
    schedule = market.get("feeSchedule")
    if isinstance(schedule, dict):
        try:
            rate = float(schedule.get("rate"))
        except (TypeError, ValueError):
            rate = None
        if rate is not None and 0.0 <= rate <= 1.0:
            return max(rate, FEE_RATE_FLOOR) if rate > 0 else 0.0
    return UNKNOWN_FEE_RATE


def taker_fee(shares, price, rate=UNKNOWN_FEE_RATE):
    """Taker-Gebuehr in USD fuer `shares` Anteile zum Preis `price`: shares x rate x p x (1 - p).
    Der Exponent des feeSchedule (1 bei Sport) wird nicht hoeher als 1 angesetzt: p(1-p) <= 0,25, ein
    groesserer Exponent ergaebe eine KLEINERE Gebuehr - Exponent 1 ist also die vorsichtige Obergrenze.
    Polymarket rundet auf 5 Nachkommastellen; hier wird aufgerundet."""
    if shares <= 0 or rate <= 0:
        return 0.0
    raw = shares * rate * price * (1.0 - price)
    return math.ceil(raw * 1e5 - 1e-9) / 1e5


CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def fetch_ask_levels(token_id):
    """Ask-Seite des CLOB-Orderbuchs eines Tokens als [(preis, anteile), ...] aufsteigend, oder None.
    Wird nur fuer die wenigen Kandidaten abgefragt, die alle anderen Pruefungen bestanden haben."""
    if not token_id:
        return None
    try:
        resp = requests.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[Warnung] Polymarket-Orderbuch {token_id[:12]}...: {e}")
        return None
    levels = []
    for lvl in data.get("asks") or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < p < 1.0 and s > 0 and math.isfinite(p) and math.isfinite(s):
            levels.append((p, s))
    levels.sort()
    return levels or None
