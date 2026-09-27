"""
Polymarket-Arbitrage-Fonds (Simulation)
========================================

Simuliert einen kleinen Fonds, der die Strategie des Scanners tatsächlich
"fährt": bei jedem Snapshot-Lauf werden Yes+No < 1-Treffer gekauft (beide
Seiten gleichzeitig) und entweder bis zur Marktauflösung gehalten (Auszahlung
1.00 USD je Paar) oder vorher wieder verkauft, sobald Yes+No wieder auf ~1.00
gestiegen ist. Es wird NICHTS echtes gehandelt - das hier ist Papier-Trading.

Zwei Einstiegspunkte teilen sich dieselbe Positions-Logik (_evaluate_position):
  - run_fund_step()      wird vom großen Scan (polymarket_snapshot.py) aufgerufen.
    Liefert neue Kaufkandidaten UND nutzt die dabei ohnehin schon geladenen
    ~200.000 Marktdaten, um alle offenen Positionen mit zu bewerten. Das ist
    das "Portfolio", aus dem neue Positionen ausgewählt werden.
  - watch_positions_step() wird vom hochfrequenten fund_watch.py aufgerufen
    (siehe .github/workflows/fund_watch.yml). Kauft NICHTS Neues, sondern holt
    für jede offene Position gezielt nur ihren einen Markt per Einzel-Lookup
    (GET /markets/{id}) und prüft Konvergenz/Auflösung - viel schneller als
    ein voller Scan, damit gehaltene Positionen nicht erst beim nächsten
    15-Minuten-Lauf bemerkt werden.

Vereinfachungen, die bewusst in Kauf genommen werden:
  - Slippage/Market Impact ist ein einfaches lineares Modell
    (siehe IMPACT_COEFFICIENT), keine echte Orderbuch-Tiefe.
  - Auszahlung bei Marktauflösung wird als sauberes 1.00 USD/Paar simuliert
    (keine Redemption-Gebühren, kein Gegenparteirisiko).
  - Die Liquiditätsangabe der Gamma-API (liquidityNum) ist eine grobe Proxy-
    Größe für "wie viel könnte man hier realistisch handeln", keine
    tatsächliche Orderbuchtiefe.
  - Keine Trading-Fees/Gas modelliert (Polymarket erhebt aktuell auch keine
    klassischen Taker-Fees auf den meisten Märkten).

Persistenter Zustand liegt in DATA_DIR:
  - fund_state.json   aktuelle Kasse + offene Positionen (Source of Truth)
  - fund_trades.csv   Handelsjournal (jeder Kauf/Verkauf/jede Auszahlung)
  - fund_history.csv  NAV-Zeitreihe (ein Eintrag pro Lauf, auch ohne Trades)
"""

import csv
import json
import os
from datetime import datetime

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
TIMEZONE = "Europe/Berlin"

DATA_DIR = "data"
STATE_JSON = os.path.join(DATA_DIR, "fund_state.json")
TRADES_CSV = os.path.join(DATA_DIR, "fund_trades.csv")
HISTORY_CSV = os.path.join(DATA_DIR, "fund_history.csv")

# ---------------------------------------------------------------------
# Strategie-Konfiguration
# ---------------------------------------------------------------------
STARTING_CAPITAL = 100.0
MIN_LIQUIDITY = 15.0          # USD, darunter gilt ein Markt als nicht handelbar
MIN_SPREAD = 0.01             # nur Treffer mit >= 1% Abstand von 1.00 sind einen Trade wert
SELL_CONVERGENCE = 0.997      # bei Yes+No >= diesem Wert vorzeitig glattstellen
MAX_POSITION_FRACTION_OF_LIQUIDITY = 0.10   # max. Positionsgröße relativ zur Liquidität
MAX_OPEN_POSITIONS = 12       # Diversifikations-Deckel bei 100 USD Kapital
IMPACT_COEFFICIENT = 0.15     # linearer Preiseinfluss: effektiver Preis = mid * (1 ± coef * notional/liquidity)
MIN_TRADE_NOTIONAL = 2.0      # Trades unter diesem Betrag lohnen sich nicht (Rundungsrauschen)
MAX_EFFECTIVE_BUY_PRICE = 0.999   # Sicherheitsnetz: Kauf nur, wenn nach Market-Impact noch ein Edge bleibt


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


def fetch_market_by_id(market_id):
    """Einzel-Lookup für den schnellen Watcher - viel billiger als eine volle
    Keyset-Pagination über alle Märkte. Gibt None zurück, wenn der Markt nicht
    (mehr) auffindbar ist (dann wird die Position als aufgelöst behandelt)."""
    for url in (f"{GAMMA_URL}/{market_id}", f"{GAMMA_URL}/slug/{market_id}"):
        try:
            resp = requests.get(url, timeout=15)
        except requests.RequestException as e:
            print(f"[Warnung] Fehler beim Einzel-Lookup {market_id}: {e}")
            return None
        if resp.status_code == 404:
            continue
        try:
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict) and data:
            return data
    return None


