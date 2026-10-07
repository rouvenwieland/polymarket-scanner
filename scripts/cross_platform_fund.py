"""
Cross-Platform-Arbitrage-Fonds (3 parallele Kapitalstufen, Simulation)
=========================================================================

Drei parallele Papier-Trading-Fonds (100 / 1.000 / 10.000 USD Start),
die ALLE exakt dieselbe Strategie fahren - einziger Unterschied ist das
Startkapital. Zweck: beobachten, wie gut sich diese Cross-Platform-
Arbitrage SKALIEREN lässt, bevor man sie mit mehr Kapital "fahren" würde.
Da die Positionsgröße je Bein an die bekannte Marktliquidität gekoppelt
ist (siehe unten), ist zu erwarten, dass die Rendite bei 10.000 USD NICHT
einfach 100x so hoch ausfällt wie bei 100 USD, sondern durch zu wenige
gleichzeitig verfügbare, groß genug handelbare Gelegenheiten gebremst wird -
genau das soll hier sichtbar werden.

Grundidee (identisch zu den Polymarket-only-Fonds in fund_simulator.py):
eine Position, die bei identischer Auflösung garantiert 1.00 USD/Anteilspaar
auszahlt - hier kommen die beiden Seiten aber von ZWEI VERSCHIEDENEN
Plattformen statt von Yes+No desselben Marktes:

  Leg A: Yes (oder No) auf Plattform A
  Leg B: No  (oder Yes) auf Plattform B, GLEICHES Realwelt-Ereignis

...gefunden über cross_platform.find_cross_platform_matches() (Text-
ähnlichkeit + Enddatum-Nähe - eine Heuristik, kein Beweis: ein falsches
Match ist das Hauptrisiko dieser Strategie, siehe cross_platform.py).

Positionsgröße ist NICHT künstlich auf eine feste Quote der Liquidität
begrenzt - die Strategie kauft so viel wie bei einer guten Gelegenheit
sinnvoll möglich ist, begrenzt nur dadurch, wo es nach dem simulierten
Kaufimpact (siehe unten) noch profitabel bleibt. Das ist derselbe Ansatz,
den auch der "aggressive"-Fonds bei Polymarket fährt - "konservativ" heißt
hier NICHT "künstlich klein", sondern "realistisch gerechnet": reale
Gebühren, reales Impact-Modell, frühzeitiger Verkauf bei Konvergenz statt
Kapital bis zur Auflösung zu binden.
  - Positionsgröße je Paar: größte Stückzahl, bei der die Summe aus beiden
    (je nach eigener Liquidität individuell einpreisten) Kaufimpacts den
    Trade noch nicht unprofitabel macht (siehe _max_shares_for_edge unten,
    ein Zwei-Beine-Analogon zu fund_simulator._max_notional_for_edge). Nur
    für ein Bein OHNE verifizierte Liquidität (aktuell: PredictIts
    öffentliche API liefert keine) gilt ersatzweise ein fester, kleiner
    Not-Deckel (UNKNOWN_LIQUIDITY_LEG_CAP) - nicht weil wir uns künstlich
    zurückhalten wollen, sondern weil ohne Tiefenangabe schlicht keine
    seriöse Impact-Schätzung möglich ist.
  - Mindest-Spread NACH geschätzten Gebühren (MIN_SPREAD) als Einstiegs-Filter.

Gebühren werden GENAU dort verrechnet, wo sie real anfallen:
  - Kalshi: Taker-Fee beim Kauf (ceil(0.07*C*P*(1-P)), siehe platforms/kalshi.py).
  - PredictIt: 10% Gebühr auf den Nettogewinn JE BEIN, verrechnet bei
    Auflösung dieses Beins (nicht vorher, da der Gewinn erst dann feststeht).
  - Polymarket: keine Trading-Fee (anders als bei den anderen beiden
    Plattformen gibt es hier real keine Gebühr zu verrechnen - bewusst
    nicht künstlich hinzugefügt).
Vereinfachung: die Kalshi-Fee wird nur beim Einstieg verrechnet (nicht bei
vorzeitigem Verkauf/Settlement). Die PredictIt-Auszahlungsgebühr (5%,
Konto-Ebene) wird bewusst NICHT pro Trade verrechnet (siehe platforms/predictit.py).
"""

import json
import math
import os

import fund_simulator
from platforms import kalshi, predictit, sxbet
import cross_platform

