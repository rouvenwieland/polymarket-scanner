"""
Single-Platform-Fonds für Kalshi und PredictIt ("Solo"-Fonds)
===================================================================

Dieselbe Grundstrategie wie der "conservative"-Fonds bei Polymarket
(fund_simulator.py) - Yes+No < 1 kaufen, bis Auflösung halten oder bei
Konvergenz vorzeitig verkaufen - aber angewandt auf Kalshis bzw. PredictIts
EIGENE Yes/No-Märkte statt auf Polymarket. Zweck: sehen, ob/wie gut dieselbe
Arbitrage-Idee innerhalb JEDER einzelnen Plattform funktioniert, unabhängig
vom Cross-Platform-Matching (cross_platform_fund.py).

Positionsgröße ist - wie beim Cross-Platform-Fonds nach Nutzer-Feedback
angepasst - NICHT künstlich auf eine feste Liquiditäts-Quote begrenzt:
es wird so viel gekauft, wie der simulierte Kaufimpact (an die bekannte
Liquidität/das Volumen gekoppelt) noch zulässt, bevor der Trade
unprofitabel würde (siehe fund_simulator._max_notional_for_edge, hier 1:1
wiederverwendet). Nur bei PredictIt, wo gar keine Liquiditätsangabe
existiert, greift ein fester, konservativer Not-Deckel statt eines
Impact-Modells (dafür fehlen schlicht die Daten).

Gebühren werden real verrechnet:
  - Kalshi: Taker-Fee auf BEIDE Seiten beim Kauf (Yes- und No-Order sind
    zwei getrennte Trades), siehe platforms/kalshi.py.
  - PredictIt: 10% Gebühr auf den Nettogewinn JE SEITE, verrechnet bei
    Verkauf/Auflösung dieser Seite.
"""

import json
import math
import os

import fund_simulator
from platforms import kalshi, predictit

IMPACT_COEFFICIENT = 0.15       # gleicher Wert wie bei den Polymarket-Fonds
MIN_SPREAD = 0.015              # 1.5 Cent Mindest-Edge nach Gebührenschätzung
SELL_CONVERGENCE = 0.997        # verkauft vorzeitig, wenn der Spread fast verschwunden ist
MAX_OPEN_POSITIONS = 20
UNKNOWN_LIQUIDITY_CAP = 20.0    # Not-Deckel NUR ohne verifizierte Liquidität (aktuell: PredictIt)

PROFILES = {
    "kalshi_only": {
        "key": "kalshi_only",
        "label": "Kalshi Solo (100 USD)",
        "platform": "kalshi",
        "starting_capital": 100.0,
        "state_json": "fund_state_kalshi.json",
        "trades_csv": "fund_trades_kalshi.csv",
        "history_csv": "fund_history_kalshi.csv",
    },
    "predictit_only": {
        "key": "predictit_only",
        "label": "PredictIt Solo (100 USD)",
        "platform": "predictit",
        "starting_capital": 100.0,
        "state_json": "fund_state_predictit.json",
        "trades_csv": "fund_trades_predictit.csv",
        "history_csv": "fund_history_predictit.csv",
    },
}


def _empty_state(profile):
    return {"cash": profile["starting_capital"], "positions": [], "started": None}


def load_state(profile):
    path = os.path.join(fund_simulator.DATA_DIR, profile["state_json"])
    if not os.path.isfile(path):
        return _empty_state(profile)
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("cash", profile["starting_capital"])
    state.setdefault("positions", [])
    state.setdefault("started", None)
    return state


def save_state(profile, state):
    os.makedirs(fund_simulator.DATA_DIR, exist_ok=True)
    path = os.path.join(fund_simulator.DATA_DIR, profile["state_json"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_trade(profile, timestamp, action, market_id, frage, shares, price, amount, cash_after, note=""):
    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, profile["trades_csv"]),
        ["timestamp", "aktion", "market_id", "frage", "anteile", "preis", "betrag", "kasse_danach", "notiz"],
        {
            "timestamp": timestamp, "aktion": action, "market_id": market_id, "frage": frage,
            "anteile": round(shares, 4), "preis": round(price, 4), "betrag": round(amount, 4),
            "kasse_danach": round(cash_after, 4), "notiz": note,
        },
    )


