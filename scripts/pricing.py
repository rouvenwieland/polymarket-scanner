"""
Preisquellen-Abstraktion: welche Zahl ist AUSFUEHRBAR, welche nur eine Schaetzung?
=================================================================================

Hintergrund (Pruefung 14.10.2026): Die Polymarkt-Fonds rechneten bisher mit `outcomePrices` der
Gamma-API. Das sind Mittel-/Letztkurse bzw. bei neuen Maerkten der Platzhalter 0,5/0,5 - kein Preis,
zu dem man kaufen oder verkaufen koennte. Yes+No < 1 auf Mittelkursen ist typischerweise ein
Daten-Artefakt (stale Letztkurs, leere Orderbuecher), keine Arbitrage.

Regeln:
  - KAUFEN : Orderbuch-ASK (Preis, den man zahlen muss).
  - VERKAUFEN : Orderbuch-BID (Preis, den man bekommt).
  - Polymarket (binaer, gespiegeltes Orderbuch): Gamma liefert bestBid/bestAsk fuer den Yes-Token.
      Yes kaufen = bestAsk,  No kaufen = 1 - bestBid,
      Yes verkaufen = bestBid, No verkaufen = 1 - bestAsk.
    (Annahme: Feldnamen und Spiegelung gemaess Gamma-/CLOB-Doku; ohne Netzzugriff nicht live
    verifiziert. Fehlen die Felder oder sind sie unplausibel, wird auf Schaetzung zurueckgefallen.)
  - Kalshi, PredictIt, SX Bet liefern ohnehin Ask-Preise (Kauf) -> ausfuehrbar.
  - Alles andere (outcomePrices) bleibt als SCHAETZUNG gekennzeichnet: `BASIS_ESTIMATE`.

Jede Zahl, die in Fonds-Logs/Dashboard auftaucht, traegt die Kennzeichnung `preisbasis`.
"""

import json
import math
from collections import namedtuple

BASIS_EXECUTABLE = "ausfuehrbar"
BASIS_ESTIMATE = "schaetzung"

LABELS = {
    BASIS_EXECUTABLE: "ausführbar (Orderbuch-Ask/-Bid)",
    BASIS_ESTIMATE: "Schätzung, nicht ausführbar (Mittel-/Letztkurs)",
}

# True = auf reiner Schaetzbasis wird NICHT gehandelt: kein Kauf und kein vorzeitiger Verkauf; die Polymarkt-Fonds
# bewerten offene Positionen ohne Orderbuch nicht neu (Cross-Beine werden weiter aus outcomePrices markiert, gekennzeichnet).
# Seit der Bereinigung vom 14.10.2026 Standard: sie hat alle Schaetzkurs-Kaeufe entfernt (Regel R1); mit False haette
# schon der naechste Workflow-Lauf genau diese Trades wieder erzeugt (tests/test_workflow_invarianten.py).
# Im einzelnen Polymarkt-Markt ist Yes+No < 1 auf ausfuehrbaren Preisen ohnehin unmoeglich (Lean book_buy_sum_ge_one).
REQUIRE_EXECUTABLE = True

Quotes = namedtuple("Quotes", "yes no basis")


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value if value is not None else "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    return out if isinstance(out, list) else None


def _outcome_names(market):
    if market.get("outcomes") in (None, ""):
        return ["yes", "no"]  # Feld fehlt (Altdaten/Tests): Gamma-Standard
    outcomes = _json_list(market.get("outcomes"))
    if outcomes is None or len(outcomes) != 2:
        return None
    return [str(o).strip().lower() for o in outcomes]


def outcome_indices(market):
    """(Index der "yes"-Seite, Index der "no"-Seite) in outcomes/outcomePrices, oder None (kein Zwei-Ausgangs-Markt).

    Ja/Nein-Maerkte: nach Namen (auch in umgekehrter Reihenfolge ["No", "Yes"]). Alle anderen Maerkte (Sport:
    ["Lamar", "Southeastern Louisiana"], Ueber/Unter ...): "yes" = erster Ausgang (Index 0), "no" = zweiter.
    Frueher verlangte parse_outcome_prices die Namen "Yes"/"No"; Sportmaerkte wurden dadurch nie abgerechnet
    (Fall Lamar vs. Southeastern Louisiana, 17.10.2026: Position hing zehn Tage)."""
    names = _outcome_names(market)
    if names is None:
        return None
    if sorted(names) == ["no", "yes"]:
        return names.index("yes"), names.index("no")
    return 0, 1