MIN_SPREAD = 0.03                 # nach grob geschätzten Gebühren noch 3 Cent Edge pro Paar nötig
IMPACT_COEFFICIENT = 0.15         # gleicher Wert wie bei den Polymarket-Fonds (fund_simulator.py)
UNKNOWN_LIQUIDITY_LEG_CAP = 10.0  # Not-Deckel NUR für Beine ohne verifizierte Liquidität (aktuell: PredictIt)
MAX_OPEN_POSITIONS = 15
MATCH_THRESHOLD = cross_platform.SIMILARITY_THRESHOLD
MATCH_MAX_DAYS = cross_platform.MAX_DAYS_APART

# Drei identische Strategien, nur das Startkapital unterscheidet sich - um
# zu testen, wie sich die Strategie mit mehr Kapital verhält (siehe Moduldoku).
PROFILES = {
    "cross_platform": {
        "key": "cross_platform",
        "label": "Cross-Platform (100 USD)",
        "starting_capital": 100.0,
        "state_json": "fund_state_crossplatform.json",
        "trades_csv": "fund_trades_crossplatform.csv",
        "history_csv": "fund_history_crossplatform.csv",
    },
    "cross_platform_1k": {
        "key": "cross_platform_1k",
        "label": "Cross-Platform (1.000 USD)",
        "starting_capital": 1000.0,
        "state_json": "fund_state_crossplatform_1k.json",
        "trades_csv": "fund_trades_crossplatform_1k.csv",
        "history_csv": "fund_history_crossplatform_1k.csv",
    },
    "cross_platform_10k": {
        "key": "cross_platform_10k",
        "label": "Cross-Platform (10.000 USD)",
        "starting_capital": 10000.0,
        "state_json": "fund_state_crossplatform_10k.json",
        "trades_csv": "fund_trades_crossplatform_10k.csv",
        "history_csv": "fund_history_crossplatform_10k.csv",
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


def log_trade(profile, timestamp, action, pair_id, frage, shares, price, amount, cash_after, note=""):
    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, profile["trades_csv"]),
        ["timestamp", "aktion", "pair_id", "frage", "anteile", "preis", "betrag", "kasse_danach", "notiz"],
        {
            "timestamp": timestamp, "aktion": action, "pair_id": pair_id, "frage": frage,
            "anteile": round(shares, 4), "preis": round(price, 4), "betrag": round(amount, 4),
            "kasse_danach": round(cash_after, 4), "notiz": note,
        },
    )


def log_history(profile, now, cash, positions):
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

def _leg_entry_fee(platform, shares, price):
    if platform == "kalshi":
        return kalshi.taker_fee(shares, price)
    return 0.0  # Polymarket: keine Fee. PredictIt: Gewinn-Fee erst bei Settlement.


def _max_shares_for_edge(price_a, liquidity_a, price_b, liquidity_b, fee_per_share):
    """Größte Stückzahl für EIN Anteilspaar (beide Beine zusammen), bei der
    der simulierte Kaufimpact - je Bein an dessen EIGENE bekannte Liquidität
    gekoppelt - die Kombi-Summe noch nicht über MAX_EFFECTIVE_BUY_PRICE
    treibt. Kein künstlicher Fraktions-Deckel: es wird so weit gekauft, wie
    der Trade noch profitabel bleibt - ein Zwei-Beine-Analogon zu
    fund_simulator._max_notional_for_edge (das Impact-Modell ist linear in
    der Stückzahl, daher lässt sich die Grenze direkt auflösen statt sie
    iterativ zu suchen).

    Für ein Bein OHNE bekannte Liquidität (<=0) wird dessen Impact NICHT
    modelliert (dafür gibt es schlicht keine Daten) - der Aufrufer muss für
    dieses Bein stattdessen UNKNOWN_LIQUIDITY_LEG_CAP als harte Grenze
    durchsetzen, siehe _select_new_trades."""
    quad_coef = 0.0
    if liquidity_a > 0:
        quad_coef += IMPACT_COEFFICIENT * price_a * price_a / liquidity_a
    if liquidity_b > 0:
        quad_coef += IMPACT_COEFFICIENT * price_b * price_b / liquidity_b

    # Kleiner Sicherheitsabstand (1e-6), siehe fund_simulator._max_notional_for_edge.
    headroom = (fund_simulator.MAX_EFFECTIVE_BUY_PRICE - 1e-6) - fee_per_share - price_a - price_b
    if headroom <= 0:
        return 0.0
    if quad_coef <= 0:
        return math.inf  # beide Beine ohne Liquiditätsdaten - Deckel kommt ausschließlich vom Not-Deckel
    return headroom / quad_coef