def log_history(profile, now, cash, positions):
    positions_value = sum(p["shares"] * p.get("letzter_kurs", p["einstandskurs"]) for p in positions)
    nav = cash + positions_value
    termination_value = cash + sum(p["shares"] * 1.0 for p in positions)
    days_list = [fund_simulator._days_to_end(p.get("end_date"), now) for p in positions]
    discounted_value = cash + sum(
        p["shares"] * 1.0 * fund_simulator._discount_factor(d) for p, d in zip(positions, days_list)
    )
    starting_capital = profile["starting_capital"]

    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, profile["history_csv"]),
        ["timestamp", "kasse", "positionswert", "nav", "rendite_pct", "offene_positionen",
         "terminierungswert", "diskontierter_terminierungswert", "diskontierte_rendite_pct"],
        {
            "timestamp": now.isoformat(),
            "kasse": round(cash, 4),
            "positionswert": round(positions_value, 4),
            "nav": round(nav, 4),
            "rendite_pct": round((nav / starting_capital - 1) * 100, 3),
            "offene_positionen": len(positions),
            "terminierungswert": round(termination_value, 4),
            "diskontierter_terminierungswert": round(discounted_value, 4),
            "diskontierte_rendite_pct": round((discounted_value / starting_capital - 1) * 100, 3),
        },
    )
    return nav


# ---------------------------------------------------------------------
# Kauf neuer Positionen
# ---------------------------------------------------------------------

def _entry_fee(platform, shares, yes_price, no_price):
    if platform == "kalshi":
        return kalshi.taker_fee(shares, yes_price) + kalshi.taker_fee(shares, no_price)
    return 0.0  # PredictIt: Gewinn-Fee erst bei Verkauf/Auflösung


def _max_notional_for_edge(platform, summe, liquidity, fee_per_share):
    """Edge-basierter Deckel (kein künstlicher Fraktions-Anteil): so groß
    wie möglich, bis der simulierte Kaufimpact den Trade unprofitabel
    machen würde. Ohne bekannte Liquidität (PredictIt) gibt es keine
    Datenbasis für ein Impact-Modell - dort greift stattdessen ein fester
    Not-Deckel."""
    if liquidity and liquidity > 0:
        # fund_simulator._max_notional_for_edge kennt selbst keine Gebühr -
        # daher wird der Fee-Anteil vorab vom Schwellenwert abgezogen,
        # indem man ihn wie zusätzlichen "Preis" behandelt: ein Markt mit
        # Gebühr braucht etwas mehr Abstand zu MAX_EFFECTIVE_BUY_PRICE.
        effective_summe = summe + fee_per_share
        return fund_simulator._max_notional_for_edge(effective_summe, liquidity, IMPACT_COEFFICIENT)
    return UNKNOWN_LIQUIDITY_CAP


def _score(spread, days_to_end):
    horizon = max(days_to_end if days_to_end else 0.25, 0.25)
    return spread / horizon


def _select_new_trades(profile, state, markets, now):
    platform = profile["platform"]
    open_ids = {p["market_id"] for p in state["positions"]}
    candidates = []
    for m in markets:
        if m["market_id"] in open_ids:
            continue
        summe = m["yes_price"] + m["no_price"]
        if summe >= 1.0:
            continue
        liquidity = m.get("liquidity") or 0.0
        fee_per_share = _entry_fee(platform, 1.0, m["yes_price"], m["no_price"])
        spread = 1.0 - summe - fee_per_share
        if spread < MIN_SPREAD:
            continue
        days = fund_simulator._days_to_end(m.get("end_date"), now)
        if days is not None and days <= 0:
            continue
        notional_cap = _max_notional_for_edge(platform, summe, liquidity, fee_per_share)
        if notional_cap <= 0:
            continue
        candidates.append({"market": m, "summe": summe, "spread": spread, "days": days, "notional_cap": notional_cap})

    candidates.sort(key=lambda c: -_score(c["spread"], c["days"]))
    if not candidates:
        return

    free_slots = MAX_OPEN_POSITIONS - len(state["positions"])
    if free_slots <= 0:
        return

    picks = candidates[:free_slots]
    per_slot_cash = state["cash"] / len(picks)
    for c in picks:
        if state["cash"] < fund_simulator.MIN_TRADE_NOTIONAL:
            break
        notional = min(per_slot_cash, c["notional_cap"])
        _execute_buy(profile, state, c, notional, now)


