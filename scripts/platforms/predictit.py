"""
PredictIt-Connector für den Multi-Platform-Scan.

Öffentliche, undokumentierte aber frei zugängliche API (kein Auth nötig):
  GET https://www.predictit.org/api/marketdata/all/

Antwort-Schema (verifiziert per Live-Abruf):
  {
    "markets": [
      {
        "id": 7589, "name": "...", "shortName": "...", "image": "...", "url": "...",
        "contracts": [
          {
            "id": 28361, "dateEnd": null, "image": "...",
            "name": "192 or fewer", "shortName": "192 or fewer", "status": "Open",
            "lastTradePrice": 0.32,
            "bestBuyYesCost": 0.32, "bestBuyNoCost": 0.69,
            "bestSellYesCost": 0.31, "bestSellNoCost": 0.68,
            "lastClosePrice": 0.32, "displayOrder": 0
          },
          ...
        ]
      },
      ...
    ]
  }

Jeder Contract ist faktisch ein eigener Yes/No-Markt (oft als eine von
mehreren Ausprägungen EINER Frage, z.B. Sitzzahl-Bänder) - bestBuyYesCost/
bestBuyNoCost sind die Kaufpreise für Yes bzw. No GENAU dieses Contracts.

Gebühren (PredictIt-Gebührenordnung):
  - 10% Gebühr auf den NETTOGEWINN je Contract, fällig bei Verkauf/Auflösung
    (nicht auf den Umsatz - nur auf tatsächlichen Profit).
  - 5% Gebühr auf Auszahlungen (Konto-Ebene, nicht pro Trade - hier nur
    dokumentiert, nicht in der Pro-Trade-Simulation verrechnet).
"""

import time

import requests

API_URL = "https://www.predictit.org/api/marketdata/all/"

FEE_RATE_PROFIT = 0.10
FEE_RATE_WITHDRAWAL = 0.05

# Mehrere Fonds (die drei Cross-Platform-Kapitalstufen plus der PredictIt-
# Solo-Fonds) können Positionen im SELBEN zugrundeliegenden Markt halten -
# ohne Cache würde jede einen eigenen Einzel-Lookup auf dieselbe outer_id
# machen und PredictIt dadurch unnötig oft anfragen (hat in der Praxis zu
# wiederholten HTTP 429 geführt). Cache lebt nur für die Laufzeit des
# jeweiligen Scan-/Watch-Prozesses (kein TTL nötig).
_contract_cache = {}


def fetch_all_markets():
    try:
        resp = requests.get(API_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[Warnung] PredictIt-Fehler: {e}")
        return []
    return data.get("markets", [])


def normalize_contract(market, contract):
    if contract.get("status") != "Open":
        return None
    try:
        yes_price = float(contract["bestBuyYesCost"])
        no_price = float(contract["bestBuyNoCost"])
    except (KeyError, TypeError, ValueError):
        return None
    if yes_price <= 0 or no_price <= 0:
        return None

    market_name = market.get("name") or market.get("shortName") or ""
    contract_name = contract.get("name") or contract.get("shortName") or ""
    question = f"{market_name} - {contract_name}" if contract_name and contract_name != market_name else market_name

    return {
        "platform": "predictit",
        "market_id": f"{market.get('id')}:{contract.get('id')}",
        "question": question,
        # PredictIts öffentliche API liefert keinen Beschreibungstext -
        # bleibt leer (siehe cross_platform.py, das extra_text optional nutzt).
        "extra_text": "",
        "yes_price": yes_price,
        "no_price": no_price,
        # PredictIts öffentliche API liefert kein Volumen-/Liquiditätsfeld -
        # bewusst konservativ als unbekannt (0.0) behandelt, siehe
        # cross_platform_fund.py für den daraus folgenden Positions-Deckel.
        "volume": 0.0,
        "liquidity": 0.0,
        "end_date": contract.get("dateEnd"),
        "url": market.get("url"),
    }


def fetch_all_normalized():
    out = []
    for market in fetch_all_markets():
        for contract in market.get("contracts", []):
            n = normalize_contract(market, contract)
            if n:
                out.append(n)
    return out


def fetch_contract_by_market_id(market_id):
    """Einzel-Lookup für den Watcher. market_id hat das Format '<marketId>:<contractId>'
    (siehe normalize_contract). Gibt (market, contract) oder (None, None) zurück.
    Innerhalb eines Laufs gecacht pro outer_id (siehe _contract_cache) und mit
    einem kurzen Retry bei HTTP 429, da mehrere Positionen/Fonds denselben
    zugrundeliegenden Markt gleichzeitig abfragen können."""
    try:
        outer_id = market_id.split(":", 1)[0]
        contract_id = int(market_id.split(":", 1)[1])
    except (AttributeError, IndexError, ValueError):
        return None, None

    market = _contract_cache.get(outer_id)
    if market is None:
        delay = 1.0
        for attempt in range(2):
            try:
                resp = requests.get(f"https://www.predictit.org/api/marketdata/markets/{outer_id}", timeout=15)
                if resp.status_code == 429 and attempt == 0:
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                market = resp.json()
                _contract_cache[outer_id] = market
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 1:
                    print(f"[Warnung] PredictIt-Einzel-Lookup {market_id} fehlgeschlagen: {e}")
                    return None, None
                time.sleep(delay)

    if market is None:
        return None, None
    for contract in market.get("contracts", []):
        if contract.get("id") == contract_id:
            return market, contract
    return market, None


def profit_fee(proceeds, cost_basis):
    profit = proceeds - cost_basis
    if profit <= 0:
        return 0.0
    return profit * FEE_RATE_PROFIT