def _score(spread, days_to_end):
    horizon = max(days_to_end if days_to_end else 0.25, 0.25)
    return spread / horizon


def _select_new_trades(profile, state, matches_by_platforms, now):
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
        if spread < MIN_SPREAD:
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

        liquidity_a = ma.get("liquidity") or 0.0
        liquidity_b = mb.get("liquidity") or 0.0
        edge_shares_cap = _max_shares_for_edge(price_a, liquidity_a, price_b, liquidity_b, est_fee_per_share)
        hard_caps = []
        if liquidity_a <= 0:
            hard_caps.append(UNKNOWN_LIQUIDITY_LEG_CAP / price_a)
        if liquidity_b <= 0:
            hard_caps.append(UNKNOWN_LIQUIDITY_LEG_CAP / price_b)
        shares_cap = min([edge_shares_cap] + hard_caps) if hard_caps else edge_shares_cap
        if shares_cap <= 0:
            continue

        candidates.append({
            "pair_id": pair_id, "match": match, "side_a": side_a, "side_b": side_b,
            "summe": summe, "spread": spread, "days": days, "end_date": end_date,
            "shares_cap": shares_cap,
        })

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
        cash_based_shares = per_slot_cash / c["summe"]
        shares = min(cash_based_shares, c["shares_cap"])
        _execute_buy(profile, state, c, shares, now)