def _execute_buy(profile, state, c, notional, now):
    m = c["market"]
    notional = min(notional, state["cash"])
    if notional < fund_simulator.MIN_TRADE_NOTIONAL:
        return False

    shares = notional / c["summe"]
    fee = _entry_fee(profile["platform"], shares, m["yes_price"], m["no_price"])
    total_cost = notional + fee
    if total_cost > state["cash"]:
        # Gebühr nicht in der Notional-Berechnung enthalten - bei knapper
        # Kasse proportional zurückskalieren statt den Trade zu verwerfen
        # (siehe cross_platform_fund.py für dieselbe Begründung).
        scale = 0.998 * state["cash"] / total_cost
        shares *= scale
        notional = shares * c["summe"]
        fee = _entry_fee(profile["platform"], shares, m["yes_price"], m["no_price"])
        total_cost = notional + fee
        if total_cost > state["cash"] or notional < fund_simulator.MIN_TRADE_NOTIONAL:
            return False

    state["cash"] -= total_cost
    state["positions"].append({
        "market_id": m["market_id"],
        "frage": f"[{profile['platform']}] {m['question']}",
        "shares": shares,
        "einstandskurs": c["summe"],
        "letzter_kurs": c["summe"],
        "liquiditaet": m.get("liquidity") or 0.0,
        "end_date": m.get("end_date"),
        "eingestiegen": now.isoformat(),
        "fee_gezahlt": fee,
    })
    log_trade(profile, now.isoformat(), "KAUF", m["market_id"], state["positions"][-1]["frage"],
              shares, c["summe"], -total_cost, state["cash"],
              f"Spread {c['spread']*100:.2f}% nach geschaetzter Gebuehr, Edge-Limit {c['notional_cap']:.0f} USD")
    return True


# ---------------------------------------------------------------------
# Bewertung / Auflösung offener Positionen
# ---------------------------------------------------------------------

def _settle_fee(platform, proceeds_per_share, einstandskurs):
    if platform == "predictit":
        profit = max(0.0, proceeds_per_share - einstandskurs)
        return predictit.FEE_RATE_PROFIT * profit
    return 0.0


def _single_lookup_sum(platform, market_id):
    """Gezielter Einzel-Lookup für eine Position, die im aktuellen Bulk-
    Scan nicht mehr auftaucht (vermutlich aufgelöst/geschlossen). Gibt
    (current_sum, resolved) zurück - resolved=True heißt: Markt ist zu,
    current_sum ist dann die Summe aus den beiden Abwicklungspreisen
    (0.0 oder 1.0 je Seite, je nach Ausgang)."""
    if platform == "kalshi":
        market = kalshi.fetch_market_by_ticker(market_id)
        if market is None:
            return None, False
        status, result = market.get("status"), market.get("result")
        if status not in ("finalized", "settled") or not result:
            try:
                return float(market["yes_ask_dollars"]) + float(market["no_ask_dollars"]), False
            except (KeyError, TypeError, ValueError):
                return None, False
        return 1.0, True  # Yes+No zusammen zahlen immer genau 1.00, unabhängig vom Ausgang
    if platform == "predictit":
        _, contract = predictit.fetch_contract_by_market_id(market_id)
        if contract is None:
            return None, False
        if contract.get("status") == "Open":
            try:
                return float(contract["bestBuyYesCost"]) + float(contract["bestBuyNoCost"]), False
            except (KeyError, TypeError, ValueError):
                return None, False
        return 1.0, True
    return None, False


def _evaluate_position(platform, pos, market):
    """Analog zu fund_simulator._evaluate_position, aber plattform-agnostisch
    über die normalisierten market-dicts. market=None heißt: nicht mehr im
    aktuellen Bulk-Scan gefunden (separat per Einzel-Lookup zu behandeln)."""
    if market is None:
        return {"action": "lookup"}

    current_sum = market["yes_price"] + market["no_price"]
    if current_sum >= SELL_CONVERGENCE:
        liquidity_now = market.get("liquidity") or pos["liquiditaet"]
        notional = pos["shares"] * current_sum
        impact = IMPACT_COEFFICIENT * (notional / max(liquidity_now, 1.0))
        effective_sum = current_sum * (1 - min(impact, 0.3))
        return {"action": "sell", "effective_sum": effective_sum, "current_sum": current_sum}

    return {"action": "hold", "current_sum": current_sum}


