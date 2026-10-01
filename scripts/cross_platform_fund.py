"""
Cross-Platform-Arbitrage-Fonds (4. Fonds, Simulation)
=========================================================

Vierter Papier-Trading-Fonds, parallel zu den drei Polymarket-only-Fonds in
fund_simulator.py. Start: 100 USD, gleiche Grundidee (Position, die bei
identischer Auflösung garantiert 1.00 USD/Anteilspaar auszahlt), aber die
beiden Seiten kommen von ZWEI VERSCHIEDENEN Plattformen statt von Yes+No
desselben Marktes:

  Leg A: Yes (oder No) auf Plattform A
  Leg B: No  (oder Yes) auf Plattform B, GLEICHES Realwelt-Ereignis

...gefunden über cross_platform.find_cross_platform_matches() (Text-
ähnlichkeit + Enddatum-Nähe - eine Heuristik, kein Beweis: ein falsches
Match ist das Hauptrisiko dieser Strategie, siehe cross_platform.py).

Gebühren werden GENAU dort verrechnet, wo sie real anfallen:
  - Kalshi: Taker-Fee beim Kauf (ceil(0.07*C*P*(1-P)), siehe platforms/kalshi.py).
  - PredictIt: 10% Gebühr auf den Nettogewinn JE BEIN, verrechnet bei
    Auflösung dieses Beins (nicht vorher, da der Gewinn erst dann feststeht).
  - Polymarket: keine Trading-Fee.
Vereinfachung: die Kalshi-Fee wird nur beim Einstieg verrechnet (nicht bei
vorzeitigem Verkauf/Settlement) - Gegenstück zur MIN_TRADE_NOTIONAL-Rundung
bei den anderen Fonds. Die PredictIt-Auszahlungsgebühr (5%, Konto-Ebene)
wird bewusst NICHT pro Trade verrechnet (siehe platforms/predictit.py).

Da Liquidität auf Kalshi nur grob (Volumen-Proxy) und auf PredictIt gar
nicht bekannt ist, gibt es hier - anders als bei den Polymarket-Fonds -
keinen liquiditätsbasierten Kaufimpact, sondern einen festen,
konservativen Notional-Deckel pro Bein (MAX_LEG_NOTIONAL).
"""

import json
import os

import fund_simulator
from platforms import kalshi, predictit
import cross_platform

PROFILE = {
    "key": "cross_platform",
    "label": "Cross-Platform (Polymarket+Kalshi+PredictIt)",
    "state_json": "fund_state_crossplatform.json",
    "trades_csv": "fund_trades_crossplatform.csv",
    "history_csv": "fund_history_crossplatform.csv",
    "min_spread": 0.03,          # nach grob geschätzten Gebühren noch 3 Cent Edge pro Paar nötig
    "max_leg_notional": 10.0,    # fester Deckel pro Bein (konservativ, Liquidität auf 2/3 Plattformen unbekannt)
    "max_open_positions": 15,
    "match_threshold": cross_platform.SIMILARITY_THRESHOLD,
    "match_max_days": cross_platform.MAX_DAYS_APART,
}


def _empty_state():
    return {"cash": fund_simulator.STARTING_CAPITAL, "positions": [], "started": None}


def load_state():
    path = os.path.join(fund_simulator.DATA_DIR, PROFILE["state_json"])
    if not os.path.isfile(path):
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("cash", fund_simulator.STARTING_CAPITAL)
    state.setdefault("positions", [])
    state.setdefault("started", None)
    return state


def save_state(state):
    os.makedirs(fund_simulator.DATA_DIR, exist_ok=True)
    path = os.path.join(fund_simulator.DATA_DIR, PROFILE["state_json"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_trade(timestamp, action, pair_id, frage, shares, price, amount, cash_after, note=""):
    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, PROFILE["trades_csv"]),
        ["timestamp", "aktion", "pair_id", "frage", "anteile", "preis", "betrag", "kasse_danach", "notiz"],
        {
            "timestamp": timestamp, "aktion": action, "pair_id": pair_id, "frage": frage,
            "anteile": round(shares, 4), "preis": round(price, 4), "betrag": round(amount, 4),
            "kasse_danach": round(cash_after, 4), "notiz": note,
        },
    )