def _empty_state():
    return {"cash": STARTING_CAPITAL, "positions": [], "started": None}


def load_state():
    if not os.path.isfile(STATE_JSON):
        return _empty_state()
    with open(STATE_JSON, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("cash", STARTING_CAPITAL)
    state.setdefault("positions", [])
    state.setdefault("started", None)
    return state


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _append_csv(path, fieldnames, row):
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def log_trade(timestamp, action, market_id, frage, shares, price, amount, cash_after, note=""):
    _append_csv(TRADES_CSV, [
        "timestamp", "aktion", "market_id", "frage", "anteile",
        "preis", "betrag", "kasse_danach", "notiz",
    ], {
        "timestamp": timestamp,
        "aktion": action,
        "market_id": market_id,
        "frage": frage,
        "anteile": round(shares, 4),
        "preis": round(price, 4),
        "betrag": round(amount, 4),
        "kasse_danach": round(cash_after, 4),
        "notiz": note,
    })


def log_history(timestamp, cash, positions_value, n_open):
    nav = cash + positions_value
    _append_csv(HISTORY_CSV, [
        "timestamp", "kasse", "positionswert", "nav", "rendite_pct", "offene_positionen",
    ], {
        "timestamp": timestamp,
        "kasse": round(cash, 4),
        "positionswert": round(positions_value, 4),
        "nav": round(nav, 4),
        "rendite_pct": round((nav / STARTING_CAPITAL - 1) * 100, 3),
        "offene_positionen": n_open,
    })
    return nav


def _days_to_end(end_date_str, now):
    if not end_date_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).astimezone(now.tzinfo)
    except ValueError:
        return None
    return (end_dt - now).total_seconds() / 86400.0


def _score(spread, liquidity, days_to_end):
    # Annualisierungs-ähnliche Kennzahl: Ertrag pro gebundenem Tag, damit
    # kurze Laufzeiten bei ähnlichem Spread bevorzugt werden (Kapital wird
    # schneller wieder frei). sqrt(Liquidität) als sanfter Tiebreaker
    # Richtung "besser handelbar", ohne die Kernkennzahl zu dominieren.
    horizon = max(days_to_end if days_to_end else 0.25, 0.25)
    liquidity_weight = min(1.0, liquidity / 100.0) ** 0.5
    return (spread / horizon) * (0.5 + 0.5 * liquidity_weight)


def _evaluate_position(pos, market):
    """Gemeinsame Entscheidungslogik für eine offene Position, egal ob der
    Markt aus einem vollen Scan (markets_by_id-Lookup) oder einem gezielten
    Einzel-Lookup (fetch_market_by_id) stammt.

    Gibt ein dict zurück:
      {"action": "settle"} .......... Markt aufgelöst -> 1.00 USD/Paar
      {"action": "sell", "effective_sum": x} .. Konvergenz -> jetzt glattstellen
      {"action": "hold", "current_sum": x oder None} .. weiter halten
    """
    if market is None:
        return {"action": "settle", "note": "Markt nicht mehr auffindbar -> als aufgelöst behandelt"}
    if market.get("closed"):
        return {"action": "settle", "note": "Markt als geschlossen/aufgelöst markiert"}

    prices = parse_prices(market)
    if prices is None:
        return {"action": "hold", "current_sum": None}

    yes_p, no_p = prices
    current_sum = yes_p + no_p
    if current_sum >= SELL_CONVERGENCE:
        liquidity_now = float(market.get("liquidityNum") or market.get("liquidity") or pos["liquiditaet"])
        notional = pos["shares"] * current_sum
        impact = IMPACT_COEFFICIENT * (notional / max(liquidity_now, 1.0))
        effective_sum = current_sum * (1 - min(impact, 0.3))
        return {"action": "sell", "effective_sum": effective_sum, "current_sum": current_sum}

    return {"action": "hold", "current_sum": current_sum}


def _apply_decision(state, pos, decision, now):
    """Führt eine _evaluate_position()-Entscheidung aus (Kasse/Log/Positionsliste
    aktualisieren). Gibt True zurück, wenn die Position geschlossen wurde."""
    if decision["action"] == "settle":
        proceeds = pos["shares"] * 1.0
        state["cash"] += proceeds
        log_trade(now.isoformat(), "AUSZAHLUNG", pos["market_id"], pos["frage"],
                  pos["shares"], 1.0, proceeds, state["cash"], decision["note"])
        return True

    if decision["action"] == "sell":
        proceeds = pos["shares"] * decision["effective_sum"]
        state["cash"] += proceeds
        log_trade(now.isoformat(), "VERKAUF", pos["market_id"], pos["frage"],
                  pos["shares"], decision["effective_sum"], proceeds, state["cash"],
                  f"Konvergenz erreicht (Kurs {decision['current_sum']:.3f})")
        return True

    # hold
    if decision.get("current_sum") is not None:
        pos["letzter_kurs"] = decision["current_sum"]
    return False


