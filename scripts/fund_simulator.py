"""
Polymarket-Arbitrage-Fonds (Simulation) - drei parallele Strategien
=====================================================================

Simuliert DREI unabhängige, gleich gestartete (je 100 USD) Papier-Trading-
Fonds, die dieselbe Grundstrategie fahren - Yes+No < 1 kaufen (beide Seiten),
bis Marktauflösung halten (1.00 USD/Paar) oder vorzeitig bei Konvergenz
verkaufen - sich aber in ihrem Realismus-Grad unterscheiden (siehe PROFILES
unten). Kein Fonds handelt irgendetwas echtes.

  - "conservative"  Mindest-Liquidität, Mindest-Spread, Kaufimpact-Modell,
                     verkauft bei Konvergenz. Das ursprüngliche, realistischste
                     Modell (unveränderte Datendateien für Kontinuität).
  - "aggressive"     Ignoriert die Mindest-Liquidität (kauft auch in sehr
                     dünnen Märkten, soweit die Positionsgröße es zulässt),
                     hält aber im Zweifel bis zur Auflösung statt früh zu
                     verkaufen. Kaufimpact bleibt simuliert.
  - "best_case"      Ignoriert Liquidität UND Marktimpact komplett (kauft/
                     verkauft exakt zum notierten Kurs), nimmt jeden
                     positiven Spread mit, und schichtet aktiv um: ist das
                     Positionslimit voll, wird die schwächste offene Position
                     verkauft, wenn eine deutlich bessere Gelegenheit
                     auftaucht. Eine bewusst unrealistische Obergrenze dafür,
                     was die Strategie im besten Fall hergeben würde.

Zwei Einstiegspunkte teilen sich dieselbe Positions-Logik, jeweils für alle
drei Profile:
  - run_all_fund_steps()    wird vom großen Scan (polymarket_snapshot.py)
    aufgerufen. Nutzt die dabei schon geladenen ~200.000 Marktdaten, um
    offene Positionen alle drei Fonds zu bewerten UND neue Kandidaten
    auszuwählen.
  - watch_all_positions_step() wird vom hochfrequenten fund_watch.py
    aufgerufen (siehe .github/workflows/fund_watch.yml). Kauft NICHTS Neues,
    holt für jede offene Position alle drei Fonds gezielt nur ihren einen
    Markt per Einzel-Lookup. Ein Fonds ohne offene Position verursacht dabei
    keinen API-Call.

Vereinfachungen, die bewusst in Kauf genommen werden (variieren je Profil,
siehe oben):
  - Slippage/Market Impact ist, wenn simuliert, ein einfaches lineares
    Modell (IMPACT_COEFFICIENT), keine echte Orderbuch-Tiefe.
  - Auszahlung bei Marktauflösung wird als sauberes 1.00 USD/Paar simuliert
    (keine Redemption-Gebühren, kein Gegenparteirisiko) - für alle Profile.
  - Die Liquiditätsangabe der Gamma-API (liquidityNum) ist, wenn genutzt,
    eine grobe Proxy-Größe, keine tatsächliche Orderbuchtiefe.
  - Keine Trading-Fees/Gas modelliert.

Persistenter Zustand liegt in DATA_DIR, je Profil unter eigenen Dateinamen
(siehe PROFILES[...]["state_json"/"trades_csv"/"history_csv"]).
"""

import csv
import json
import math
import os
from datetime import datetime

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
TIMEZONE = "Europe/Berlin"

DATA_DIR = "data"

STARTING_CAPITAL = 100.0
MIN_TRADE_NOTIONAL = 2.0          # Trades unter diesem Betrag lohnen sich nicht (Rundungsrauschen)
MAX_EFFECTIVE_BUY_PRICE = 0.999   # universeller Sicherheitsnetz-Wert: nie zu einem Garantie-Verlust kaufen