def log_history(now, cash, positions):
    positions_value = sum(p["shares"] * (p["leg_a"]["letzter_kurs"] + p["leg_b"]["letzter_kurs"]) for p in positions)
    nav = cash + positions_value
    # Terminierungswert: wie bei den Polymarket-Fonds wird angenommen, dass
    # ein korrekt gematchtes Paar bei Auflösung garantiert 1.00 USD/Anteil
    # auszahlt (Kernannahme der Strategie, siehe Moduldoku).
    termination_value = cash + sum(p["shares"] * 1.0 for p in positions)
    days_list = [fund_simulator._days_to_end(p.get("end_date"), now) for p in positions]
    discounted_value = cash + sum(
        p["shares"] * 1.0 * fund_simulator._discount_factor(d) for p, d in zip(positions, days_list)
    )

    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, PROFILE["history_csv"]),
        ["timestamp", "kasse", "positionswert", "nav", "rendite_pct", "offene_positionen",
         "terminierungswert", "diskontierter_terminierungswert", "diskontierte_rendite_pct"],
        {
            "timestamp": now.isoformat(),
            "kasse": round(cash, 4),
            "positionswert": round(positions_value, 4),
            "nav": round(nav, 4),
            "rendite_pct": round((nav / fund_simulator.STARTING_CAPITAL - 1) * 100, 3),
            "offene_positionen": len(positions),
            "terminierungswert": round(termination_value, 4),
            "diskontierter_terminierungswert": round(discounted_value, 4),
            "diskontierte_rendite_pct": round((discounted_value / fund_simulator.STARTING_CAPITAL - 1) * 100, 3),
        },
    )
    return nav


# ---------------------------------------------------------------------
# Kauf neuer Positionen
# ---------------------------------------------------------------------

def _leg_entry_fee(platform, shares, price):
    if platform == "kalshi":
        return kalshi.taker_fee(shares, price)
    return 0.0  # Polymarket: keine Fee. PredictIt: Gewinn-Fee erst bei Settlement.


def _score(spread, days_to_end):
    horizon = max(days_to_end if days_to_end else 0.25, 0.25)
    return spread / horizon


def _select_new_trades(state, matches_by_platforms, now):
    open_pair_ids = {p["pair_id"] for p in state["positions"]}
    candidates = []
    for match in matches_by_platforms:
        ma, mb = match["market_a"], match["market_b"]
        pair_id = f"{ma['platform']}:{ma['market_id']}__{mb['platform']}:{mb['market_id']}"
        if pair_id in open_pair_ids:
            continue
        side_a, side_b, summe = cross_platform.best_arbitrage_combo(ma, mb)

        # grobe Gebührenabschätzung für die Auswahl (bei 1 Anteil) - reduziert
        # den rechnerischen Spread, damit nur nach Gebühren noch profitable
        # Gelegenheiten überhaupt in Betracht kommen.
        price_a = ma["yes_price"] if side_a == "yes" else ma["no_price"]
        price_b = mb["yes_price"] if side_b == "yes" else mb["no_price"]
        est_fee_per_share = _leg_entry_fee(ma["platform"], 1.0, price_a) + _leg_entry_fee(mb["platform"], 1.0, price_b)
        spread = 1.0 - summe - est_fee_per_share
        if spread < PROFILE["min_spread"]:
            continue

        end_a = fund_simulator._days_to_end(ma.get("end_date"), now)
        end_b = fund_simulator._days_to_end(mb.get("end_date"), now)
        days_candidates = [d for d in (end_a, end_b) if d is not None]
        if days_candidates and min(days_candidates) <= 0:
            continue
        days = max(days_candidates) if days_candidates else None  # Paar ist erst "fertig", wenn BEIDE Beine aufgelöst sind
        end_date = None
        if ma.get("end_date") and mb.get("end_date"):
            end_date = ma["end_date"] if end_a is None or (end_b is not None and end_a >= end_b) else mb["end_date"]
        else:
            end_date = ma.get("end_date") or mb.get("end_date")

        candidates.append({
            "pair_id": pair_id, "match": match, "side_a": side_a, "side_b": side_b,
            "summe": summe, "spread": spread, "days": days, "end_date": end_date,
        })

    candidates.sort(key=lambda c: -_score(c["spread"], c["days"]))
    if not candidates:
        return

    free_slots = PROFILE["max_open_positions"] - len(state["positions"])
    if free_slots <= 0:
        return

    picks = candidates[:free_slots]
    per_slot_notional = min(state["cash"] / len(picks), PROFILE["max_leg_notional"] * 2)
    for c in picks:
        if state["cash"] < fund_simulator.MIN_TRADE_NOTIONAL:
            break
        _execute_buy(state, c, per_slot_notional, now)


