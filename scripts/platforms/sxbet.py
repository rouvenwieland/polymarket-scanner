"""
SX Bet-Connector für den Multi-Platform-Scan.
=================================================

SX Bet (sx.bet) ist eine dezentrale, on-chain abgewickelte Sportwetten-Börse
(Orderbuch-Modell, USDC) - kein klassischer Prediction-Market-Anbieter wie
Polymarket/Kalshi/PredictIt, aber real-money und mit öffentlicher, auth-
freier Lese-API: genau die Art "Nischenplattform", die den Vergleichsradius
erweitert, ohne dass Nutzer:innen dafür einen eigenen Account/API-Key
brauchen (siehe Nutzer-Entscheidung: möglichst viele vergleichbare
Plattformen einbinden, zusätzliche Laufzeit ist akzeptiert).

API-Referenz (verifiziert per Live-Abruf, https://docs.sx.bet):
  GET /markets/active   - Markt-Metadaten, Pagination über paginationKey
  GET /orderbook-v3/snapshot?marketHash=... - EIN Markt pro Call, alle Preis-
      Level beider Seiten. /orders-v3/odds/best (bis zu 100 Märkte/Call)
      wäre effizienter, verlangt aber laut Doku einen API-Key (401 ohne) -
      für den auth-freien Betrieb bleibt nur der Einzel-Snapshot pro Markt.

Preis-Umrechnung (siehe docs.sx.bet/developers/order-book):
  Ein Orderbuch-Level ist immer aus Sicht des MAKERS: "outcomeOne"-Level
  sind Orders, die auf Outcome 1 WETTEN - wer dagegen (Outcome 2) wetten
  will, handelt GEGEN diese Levels. Taker-Preis = 10^20 - percentageOdds.
  Also: bester Preis, um auf Outcome 1 zu wetten = 1 - max(outcomeTwo-Odds);
  bester Preis für Outcome 2 = 1 - max(outcomeOne-Odds). "size" ist der
  Stake des MAKERS, nicht die für den Taker verfügbare Kapazität - für eine
  grobe Liquiditätsschätzung wird size trotzdem direkt verwendet (gleiche
  Größenordnung, siehe taker_capacity-Formel in der Doku).

Nur echte Zwei-Ausgänge-ohne-Unentschieden-Markttypen werden übernommen
(siehe docs.sx.bet/api-reference/market-types - Spalte "Has lines: false",
Beschreibung nennt explizit "no draw" bzw. ist strukturell binär):
  52  - "12"                     wer gewinnt, zwei Ausgänge OHNE Remis-Ausgang (in der Praxis: Fußball). Ein Remis wird
                                 weder als Sieg der einen noch der anderen Seite gewertet (Void-Name "NO_CONTEST";
                                 die Doku nennt die Abrechnung nicht). Das ist KEIN Gegenstück zu "Will X win?" bei
                                 Polymarkt/Kalshi, wo ein Remis als "Nein" zählt - der Fragetext trägt deshalb den Vermerk
                                 "(ohne Remis-Wertung)" und gilt im Matcher als eigene Wettart (draw_no_bet)
  226 - "12 Including Overtime"  wie 52, inkl. Verlängerung - laut Doku "no draw" (NFL, NBA): ohne Vermerk
  17  - "Both Teams To Score"    echte Yes/No-Frage

Gebühren (docs.sx.bet/developers/fees): die vier Payout-Gebühren
(maker/takerPayoutFee für Einzelwetten, .../ParlayPayoutFee für Parlays)
sind PRO ACCOUNT konfiguriert und nur über einen authentifizierten
/user/fees-v3-Call abrufbar - ohne eigenen Account/API-Key nicht einsehbar.
Laut Doku bedeutet ein ungesetzter Satz "null" explizit "nichts wird
berechnet". Das ist NICHT vorsichtig: SX Bet hat per Governance-Abstimmung
eine Taker-Gebühr auf Gewinne (zuletzt 2 % statt 4 %) eingeführt. Seit der
Reparatur vom 27.10.2026 rechnet die Strategie deshalb mit PROFIT_FEE_RATE
= 2 % auf den Gewinn des gewinnenden Beins (siehe arbitrage_check.py).
"""

PROFIT_FEE_RATE = 0.02