# ---------------------------------------------------------------------
# Strategie-Profile
# ---------------------------------------------------------------------
PROFILES = {
    "conservative": {
        "key": "conservative",
        "label": "Konservativ",
        "state_json": "fund_state.json",
        "trades_csv": "fund_trades.csv",
        "history_csv": "fund_history.csv",
        "min_liquidity": 15.0,
        "min_spread": 0.01,
        "sell_convergence": 0.997,
        "max_position_fraction": 0.10,   # Anteil der gemeldeten Liquidität, den eine Position max. ausmachen darf
        "impact_coefficient": 0.15,
        "max_open_positions": 12,
        "reallocate": False,
        "reallocate_factor": None,
    },
    "aggressive": {
        "key": "aggressive",
        "label": "Aggressiv",
        "state_json": "fund_state_aggressive.json",
        "trades_csv": "fund_trades_aggressive.csv",
        "history_csv": "fund_history_aggressive.csv",
        "min_liquidity": 0.0,            # keine Mindest-Liquidität - kauft auch in sehr dünnen Märkten
        "min_spread": 0.01,
        "sell_convergence": None,        # verkauft NIE vorzeitig - haelt im Zweifel bis zur Auflösung
        "max_position_fraction": None,   # KEIN künstlicher Liquiditäts-Deckel - die einzige Grenze
                                          # ist, wie groß eine Position sein darf, bevor der simulierte
                                          # Kaufimpact den Edge auffrisst (siehe _max_notional_for_edge).
                                          # So kann auch in sehr dünnen Märkten noch eine kleine,
                                          # gerade noch profitable Position entstehen, statt dass eine
                                          # starre Fraktion den Trade von vornherein verhindert.
        "impact_coefficient": 0.15,      # Kaufimpact bleibt simuliert
        "max_open_positions": 20,
        "reallocate": False,
        "reallocate_factor": None,
    },
    "best_case": {
        "key": "best_case",
        "label": "Best Case (unrealistisch)",
        "state_json": "fund_state_bestcase.json",
        "trades_csv": "fund_trades_bestcase.csv",
        "history_csv": "fund_history_bestcase.csv",
        "min_liquidity": 0.0,            # keine Liquiditätsschranke
        "min_spread": 0.0001,            # nimmt praktisch jeden positiven Spread mit
        "sell_convergence": 0.999,       # nimmt Gewinne zügig mit
        "max_position_fraction": None,   # KEIN Liquiditäts-Deckel auf die Positionsgröße
        "impact_coefficient": 0.0,       # KEIN Marktimpact - handelt exakt zum notierten Kurs
        "max_open_positions": 50,        # praktisch unbegrenzt diversifiziert
        "reallocate": True,              # schichtet aktiv in bessere Gelegenheiten um
        "reallocate_factor": 1.25,       # neue Gelegenheit muss 25% besser bewertet sein als die schwächste Position
    },
}


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


def load_state(profile):
    path = os.path.join(DATA_DIR, profile["state_json"])
    if not os.path.isfile(path):
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("cash", STARTING_CAPITAL)
    state.setdefault("positions", [])
    state.setdefault("started", None)
    return state


