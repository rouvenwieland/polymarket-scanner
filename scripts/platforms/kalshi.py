"""
Kalshi-Connector für den Multi-Platform-Scan.

Öffentliche REST-API, kein Auth für Markt-Lesezugriffe nötig.
API-Referenz: https://docs.kalshi.com/api-reference/market/get-markets

Felder laut API-Doku (Auszug, *_dollars-Varianten sind direkt in USD):
  ticker, title, subtitle, status, close_time,
  yes_bid_dollars, yes_ask_dollars, no_bid_dollars, no_ask_dollars,
  last_price_dollars, volume_fp, open_interest_fp, result

Gebühren (Kalshi Fee Schedule, bestätigt über docs.kalshi.com/kalshi.com):
  Taker: ceil(M * 0.07 * C * P * (1-P)), auf den Cent gerundet
  Maker: ceil(M * 0.0175 * C * P * (1-P)), auf den Cent gerundet
  M = Serien-Multiplikator (idR 1), C = Anzahl Kontrakte, P = Preis in USD.
  Hier wird durchgehend die (höhere, realistischere) Taker-Fee verwendet,
  da der simulierte Fonds Markt-Orders annimmt statt im Orderbuch zu warten.
"""

import math
import time

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
LIMIT = 1000
PAGE_DELAY_SECONDS = 0.15  # etwas Abstand zwischen Seiten, um das Rate-Limit nicht zu reizen
MAX_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 8.0   # Obergrenze pro Wartezeit, auch wenn Retry-After mehr verlangt
FETCH_DEADLINE_SECONDS = 90.0  # Gesamt-Zeitbudget für den kompletten Scan - siehe fetch_all_open_markets


def _get_with_retry(url, params, timeout=20):
    """Kalshi hat den vollen Scan unter Last mit HTTP 429 abgebrochen - ein
    einzelner Fehlschlag brach bisher die GESAMTE Seite ab (0 Märkte für den
    ganzen Lauf). Jetzt: bei 429/5xx kurz mit Backoff erneut versuchen
    (Retry-After respektiert, aber auf MAX_RETRY_WAIT_SECONDS gedeckelt -
    bei anhaltendem Rate-Limiting lieber schnell aufgeben als den gesamten
    Multi-Platform-Scan über das Zeitbudget der GitHub-Action hinaus zu
    verzögern, siehe FETCH_DEADLINE_SECONDS)."""
    delay = 1.0
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"[Warnung] Kalshi-Netzwerkfehler (Versuch {attempt+1}/{MAX_RETRIES+1}): {e}")
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            wait = min(float(resp.headers.get("Retry-After", delay)), MAX_RETRY_WAIT_SECONDS)
            print(f"[Warnung] Kalshi HTTP {resp.status_code} (Versuch {attempt+1}/{MAX_RETRIES+1}), warte {wait:.1f}s...")
            time.sleep(wait)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()


def fetch_all_open_markets(limit=LIMIT):
    markets = []
    cursor = None
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    while True:
        if time.monotonic() > deadline:
            print(f"[Warnung] Kalshi-Zeitbudget ({FETCH_DEADLINE_SECONDS:.0f}s) ausgeschöpft - "
                  f"brich mit {len(markets)} bisher geladenen Märkten ab (anhaltendes Rate-Limiting?).")
            break
        params = {"limit": limit, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get_with_retry(BASE_URL, params)
        except requests.RequestException as e:
            print(f"[Warnung] Kalshi-Fehler bei Cursor {cursor!r}: {e}")
            break
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(PAGE_DELAY_SECONDS)
    return markets


def normalize(market):
    # Die Query gegen status=open filtert serverseitig korrekt, aber das
    # status-FELD in der Antwort selbst nutzt ein anderes Vokabular: ein
    # gerade handelbarer Markt trägt dort "active", nicht "open" - mit dem
    # falschen Wert hier wurde bisher JEDER geladene Markt verworfen (0
    # normalisierte Märkte trotz hunderttausender geladener Rohdaten).
    if market.get("status") != "active":
        return None
    try:
        yes_price = float(market["yes_ask_dollars"])
        no_price = float(market["no_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if yes_price <= 0 or no_price <= 0:
        return None
    volume = float(market.get("volume_fp") or market.get("volume") or 0)
    ticker = market.get("ticker", "")
    return {
        "platform": "kalshi",
        "market_id": ticker,
        "question": market.get("title") or market.get("subtitle") or ticker,
        # Zusätzlicher Text fürs Cross-Platform-Matching (siehe cross_platform.py):
        # subtitle (falls nicht schon die question) + rules_primary, falls
        # die API sie liefert - hilft bei anders formulierten Titeln.
        "extra_text": " ".join(filter(None, [
            market.get("subtitle") if market.get("title") else "",
            market.get("rules_primary") or "",
        ])),
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": volume,
        # Kalshi liefert kein separates Orderbuch-Tiefe-Feld in dieser
        # Übersicht - Volumen wird wie bei Polymarket als grobe Proxy-Größe
        # für die Liquidität verwendet.
        "liquidity": volume,
        "end_date": market.get("close_time"),
        "url": f"https://kalshi.com/markets/{ticker}",
    }


def fetch_all_normalized():
    out = []
    for m in fetch_all_open_markets():
        n = normalize(m)
        if n:
            out.append(n)
    return out


def fetch_market_by_ticker(ticker):
    """Einzel-Lookup für den Watcher, analog zu fund_simulator.fetch_market_by_id."""
    try:
        resp = requests.get(f"{BASE_URL}/{ticker}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[Warnung] Kalshi-Einzel-Lookup {ticker} fehlgeschlagen: {e}")
        return None
    return data.get("market")


def taker_fee(contracts, price_dollars, multiplier=1):
    """ceil(M * 0.07 * C * P * (1-P)), auf den Cent gerundet."""
    raw = multiplier * 0.07 * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(raw * 100) / 100.0