import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.sx.bet"
BINARY_MARKET_TYPES = [52, 226, 17]
PAGE_SIZE = 100
# Begrenzung der insgesamt pro Lauf abgefragten Märkte: /orderbook-v3/snapshot
# erlaubt nur EINEN Markt pro Call (siehe Doku-Hinweis oben) - bei
# potenziell tausenden aktiven Sportmärkten wäre ein Snapshot-Call pro
# Markt sonst nicht mehr mit dem 15-Minuten-Takt der Action vereinbar.
# Es werden die MAX_MARKETS zeitlich nächsten Spiele ausgewählt (am
# ehesten liquide, am relevantesten für zeitnahe Arbitrage).
MAX_MARKETS_PER_RUN = 250
ORDERBOOK_DELAY_SECONDS = 0.05
FETCH_DEADLINE_SECONDS = 90.0
ODDS_SCALE = 10 ** 20


def fetch_active_markets():
    """Lädt Metadaten aller aktiven Märkte der erlaubten Typen (Pagination
    über paginationKey) und gibt die MAX_MARKETS_PER_RUN zeitlich nächsten
    zurück (siehe MAX_MARKETS_PER_RUN)."""
    markets = []
    pagination_key = None
    deadline = time.monotonic() + 30.0  # Metadaten-Pagination allein darf nicht den ganzen Lauf verzögern
    while True:
        if time.monotonic() > deadline:
            print("[Warnung] SX Bet-Metadaten-Zeitbudget ausgeschöpft, brich Pagination ab.")
            break
        params = {
            "type": ",".join(str(t) for t in BINARY_MARKET_TYPES),
            "pageSize": PAGE_SIZE,
        }
        if pagination_key:
            params["paginationKey"] = pagination_key
        try:
            resp = requests.get(f"{BASE_URL}/markets/active", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[Warnung] SX Bet-Metadaten-Fehler: {e}")
            break
        batch = data.get("data", {}).get("markets", [])
        markets.extend(batch)
        pagination_key = data.get("data", {}).get("nextKey")
        if not pagination_key or not batch:
            break
    now_ts = time.time()
    # Nur zukünftige Spiele (vergangene gameTime = vermutlich schon beendet/
    # kurz vor Abwicklung, für neue Positionen uninteressant).
    markets = [m for m in markets if m.get("gameTime") and m["gameTime"] > now_ts]
    markets.sort(key=lambda m: m["gameTime"])
    return markets[:MAX_MARKETS_PER_RUN]


def _fetch_orderbook(market_hash):
    try:
        resp = requests.get(
            f"{BASE_URL}/orderbook-v3/snapshot", params={"marketHash": market_hash}, timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("data")
    except (requests.RequestException, ValueError) as e:
        print(f"[Warnung] SX Bet-Orderbuch-Fehler für {market_hash}: {e}")
        return None


def taker_levels(book):
    """Ausfuehrbare Tiefe fuer den Taker je Seite: {"yes": [(preis, anteile), ...], "no": [...]} aufsteigend.
    Auf Outcome 1 ("yes") wetten heisst, gegen outcomeTwo-Maker-Orders zu handeln: Taker-Preis = 1 - q (q =
    percentageOdds/1e20 des Makers). Der Maker setzt size (USDC, 6 Nachkommastellen) als Anteil q des
    Gesamtpotts ein, der Pott (= Anteile zu je 1 USD Auszahlung) ist also size / q."""
    out = {"yes": [], "no": []}
    if not book:
        return out
    for side, maker_key in (("yes", "outcomeTwo"), ("no", "outcomeOne")):
        for level in book.get(maker_key) or []:
            try:
                q = int(level["percentageOdds"]) / ODDS_SCALE
                size = int(level["size"]) / 1e6
            except (KeyError, TypeError, ValueError):
                continue
            if 0.0 < q < 1.0 and size > 0:
                out[side].append((1.0 - q, size / q))
        out[side].sort()
    return out


def _best_taker_prices(book):
    """Siehe Modul-Docstring zur Preis-Umrechnung. Gibt (yes_price, no_price,
    liquidity_usdc) zurück, oder None, wenn eine Seite kein Orderbuch-Level
    hat (keine Gegenseite zum Handeln vorhanden)."""
    outcome_one = book.get("outcomeOne") or []
    outcome_two = book.get("outcomeTwo") or []
    if not outcome_one or not outcome_two:
        return None
    best_odds_against_one = max(int(level["percentageOdds"]) for level in outcome_two)
    best_odds_against_two = max(int(level["percentageOdds"]) for level in outcome_one)
    yes_price = (ODDS_SCALE - best_odds_against_one) / ODDS_SCALE
    no_price = (ODDS_SCALE - best_odds_against_two) / ODDS_SCALE
    liquidity = (
        sum(int(level["size"]) for level in outcome_one)
        + sum(int(level["size"]) for level in outcome_two)
    ) / 1e6  # USDC hat 6 Dezimalstellen
    return yes_price, no_price, liquidity


_TYPE_LABELS = {52: "Sieger", 226: "Sieger (inkl. Verlängerung)", 17: "Beide Teams treffen"}


def _build_question(market):
    type_id = market.get("type")
    league = market.get("leagueLabel") or market.get("sportLabel") or ""
    if type_id == 17:
        question = f"Both Teams To Score: {market.get('teamOneName')} vs {market.get('teamTwoName')}"
    elif type_id == 52:
        # Remis moeglich (Fussball), aber kein Remis-Ausgang: nicht mit "Will X win?" (Remis = Nein) gleichsetzen
        question = (f"{market.get('outcomeOneName')} vs {market.get('outcomeTwoName')} - "
                    f"wer gewinnt (ohne Remis-Wertung)?")
    else:
        question = f"{market.get('outcomeOneName')} vs {market.get('outcomeTwoName')} - wer gewinnt?"
    extra = " ".join(filter(None, [league, _TYPE_LABELS.get(type_id, "")]))
    return question, extra


def normalize(market, book):
    prices = _best_taker_prices(book)
    if prices is None:
        return None
    yes_price, no_price, liquidity = prices
    if yes_price <= 0 or no_price <= 0:
        return None
    question, extra_text = _build_question(market)
    market_hash = market["marketHash"]
    end_date = None
    if market.get("gameTime"):
        end_date = datetime.fromtimestamp(market["gameTime"], tz=timezone.utc).isoformat()
    type_id = market.get("type")
    two_team = type_id in (52, 226)
    levels = taker_levels(book)
    return {
        "platform": "sxbet",
        "price_basis": "ausfuehrbar",  # beste Taker-Preise aus dem Orderbuch
        "market_id": market_hash,
        "question": question,
        # "yes" = Outcome 1, "no" = Outcome 2. Bei Sieger-Maerkten sind das Teams - und zwar in der Reihenfolge
        # von SX Bet, die NICHT der von Polymarkt entsprechen muss (Lamar vs. Southeastern Louisiana, 17.10.).
        "yes_label": market.get("outcomeOneName") if two_team else None,
        "no_label": market.get("outcomeTwoName") if two_team else None,
        "is_sport": True,
        "sport_market_type": {52: "winner_no_draw", 226: "moneyline", 17: "btts"}.get(type_id, str(type_id)),
        "start_time": end_date,
        "depth_yes": levels["yes"],
        "depth_no": levels["no"],
        "extra_text": extra_text,
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": liquidity,
        "liquidity": liquidity,
        "end_date": end_date,
        "url": f"https://sx.bet/market/{market_hash}",
    }


def fetch_all_normalized():
    out = []
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    for market in fetch_active_markets():
        if time.monotonic() > deadline:
            print(f"[Warnung] SX Bet-Zeitbudget ({FETCH_DEADLINE_SECONDS:.0f}s) ausgeschöpft - "
                  f"brich mit {len(out)} bisher normalisierten Märkten ab.")
            break
        book = _fetch_orderbook(market["marketHash"])
        if book is None:
            continue
        n = normalize(market, book)
        if n:
            out.append(n)
        time.sleep(ORDERBOOK_DELAY_SECONDS)
    return out


def fetch_market_by_hash(market_hash):
    """Orderbuch-Einzel-Lookup (aktueller Preis), siehe fetch_market_meta für
    den Abwicklungsstatus."""
    return _fetch_orderbook(market_hash)


def fetch_market_meta(market_hash):
    """GET /markets/find - liefert bei abgewickelten Märkten zusätzlich
    "outcome" (0=void, 1=Outcome Eins gewinnt, 2=Outcome Zwei gewinnt) und
    "reportedDate". Kein Auth nötig (im Gegensatz zu /orders-v3/odds/best).
    Gibt None bei Netzwerkfehler oder wenn der Hash nicht gefunden wird."""
    try:
        resp = requests.get(f"{BASE_URL}/markets/find", params={"marketHashes": market_hash}, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        print(f"[Warnung] SX Bet-Markt-Lookup-Fehler für {market_hash}: {e}")
        return None
    return data[0] if data else None