def _apply_decision(profile, state, pos, decision, now):
    platform = profile["platform"]

    if decision["action"] == "lookup":
        current_sum, resolved = _single_lookup_sum(platform, pos["market_id"])
        if current_sum is None:
            return False  # vorübergehender Fehler - nächstes Mal erneut versuchen
        if resolved:
            decision = {"action": "settle", "proceeds_per_share": current_sum}
        elif current_sum >= SELL_CONVERGENCE:
            # Konvergenz auch auf dem Einzel-Lookup-Pfad erkennen (Watcher
            # hat keinen Bulk-Markt-dict, aber liquiditaet aus der Position
            # reicht fürs selbe Impact-Modell wie in _evaluate_position).
            liquidity_now = pos["liquiditaet"]
            notional = pos["shares"] * current_sum
            impact = IMPACT_COEFFICIENT * (notional / max(liquidity_now, 1.0))
            effective_sum = current_sum * (1 - min(impact, 0.3))
            decision = {"action": "sell", "effective_sum": effective_sum, "current_sum": current_sum}
        else:
            pos["letzter_kurs"] = current_sum
            return False

    if decision["action"] == "settle":
        proceeds_per_share = decision.get("proceeds_per_share", 1.0)
        fee = _settle_fee(platform, proceeds_per_share, pos["einstandskurs"])
        proceeds = pos["shares"] * proceeds_per_share - fee
        state["cash"] += proceeds
        log_trade(profile, now.isoformat(), "AUSZAHLUNG", pos["market_id"], pos["frage"],
                  pos["shares"], proceeds_per_share, proceeds, state["cash"], "Markt aufgelöst")
        return True

    if decision["action"] == "sell":
        fee = _settle_fee(platform, decision["effective_sum"], pos["einstandskurs"])
        proceeds = pos["shares"] * decision["effective_sum"] - fee
        state["cash"] += proceeds
        log_trade(profile, now.isoformat(), "VERKAUF", pos["market_id"], pos["frage"],
                  pos["shares"], decision["effective_sum"], proceeds, state["cash"],
                  f"Konvergenz erreicht (Kurs {decision['current_sum']:.3f})")
        return True

    # hold
    if decision.get("current_sum") is not None:
        pos["letzter_kurs"] = decision["current_sum"]
    return False


def run_fund_step(profile, markets, now):
    """markets: aktuell normalisierte, OFFENE Märkte dieser Plattform aus
    dem gerade gelaufenen Multi-Platform-Scan (kein zusätzlicher API-Call
    für die Bewertung nötig, außer für Positionen, die darin fehlen)."""
    state = load_state(profile)
    if state["started"] is None:
        state["started"] = now.isoformat()

    markets_by_id = {m["market_id"]: m for m in markets}

    still_open = []
    for pos in state["positions"]:
        decision = _evaluate_position(profile["platform"], pos, markets_by_id.get(pos["market_id"]))
        closed = _apply_decision(profile, state, pos, decision, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open

    _select_new_trades(profile, state, markets, now)

    save_state(profile, state)
    nav = log_history(profile, now, state["cash"], state["positions"])
    print(f"Fonds [{profile['label']}]: NAV {nav:.2f} USD "
          f"({(nav/profile['starting_capital']-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def run_all_fund_steps(markets_by_platform, now):
    for profile in PROFILES.values():
        run_fund_step(profile, markets_by_platform.get(profile["platform"], []), now)


def watch_step(profile, now):
    """Hochfrequenter Watcher: kauft nichts Neues, prüft offene Positionen
    per gezieltem Einzel-Lookup. _apply_decision() behandelt die Aktion
    "lookup" bereits selbst über _single_lookup_sum() - kein Bulk-Scan
    nötig, derselbe Code-Pfad wie bei einer im Bulk-Scan fehlenden Position."""
    state = load_state(profile)
    if not state["positions"]:
        print(f"Fonds-Watch [{profile['label']}]: keine offenen Positionen, nichts zu tun.")
        return

    still_open = []
    for pos in state["positions"]:
        closed = _apply_decision(profile, state, pos, {"action": "lookup"}, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open

    save_state(profile, state)
    nav = log_history(profile, now, state["cash"], state["positions"])
    print(f"Fonds-Watch [{profile['label']}]: NAV {nav:.2f} USD "
          f"({(nav/profile['starting_capital']-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def watch_all_positions_step(now):
    for profile in PROFILES.values():
        watch_step(profile, now)