def _execute_buy(state, c, notional, now):
    ma, mb = c["match"]["market_a"], c["match"]["market_b"]
    notional = min(notional, state["cash"])
    if notional < fund_simulator.MIN_TRADE_NOTIONAL:
        return False

    shares = notional / c["summe"]
    price_a = ma["yes_price"] if c["side_a"] == "yes" else ma["no_price"]
    price_b = mb["yes_price"] if c["side_b"] == "yes" else mb["no_price"]
    fee_a = _leg_entry_fee(ma["platform"], shares, price_a)
    fee_b = _leg_entry_fee(mb["platform"], shares, price_b)
    total_cost = shares * price_a + shares * price_b + fee_a + fee_b
    if total_cost > state["cash"]:
        return False  # Sicherheitsnetz - sollte durch die Slot-Größe oben praktisch nie greifen

    state["cash"] -= total_cost
    state["positions"].append({
        "pair_id": c["pair_id"],
        "frage": f"[{ma['platform']}] {ma['question']}  <->  [{mb['platform']}] {mb['question']}",
        "shares": shares,
        "end_date": c["end_date"],
        "eingestiegen": now.isoformat(),
        "leg_a": {
            "platform": ma["platform"], "market_id": ma["market_id"], "side": c["side_a"],
            "einstandskurs": price_a, "letzter_kurs": price_a, "fee_gezahlt": fee_a,
        },
        "leg_b": {
            "platform": mb["platform"], "market_id": mb["market_id"], "side": c["side_b"],
            "einstandskurs": price_b, "letzter_kurs": price_b, "fee_gezahlt": fee_b,
        },
    })
    log_trade(now.isoformat(), "KAUF", c["pair_id"], state["positions"][-1]["frage"],
              shares, c["summe"], -total_cost, state["cash"],
              f"Spread {c['spread']*100:.2f}% nach geschaetzten Gebuehren, "
              f"Beine: {ma['platform']}/{c['side_a']} + {mb['platform']}/{c['side_b']}")
    return True


# ---------------------------------------------------------------------
# Bewertung / Auflösung offener Positionen
# ---------------------------------------------------------------------

def _poly_leg_mark_and_result(leg):
    market = fund_simulator.fetch_market_by_id(leg["market_id"])
    if market is None:
        return None, None  # (aktueller Kurs oder None, Payout oder None falls noch offen)
    prices = fund_simulator.parse_prices(market)
    current_price = None
    if prices is not None:
        yes_p, no_p = prices
        current_price = yes_p if leg["side"] == "yes" else no_p
    if not market.get("closed"):
        return current_price, None
    if current_price is None:
        return None, None
    return current_price, (1.0 if current_price >= 0.5 else 0.0)


def _kalshi_leg_mark_and_result(leg):
    market = kalshi.fetch_market_by_ticker(leg["market_id"])
    if market is None:
        return None, None
    ask_field = "yes_ask_dollars" if leg["side"] == "yes" else "no_ask_dollars"
    try:
        current_price = float(market.get(ask_field))
    except (TypeError, ValueError):
        current_price = None
    status, result = market.get("status"), market.get("result")
    if status not in ("finalized", "settled") or not result:
        return current_price, None
    return current_price, (1.0 if result == leg["side"] else 0.0)


def _predictit_leg_mark_and_result(leg):
    _, contract = predictit.fetch_contract_by_market_id(leg["market_id"])
    if contract is None:
        return None, None
    cost_field = "bestBuyYesCost" if leg["side"] == "yes" else "bestBuyNoCost"
    try:
        current_price = float(contract.get(cost_field))
    except (TypeError, ValueError):
        current_price = None
    if contract.get("status") == "Open":
        return current_price, None
    try:
        last_price = float(contract.get("lastTradePrice") or 0.0)
    except (TypeError, ValueError):
        last_price = 0.0
    yes_won = last_price >= 0.5
    payout = 1.0 if (yes_won and leg["side"] == "yes") or (not yes_won and leg["side"] == "no") else 0.0
    return current_price, payout


