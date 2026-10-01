"""
Polymarket-Connector für den Multi-Platform-Scan.

Wiederverwendet dieselbe Gamma-API-Keyset-Pagination wie
polymarket_snapshot.py, liefert aber im normalisierten Schema
(siehe scripts/platforms/__init__.py), statt der rohen Gamma-API-dicts.

Gebühren: Polymarket erhebt aktuell (Stand dieser Implementierung) keine
Trading-Fee auf Käufe/Verkäufe im Orderbuch - nur Gas-Kosten auf Polygon,
die hier als vernachlässigbar (Bruchteile eines Cents) behandelt werden.
"""

import json
import time

import requests

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
        yes_price, no_price = float(prices[yi]), float(prices[ni])
    except (ValueError, TypeError):
        return None

    return {
        "platform": "polymarket",
        "market_id": market.get("id") or market.get("slug"),
        "question": market.get("question", "?"),
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


def taker_fee(notional, price):
    """Keine Trading-Fee bei Polymarket (Stand dieser Implementierung)."""
    return 0.0