def book_matches_yes_side(market):
    """Gamma liefert bestBid/bestAsk fuer den ERSTEN Token. Nur wenn dieser die "yes"-Seite ist, passen die
    Orderbuchpreise zur Seite. Bei ["No", "Yes"] waere das vertauscht - dann wird das Buch nicht genutzt
    (Rueckfall auf die gekennzeichnete Schaetzung, die nie gehandelt wird)."""
    idx = outcome_indices(market)
    return idx is not None and idx[0] == 0


def outcome_labels(market):
    """(yes_label, no_label) eines Gamma-Markts. (None, None) bei Ja/Nein-Ausgaengen (die Frage selbst
    beschreibt dann die Ja-Seite), sonst die Ausgangsnamen (z.B. Teamnamen)."""
    outcomes = _json_list(market.get("outcomes", "[]"))
    if outcomes is None or len(outcomes) != 2:
        return None, None
    a, b = (str(o).strip() for o in outcomes)
    if sorted([a.lower(), b.lower()]) == ["no", "yes"]:
        return None, None
    return a, b


def parse_outcome_prices(market):
    """outcomePrices der Gamma-API (Schaetzung bzw. Endkurse). Validiert: endlich, 0..1, Summe > 0.
    -> (yes, no) mit yes = Kurs des ersten Tokens (siehe outcome_indices), oder None."""
    prices = _json_list(market.get("outcomePrices", "[]"))
    idx = outcome_indices(market)
    if prices is None or idx is None or len(prices) != 2:
        return None
    yes_p, no_p = _num(prices[idx[0]]), _num(prices[idx[1]])
    if yes_p is None or no_p is None:
        return None
    if not (0.0 <= yes_p <= 1.0 and 0.0 <= no_p <= 1.0) or yes_p + no_p <= 0.0:
        return None
    return yes_p, no_p


def _book(market):
    """(bestBid, bestAsk) des Yes-Tokens, nur wenn plausibel (0 < bid <= ask < 1), sonst None."""
    bid, ask = _num(market.get("bestBid")), _num(market.get("bestAsk"))
    if bid is None or ask is None:
        return None
    if not (0.0 < bid <= ask < 1.0):
        return None
    return bid, ask


def buy_quotes(market):
    """Kaufpreise (yes, no) + Basis. Bevorzugt Orderbuch-Asks, sonst outcomePrices-Schaetzung, sonst None."""
    if outcome_indices(market) is None:
        return None  # kein Zwei-Ausgangs-Markt
    book = _book(market) if book_matches_yes_side(market) else None
    if book is not None:
        bid, ask = book
        return Quotes(ask, 1.0 - bid, BASIS_EXECUTABLE)
    est = parse_outcome_prices(market)
    if est is None:
        return None
    return Quotes(est[0], est[1], BASIS_ESTIMATE)


def sell_quotes(market):
    """Verkaufspreise (yes, no) + Basis. Bevorzugt Orderbuch-Bids, sonst outcomePrices-Schaetzung."""
    if outcome_indices(market) is None:
        return None
    book = _book(market) if book_matches_yes_side(market) else None
    if book is not None:
        bid, ask = book
        return Quotes(bid, 1.0 - ask, BASIS_EXECUTABLE)
    est = parse_outcome_prices(market)
    if est is None:
        return None
    return Quotes(est[0], est[1], BASIS_ESTIMATE)


def combine_basis(*bases):
    """Ein Paar/Portfolio ist nur ausfuehrbar, wenn ALLE Beine ausfuehrbar sind."""
    return BASIS_EXECUTABLE if bases and all(b == BASIS_EXECUTABLE for b in bases) else BASIS_ESTIMATE


def basis_of(item, default=BASIS_ESTIMATE):
    """Basis eines dicts (Position, Handelszeile). Altbestand ohne Feld gilt als Schaetzung."""
    b = (item or {}).get("preisbasis")
    return b if b in LABELS else default


def estimate_split(positions, value_fn):
    """(Anzahl, Wert) der offenen Positionen auf reiner Schaetzbasis - fuer History-Spalten/Dashboard."""
    est = [p for p in positions if basis_of(p) != BASIS_EXECUTABLE]
    return len(est), sum(value_fn(p) for p in est)