def _execute_buy(profile, state, c, shares, now):
    ma, mb = c["match"]["market_a"], c["match"]["market_b"]
    if shares * c["summe"] < fund_simulator.MIN_TRADE_NOTIONAL:
        return False

    price_a = ma["yes_price"] if c["side_a"] == "yes" else ma["no_price"]
    price_b = mb["yes_price"] if c["side_b"] == "yes" else mb["no_price"]
    fee_a = _leg_entry_fee(ma["platform"], shares, price_a)
    fee_b = _leg_entry_fee(mb["platform"], shares, price_b)
    total_cost = shares * price_a + shares * price_b + fee_a + fee_b
    if total_cost > state["cash"]:
        # Die Gebühr steckt nicht in der ursprünglichen Notional-Berechnung -
        # bei knapper Kasse (v.a. beim 100-USD-Fonds) kann das Budget dadurch
        # leicht überschritten werden. Einmal proportional zurückskalieren
        # (Kalshi-Fee ist näherungsweise linear in der Stückzahl), statt den
        # Trade direkt zu verwerfen.
        # Kleiner Sicherheitsabstand (0.2%): die Kalshi-Fee rundet auf den
        # Cent AUF (ceil), ist also nicht exakt linear in der Stückzahl -
        # eine reine Verhältnis-Skalierung kann das Cash-Limit dadurch um
        # Bruchteile eines Cents knapp wieder überschreiten.
        scale = 0.998 * state["cash"] / total_cost
        shares *= scale
        fee_a = _leg_entry_fee(ma["platform"], shares, price_a)
        fee_b = _leg_entry_fee(mb["platform"], shares, price_b)
        total_cost = shares * price_a + shares * price_b + fee_a + fee_b
        if total_cost > state["cash"] or shares * c["summe"] < fund_simulator.MIN_TRADE_NOTIONAL:
            return False  # auch nach Rückskalierung zu knapp - Sicherheitsnetz

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
    log_trade(profile, now.isoformat(), "KAUF", c["pair_id"], state["positions"][-1]["frage"],
              shares, c["summe"], -total_cost, state["cash"],
              f"Spread {c['spread']*100:.2f}% nach geschaetzten Gebuehren, Edge-Limit {c['shares_cap']:.0f} Anteile, "
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


def _sxbet_leg_mark_and_result(leg):
    book = sxbet.fetch_market_by_hash(leg["market_id"])
    prices = sxbet._best_taker_prices(book) if book else None
    current_price = None
    if prices is not None:
        yes_price, no_price, _ = prices
        current_price = yes_price if leg["side"] == "yes" else no_price

    meta = sxbet.fetch_market_meta(leg["market_id"])
    if meta is None:
        return current_price, None  # Netzwerkfehler/transient - abwarten, nicht erzwingen
    outcome = meta.get("outcome")
    if outcome is None:
        return current_price, None  # noch nicht abgewickelt
    if outcome == 0:
        return current_price, leg["einstandskurs"]  # void/Unentschieden -> Einsatz zurück, kein Gewinn/Verlust
    # outcome 1 = outcomeOne gewinnt ("yes"-Seite), outcome 2 = outcomeTwo gewinnt ("no"-Seite)
    yes_won = outcome == 1
    payout = 1.0 if (yes_won and leg["side"] == "yes") or (not yes_won and leg["side"] == "no") else 0.0
    return current_price, payout


_LEG_HANDLERS = {
    "polymarket": _poly_leg_mark_and_result,
    "kalshi": _kalshi_leg_mark_and_result,
    "predictit": _predictit_leg_mark_and_result,
    "sxbet": _sxbet_leg_mark_and_result,
}


def _settle_leg(leg, payout):
    """Zahlt ein einzelnes Bein aus, inkl. plattformspezifischer Gebühr auf
    den Nettogewinn (aktuell nur bei PredictIt). Gibt die Netto-Proceeds zurück."""
    shares_proceeds = payout  # pro Anteil - wird vom Aufrufer mit shares multipliziert
    if leg["platform"] == "predictit":
        fee = predictit.FEE_RATE_PROFIT * max(0.0, payout - leg["einstandskurs"])
        return shares_proceeds - fee
    return shares_proceeds


def _mark_and_maybe_settle(pos, now):
    """Holt für jedes Bein einen gezielten Einzel-Lookup (anders als bei den
    Polymarket-only-Fonds gibt es hier keinen günstigeren "aus dem Bulk-Scan
    markieren"-Pfad, da die drei Plattform-Bulk-Listen keine stabile ID-Map
    wie Polymarket liefern/benötigen - bei max. ~15 offenen Positionen x 2
    Beinen je Fonds ist das vertretbar). Gibt (proceeds_per_share oder None,
    results-dict) zurück - proceeds_per_share ist nur gesetzt, wenn BEIDE
    Beine aufgelöst sind."""
    results = {}
    for leg_key in ("leg_a", "leg_b"):
        leg = pos[leg_key]
        handler = _LEG_HANDLERS[leg["platform"]]
        current_price, payout = handler(leg)
        if current_price is not None:
            leg["letzter_kurs"] = current_price
        results[leg_key] = payout

    if results["leg_a"] is None or results["leg_b"] is None:
        return None, results  # mindestens ein Bein noch offen - warten, nichts erzwingen

    proceeds_per_share = (
        _settle_leg(pos["leg_a"], results["leg_a"]) + _settle_leg(pos["leg_b"], results["leg_b"])
    )
    return proceeds_per_share, results


def _mark_and_settle_all(profile, state, now):
    still_open = []
    for pos in state["positions"]:
        proceeds_per_share, results = _mark_and_maybe_settle(pos, now)
        if proceeds_per_share is None:
            still_open.append(pos)
            continue
        proceeds = pos["shares"] * proceeds_per_share
        state["cash"] += proceeds
        log_trade(profile, now.isoformat(), "AUSZAHLUNG", pos["pair_id"], pos["frage"], pos["shares"],
                  proceeds_per_share, proceeds, state["cash"],
                  f"Beide Beine aufgelöst (Payout A={results['leg_a']:.2f}, B={results['leg_b']:.2f})")
    state["positions"] = still_open


def run_fund_step(profile, all_matches, now):
    """Wird vom Multi-Platform-Scan aufgerufen: bewertet offene Positionen
    anhand der schon gefundenen Matches/Marktdaten UND kauft neue."""
    state = load_state(profile)
    if state["started"] is None:
        state["started"] = now.isoformat()

    _mark_and_settle_all(profile, state, now)
    _select_new_trades(profile, state, all_matches, now)

    save_state(profile, state)
    nav = log_history(profile, now, state["cash"], state["positions"])
    print(f"Fonds [{profile['label']}]: NAV {nav:.2f} USD "
          f"({(nav/profile['starting_capital']-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def run_all_fund_steps(all_matches, now):
    for profile in PROFILES.values():
        run_fund_step(profile, all_matches, now)


def watch_step(profile, now):
    """Hochfrequenter Watcher: prüft nur offene Positionen per Einzel-Lookup,
    kauft nichts Neues."""
    state = load_state(profile)
    if not state["positions"]:
        print(f"Fonds-Watch [{profile['label']}]: keine offenen Positionen, nichts zu tun.")
        return

    _mark_and_settle_all(profile, state, now)

    save_state(profile, state)
    nav = log_history(profile, now, state["cash"], state["positions"])
    print(f"Fonds-Watch [{profile['label']}]: NAV {nav:.2f} USD "
          f"({(nav/profile['starting_capital']-1)*100:+.2f}%), "
          f"{len(state['positions'])} offene Position(en), Kasse {state['cash']:.2f} USD.")


def watch_all_positions_step(now):
    for profile in PROFILES.values():
        watch_step(profile, now)