def _positions_value(positions):
    return sum(p["shares"] * p.get("letzter_kurs", p["einstandskurs"]) for p in positions)


def _mark_positions_from_markets(state, markets_by_id, now):
    """Voller-Scan-Variante: Marktdaten liegen schon als dict vor."""
    still_open = []
    for pos in state["positions"]:
        market = markets_by_id.get(pos["market_id"])
        decision = _evaluate_position(pos, market)
        closed = _apply_decision(state, pos, decision, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open


def _mark_positions_via_lookup(state, now):
    """Watcher-Variante: jede Position bekommt ihren eigenen Einzel-API-Call."""
    still_open = []
    for pos in state["positions"]:
        market = fetch_market_by_id(pos["market_id"])
        decision = _evaluate_position(pos, market)
        closed = _apply_decision(state, pos, decision, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open


def _select_new_trades(state, hits, now):
    open_ids = {p["market_id"] for p in state["positions"]}
    candidates = []
    for h in hits:
        if h["market_id"] in open_ids:
            continue
        spread = 1.0 - h["summe"]
        if spread < MIN_SPREAD or h["liquiditaet"] < MIN_LIQUIDITY:
            continue
        days = _days_to_end(h["end_date"], now)
        if days is not None and days <= 0:
            continue
        candidates.append((h, spread, days))

    candidates.sort(key=lambda c: -_score(c[1], c[0]["liquiditaet"], c[2]))

    free_slots = MAX_OPEN_POSITIONS - len(state["positions"])
    if free_slots <= 0 or not candidates or state["cash"] < MIN_TRADE_NOTIONAL:
        return

    picks = candidates[:free_slots]
    per_slot_notional = state["cash"] / len(picks)

    for h, spread, days in picks:
        if state["cash"] < MIN_TRADE_NOTIONAL:
            break
        liquidity_cap = MAX_POSITION_FRACTION_OF_LIQUIDITY * h["liquiditaet"]
        notional = min(per_slot_notional, liquidity_cap, state["cash"])
        if notional < MIN_TRADE_NOTIONAL:
            continue

        impact = IMPACT_COEFFICIENT * (notional / max(h["liquiditaet"], 1.0))
        effective_sum = h["summe"] * (1 + min(impact, 0.3))
        if effective_sum >= MAX_EFFECTIVE_BUY_PRICE:
            # Der Market Impact bei dieser Positionsgröße frisst den Edge
            # komplett auf (oder dreht ihn sogar ins Minus) - kein Trade,
            # lieber Kasse halten als garantiert mit Verlust einsteigen.
            continue
        shares = notional / effective_sum

        state["cash"] -= notional
        state["positions"].append({
            "market_id": h["market_id"],
            "frage": h["frage"],
            "slug": h["slug"],
            "shares": shares,
            "einstandskurs": effective_sum,
            "letzter_kurs": effective_sum,
            "liquiditaet": h["liquiditaet"],
            "end_date": h["end_date"],
            "eingestiegen": now.isoformat(),
        })
        log_trade(now.isoformat(), "KAUF", h["market_id"], h["frage"],
                  shares, effective_sum, -notional, state["cash"],
                  f"Spread {spread*100:.1f}%, Liquiditaet {h['liquiditaet']:.0f} USD")


def run_fund_step(markets, hits, now):
    """Wird vom großen Scan aufgerufen: Positionen anhand der schon geladenen
    ~200.000 Marktdaten bewerten/glattstellen, dann neue Treffer aus diesem
    Lauf ('hits') ggf. kaufen. Kein zusätzlicher API-Call."""
    state = load_state()
    if state["started"] is None:
        state["started"] = now.isoformat()

    markets_by_id = {}
    for m in markets:
        key = m.get("id") or m.get("slug")
        markets_by_id[key] = m
        if m.get("slug"):
            markets_by_id.setdefault(m["slug"], m)

    _mark_positions_from_markets(state, markets_by_id, now)
    _select_new_trades(state, hits, now)

    save_state(state)
    nav = log_history(now.isoformat(), state["cash"], _positions_value(state["positions"]), len(state["positions"]))
    print(f"Fonds: NAV {nav:.2f} USD ({(nav/STARTING_CAPITAL-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def watch_positions_step(now):
    """Wird vom hochfrequenten fund_watch.py aufgerufen: kauft nichts Neues,
    prüft nur die schon gehaltenen Positionen per gezieltem Einzel-Lookup."""
    state = load_state()
    if not state["positions"]:
        print("Fonds-Watch: keine offenen Positionen, nichts zu tun.")
        return

    _mark_positions_via_lookup(state, now)
    save_state(state)
    nav = log_history(now.isoformat(), state["cash"], _positions_value(state["positions"]), len(state["positions"]))
    print(f"Fonds-Watch: NAV {nav:.2f} USD ({(nav/STARTING_CAPITAL-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")
