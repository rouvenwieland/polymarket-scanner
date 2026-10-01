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

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
LIMIT = 1000


def fetch_all_open_markets(limit=LIMIT):
    markets = []
    cursor = None
    while True:
        params = {"limit": limit, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(BASE_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[Warnung] Kalshi-Fehler bei Cursor {cursor!r}: {e}")
            break
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return markets


def normalize(market):
    if market.get("status") != "open":
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