def save_state(profile, state):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, profile["state_json"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _append_csv(path, fieldnames, row):
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def log_trade(profile, timestamp, action, market_id, frage, shares, price, amount, cash_after, note=""):
    _append_csv(os.path.join(DATA_DIR, profile["trades_csv"]), [
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


def log_history(profile, timestamp, cash, positions_value, n_open):
    nav = cash + positions_value
    _append_csv(os.path.join(DATA_DIR, profile["history_csv"]), [
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
    liquidity_weight = min(1.0, max(liquidity, 0.0) / 100.0) ** 0.5
    return (spread / horizon) * (0.5 + 0.5 * liquidity_weight)


def _position_score(pos, now):
    letzter = pos.get("letzter_kurs", pos["einstandskurs"])
    spread = max(0.0, 1.0 - letzter)
    days = _days_to_end(pos.get("end_date"), now)
    return _score(spread, pos.get("liquiditaet", 0.0), days)


def _evaluate_position(pos, market, profile):
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

    sell_convergence = profile["sell_convergence"]
    if sell_convergence is not None and current_sum >= sell_convergence:
        liquidity_now = float(market.get("liquidityNum") or market.get("liquidity") or pos["liquiditaet"])
        notional = pos["shares"] * current_sum
        impact = profile["impact_coefficient"] * (notional / max(liquidity_now, 1.0))
        effective_sum = current_sum * (1 - min(impact, 0.3))
        return {"action": "sell", "effective_sum": effective_sum, "current_sum": current_sum}

    return {"action": "hold", "current_sum": current_sum}


def _apply_decision(profile, state, pos, decision, now):
    """Führt eine _evaluate_position()-Entscheidung aus (Kasse/Log/Positionsliste
    aktualisieren). Gibt True zurück, wenn die Position geschlossen wurde."""
    if decision["action"] == "settle":
        proceeds = pos["shares"] * 1.0
        state["cash"] += proceeds
        log_trade(profile, now.isoformat(), "AUSZAHLUNG", pos["market_id"], pos["frage"],
                  pos["shares"], 1.0, proceeds, state["cash"], decision["note"])
        return True

    if decision["action"] == "sell":
        proceeds = pos["shares"] * decision["effective_sum"]
        state["cash"] += proceeds
        log_trade(profile, now.isoformat(), "VERKAUF", pos["market_id"], pos["frage"],
                  pos["shares"], decision["effective_sum"], proceeds, state["cash"],
                  f"Konvergenz erreicht (Kurs {decision['current_sum']:.3f})")
        return True

    # hold
    if decision.get("current_sum") is not None:
        pos["letzter_kurs"] = decision["current_sum"]
    return False


def _positions_value(positions):
    return sum(p["shares"] * p.get("letzter_kurs", p["einstandskurs"]) for p in positions)


def _mark_positions_from_markets(profile, state, markets_by_id, now):
    """Voller-Scan-Variante: Marktdaten liegen schon als dict vor."""
    still_open = []
    for pos in state["positions"]:
        market = markets_by_id.get(pos["market_id"])
        decision = _evaluate_position(pos, market, profile)
        closed = _apply_decision(profile, state, pos, decision, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open


def _mark_positions_via_lookup(profile, state, now):
    """Watcher-Variante: jede Position bekommt ihren eigenen Einzel-API-Call."""
    still_open = []
    for pos in state["positions"]:
        market = fetch_market_by_id(pos["market_id"])
        decision = _evaluate_position(pos, market, profile)
        closed = _apply_decision(profile, state, pos, decision, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open


def _max_notional_for_edge(summe, liquidity, impact_coefficient):
    """Größte Positionsgröße, bei der der simulierte Kaufimpact den Kurs noch
    NICHT über MAX_EFFECTIVE_BUY_PRICE treibt (danach wäre der Trade ein
    Garantie-Verlust). Ohne Impact-Modell (impact_coefficient=0) gibt es keine
    solche Grenze. Das ersetzt eine feste, willkürliche Liquiditäts-Fraktion:
    bei sehr dünner Liquidität erlaubt das trotzdem eine kleine, gerade noch
    profitable Position, statt den Trade komplett zu verweigern."""
    if impact_coefficient <= 0:
        return math.inf
    # Kleiner Sicherheitsabstand (1e-6), damit die Rekonstruktion von
    # effective_sum aus diesem Notional wegen Floating-Point-Rundung nicht
    # hauchdünn über MAX_EFFECTIVE_BUY_PRICE landet und der Trade am eigenen
    # Sicherheitsnetz scheitert, obwohl er genau dafür berechnet wurde.
    ratio = (MAX_EFFECTIVE_BUY_PRICE - 1e-6) / summe - 1.0
    if ratio <= 0:
        return 0.0
    return liquidity * ratio / impact_coefficient


def _execute_buy(profile, state, h, spread, days, now):
    """Kauft eine Position gemäß Profil (mit/ohne Impact, mit/ohne
    Liquiditäts-Deckel). Gibt True zurück, wenn tatsächlich gekauft wurde."""
    if profile["max_position_fraction"] is not None:
        liquidity_cap = profile["max_position_fraction"] * h["liquiditaet"]
    else:
        liquidity_cap = math.inf
    edge_cap = _max_notional_for_edge(h["summe"], h["liquiditaet"], profile["impact_coefficient"])

    notional = min(state.get("_per_slot_notional", state["cash"]), liquidity_cap, edge_cap, state["cash"])
    if notional < MIN_TRADE_NOTIONAL:
        return False

    impact = profile["impact_coefficient"] * (notional / max(h["liquiditaet"], 1.0))
    effective_sum = h["summe"] * (1 + min(impact, 0.3))
    if effective_sum >= MAX_EFFECTIVE_BUY_PRICE:
        # Sollte durch edge_cap eigentlich schon ausgeschlossen sein - bleibt
        # als Sicherheitsnetz gegen Rundungseffekte.
        return False
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
    log_trade(profile, now.isoformat(), "KAUF", h["market_id"], h["frage"],
              shares, effective_sum, -notional, state["cash"],
              f"Spread {spread*100:.2f}%, Liquiditaet {h['liquiditaet']:.0f} USD")
    return True


def _select_new_trades(profile, state, hits, now):
    open_ids = {p["market_id"] for p in state["positions"]}
    candidates = []
    for h in hits:
        if h["market_id"] in open_ids:
            continue
        spread = 1.0 - h["summe"]
        if spread < profile["min_spread"] or h["liquiditaet"] < profile["min_liquidity"]:
            continue
        days = _days_to_end(h["end_date"], now)
        if days is not None and days <= 0:
            continue
        candidates.append((h, spread, days))

    candidates.sort(key=lambda c: -_score(c[1], c[0]["liquiditaet"], c[2]))
    if not candidates:
        return

    free_slots = profile["max_open_positions"] - len(state["positions"])

    if free_slots <= 0:
        if not profile["reallocate"] or not state["positions"]:
            return
        best_h, best_spread, best_days = candidates[0]
        best_score = _score(best_spread, best_h["liquiditaet"], best_days)
        worst_pos = min(state["positions"], key=lambda p: _position_score(p, now))
        worst_score = _position_score(worst_pos, now)
        if worst_score > 0 and best_score < worst_score * profile["reallocate_factor"]:
            return  # keine Gelegenheit deutlich genug besser als die schwächste Position

        proceeds = worst_pos["shares"] * worst_pos.get("letzter_kurs", worst_pos["einstandskurs"])
        state["cash"] += proceeds
        state["positions"].remove(worst_pos)
        log_trade(profile, now.isoformat(), "UMSCHICHTUNG", worst_pos["market_id"], worst_pos["frage"],
                  worst_pos["shares"], worst_pos.get("letzter_kurs", worst_pos["einstandskurs"]), proceeds,
                  state["cash"], "Für bessere Gelegenheit glattgestellt")
        free_slots = 1

    picks = candidates[:free_slots]
    state["_per_slot_notional"] = state["cash"] / len(picks)
    for h, spread, days in picks:
        if state["cash"] < MIN_TRADE_NOTIONAL:
            break
        _execute_buy(profile, state, h, spread, days, now)
    state.pop("_per_slot_notional", None)


def run_fund_step(profile, markets, hits, now):
    """Wird vom großen Scan aufgerufen: Positionen anhand der schon geladenen
    ~200.000 Marktdaten bewerten/glattstellen, dann neue Treffer aus diesem
    Lauf ('hits') ggf. kaufen. Kein zusätzlicher API-Call."""
    state = load_state(profile)
    if state["started"] is None:
        state["started"] = now.isoformat()

    markets_by_id = {}
    for m in markets:
        key = m.get("id") or m.get("slug")
        markets_by_id[key] = m
        if m.get("slug"):
            markets_by_id.setdefault(m["slug"], m)

    _mark_positions_from_markets(profile, state, markets_by_id, now)
    _select_new_trades(profile, state, hits, now)

    save_state(profile, state)
    nav = log_history(profile, now.isoformat(), state["cash"], _positions_value(state["positions"]), len(state["positions"]))
    print(f"Fonds [{profile['label']}]: NAV {nav:.2f} USD ({(nav/STARTING_CAPITAL-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def run_all_fund_steps(markets, hits, now):
    for profile in PROFILES.values():
        run_fund_step(profile, markets, hits, now)


def watch_positions_step(profile, now):
    """Wird vom hochfrequenten fund_watch.py aufgerufen: kauft nichts Neues,
    prüft nur die schon gehaltenen Positionen per gezieltem Einzel-Lookup."""
    state = load_state(profile)
    if not state["positions"]:
        print(f"Fonds-Watch [{profile['label']}]: keine offenen Positionen, nichts zu tun.")
        return

    _mark_positions_via_lookup(profile, state, now)
    save_state(profile, state)
    nav = log_history(profile, now.isoformat(), state["cash"], _positions_value(state["positions"]), len(state["positions"]))
    print(f"Fonds-Watch [{profile['label']}]: NAV {nav:.2f} USD ({(nav/STARTING_CAPITAL-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def watch_all_positions_step(now):
    for profile in PROFILES.values():
        watch_positions_step(profile, now)