_LEG_HANDLERS = {
    "polymarket": _poly_leg_mark_and_result,
    "kalshi": _kalshi_leg_mark_and_result,
    "predictit": _predictit_leg_mark_and_result,
}


def _settle_leg(leg, payout):
    """Zahlt ein einzelnes Bein aus, inkl. plattformspezifischer Gebühr auf
    den Nettogewinn (aktuell nur bei PredictIt). Gibt die Netto-Proceeds zurück."""
    shares_proceeds = payout  # pro Anteil - wird vom Aufrufer mit shares multipliziert
    if leg["platform"] == "predictit":
        fee = predictit.FEE_RATE_PROFIT * max(0.0, payout - leg["einstandskurs"])
        return shares_proceeds - fee
    return shares_proceeds


def _mark_and_maybe_settle(state, pos, now):
    """Holt für jedes Bein einen gezielten Einzel-Lookup (anders als bei den
    Polymarket-only-Fonds gibt es hier keinen günstigeren "aus dem Bulk-Scan
    markieren"-Pfad, da die drei Plattform-Bulk-Listen keine stabile ID-Map
    wie Polymarket liefern/benötigen - bei max. ~15 offenen Positionen x 2
    Beinen ist das vertretbar). Gibt True zurück, wenn die Position
    VOLLSTÄNDIG geschlossen wurde (beide Beine aufgelöst)."""
    results = {}
    for leg_key in ("leg_a", "leg_b"):
        leg = pos[leg_key]
        handler = _LEG_HANDLERS[leg["platform"]]
        current_price, payout = handler(leg)
        if current_price is not None:
            leg["letzter_kurs"] = current_price
        results[leg_key] = payout

    if results["leg_a"] is None or results["leg_b"] is None:
        return False  # mindestens ein Bein noch offen - warten, nichts erzwingen

    proceeds_per_share = (
        _settle_leg(pos["leg_a"], results["leg_a"]) + _settle_leg(pos["leg_b"], results["leg_b"])
    )
    proceeds = pos["shares"] * proceeds_per_share
    state["cash"] += proceeds
    log_trade(now.isoformat(), "AUSZAHLUNG", pos["pair_id"], pos["frage"], pos["shares"],
              proceeds_per_share, proceeds, state["cash"],
              f"Beide Beine aufgelöst (Payout A={results['leg_a']:.2f}, B={results['leg_b']:.2f})")
    return True


def run_fund_step(all_matches, now):
    """Wird vom Multi-Platform-Scan aufgerufen: bewertet offene Positionen
    anhand der schon gefundenen Matches/Marktdaten UND kauft neue."""
    state = load_state()
    if state["started"] is None:
        state["started"] = now.isoformat()

    still_open = []
    for pos in state["positions"]:
        closed = _mark_and_maybe_settle(state, pos, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open

    _select_new_trades(state, all_matches, now)

    save_state(state)
    nav = log_history(now, state["cash"], state["positions"])
    print(f"Fonds [{PROFILE['label']}]: NAV {nav:.2f} USD "
          f"({(nav/fund_simulator.STARTING_CAPITAL-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def watch_step(now):
    """Hochfrequenter Watcher: prüft nur offene Positionen per Einzel-Lookup,
    kauft nichts Neues."""
    state = load_state()
    if not state["positions"]:
        print(f"Fonds-Watch [{PROFILE['label']}]: keine offenen Positionen, nichts zu tun.")
        return

    still_open = []
    for pos in state["positions"]:
        closed = _mark_and_maybe_settle(state, pos, now)
        if not closed:
            still_open.append(pos)
    state["positions"] = still_open

    save_state(state)
    nav = log_history(now, state["cash"], state["positions"])
    print(f"Fonds-Watch [{PROFILE['label']}]: NAV {nav:.2f} USD "
          f"({(nav/fund_simulator.STARTING_CAPITAL-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")
