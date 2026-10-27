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

REPARATUR 27.10.2026 (gilt vor allem anderen in dieser Doku): Jeder Kauf muss die Strategie-Pruefung in
arbitrage_check.py bestehen (Seitenzuordnung, vergleichbare Regeln, ausfuehrbare Preise, garantierter Gewinn
in jedem Ausgang nach allen Gebuehren inkl. Polymarkt-Taker-Gebuehr, Kapitalkosten-Huerde). Die Groesse folgt
der echten Orderbuch-Tiefe (nicht mehr dem linearen Impact-Modell), hoechstens 10 % des Fondswerts je Paar.
Bewertet wird zum Anschaffungswert; Geldkurs-Liquidationswert und garantierter Endwert stehen daneben.

Gebühren werden GENAU dort verrechnet, wo sie real anfallen:
  - Kalshi: Taker-Fee beim Kauf (ceil(0.07*C*P*(1-P)), siehe platforms/kalshi.py).
  - PredictIt: 10% Gebühr auf den Nettogewinn JE BEIN, verrechnet bei
    Auflösung dieses Beins (nicht vorher, da der Gewinn erst dann feststeht).
  - Polymarket: Taker-Gebuehr je Markt (feeSchedule, siehe platforms/polymarket.py; frueher
    faelschlich als gebuehrenfrei gerechnet).
Vereinfachung: die Kalshi-Fee wird nur beim Einstieg verrechnet (nicht bei
vorzeitigem Verkauf/Settlement). Die PredictIt-Auszahlungsgebühr (5%,
Konto-Ebene) wird bewusst NICHT pro Trade verrechnet (siehe platforms/predictit.py).
"""

import json
import os

import arbitrage_check as ac
import cross_platform
import fund_simulator
import llm_verify
import pricing
from platforms import kalshi, polymarket, predictit, sxbet

# Strategie-Parameter stehen in arbitrage_check.py (eine Quelle fuer Live-Fonds, Neuberechnung und Tests).
MIN_SPREAD = ac.MIN_EDGE
MAX_PLAUSIBLE_SPREAD = ac.MAX_PLAUSIBLE_SPREAD
MAX_OPEN_POSITIONS = 15
MATCH_THRESHOLD = cross_platform.SIMILARITY_THRESHOLD
MATCH_MAX_DAYS = cross_platform.MAX_DAYS_APART
STRATEGY_VERSION = "v2-2026-10-27"

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


def log_trade(profile, timestamp, action, pair_id, frage, shares, price, amount, cash_after, note="",
              basis=pricing.BASIS_ESTIMATE):
    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, profile["trades_csv"]),
        ["timestamp", "aktion", "pair_id", "frage", "anteile", "preis", "betrag", "kasse_danach", "notiz", "preisbasis"],
        {
            "preisbasis": basis,
            "timestamp": timestamp, "aktion": action, "pair_id": pair_id, "frage": frage,
            "anteile": round(shares, 4), "preis": round(price, 4), "betrag": round(amount, 4),
            "kasse_danach": round(cash_after, 4), "notiz": note,
        },
    )


# ---------------------------------------------------------------------
# Bewertung: Anschaffungswert, Liquidationswert, garantierter Endwert
# ---------------------------------------------------------------------
# Frueher: NAV = Kasse + Anteile x (letzter ASK beider Beine). Das ist weder ein erzielbarer Verkaufspreis (der
# waere der Geldkurs) noch der Endwert, und bei einem nicht abrechenbaren Bein (Lamar) eingefroren. Jetzt:
#   nav               = Kasse + Anschaffungswert offener Paare (inkl. Kaufgebuehren). Gewinne zaehlen erst bei
#                       Aufloesung - bei echter Arbitrage steht der Endwert ohnehin fest.
#   liquidationswert  = Kasse + Anteile x Geldkurse (sofort verkaufbar), soweit bekannt, sonst Anschaffungswert.
#   terminierungswert = Kasse + garantierter Endwert (schlechtester Ausgang nach Gebuehren).

def book_value(p):
    if p.get("einstand_gesamt") is not None:
        return float(p["einstand_gesamt"])
    a, b = p["leg_a"], p["leg_b"]
    return p["shares"] * (a["einstandskurs"] + b["einstandskurs"]) + a.get("fee_gezahlt", 0.0) + b.get("fee_gezahlt", 0.0)


def liquidation_value(p):
    a, b = p["leg_a"].get("geldkurs"), p["leg_b"].get("geldkurs")
    if a is None or b is None:
        return book_value(p)
    return p["shares"] * (a + b)


def guaranteed_value(p):
    if p.get("garantierter_endwert") is not None:
        return float(p["garantierter_endwert"])
    return p["shares"] * 1.0  # Altbestand ohne Pruefung: Kernannahme 1 USD je Paar (nicht garantiert!)


def log_history(profile, now, cash, positions):
    positions_value = sum(book_value(p) for p in positions)
    nav = cash + positions_value
    termination_value = cash + sum(guaranteed_value(p) for p in positions)
    days_list = [fund_simulator._days_to_end(p.get("end_date"), now) for p in positions]
    discounted_value = cash + sum(
        guaranteed_value(p) * fund_simulator._discount_factor(d) for p, d in zip(positions, days_list))
    starting_capital = profile["starting_capital"]
    n_est, v_est = pricing.estimate_split(positions, book_value)

    fund_simulator._append_csv(
        os.path.join(fund_simulator.DATA_DIR, profile["history_csv"]),
        ["timestamp", "kasse", "positionswert", "nav", "rendite_pct", "offene_positionen",
         "terminierungswert", "diskontierter_terminierungswert", "diskontierte_rendite_pct",
         "positionen_schaetzung", "positionswert_schaetzung", "liquidationswert", "bewertung"],
        {
            "positionen_schaetzung": n_est, "positionswert_schaetzung": round(v_est, 4),
            "timestamp": now.isoformat(),
            "kasse": round(cash, 4),
            "positionswert": round(positions_value, 4),
            "nav": round(nav, 4),
            "rendite_pct": round((nav / starting_capital - 1) * 100, 3),
            "offene_positionen": len(positions),
            "terminierungswert": round(termination_value, 4),
            "diskontierter_terminierungswert": round(discounted_value, 4),
            "diskontierte_rendite_pct": round((discounted_value / starting_capital - 1) * 100, 3),
            "liquidationswert": round(cash + sum(liquidation_value(p) for p in positions), 4),
            "bewertung": "anschaffungswert",
        },
    )
    return nav


# ---------------------------------------------------------------------
# Kauf neuer Positionen (nur nach bestandener Strategie-Pruefung)
# ---------------------------------------------------------------------

def leg_depth(market, side):
    """Kauf-Tiefe [(preis, anteile)] eines Beins oder None (unbekannt -> arbitrage_check.UNKNOWN_DEPTH_LEG_USD)."""
    platform = market["platform"]
    if platform == "sxbet":
        return market.get("depth_yes" if side == "yes" else "depth_no") or None
    if platform == "polymarket":
        tokens = market.get("token_ids") or []
        idx = 0 if side == "yes" else 1
        return polymarket.fetch_ask_levels(tokens[idx]) if len(tokens) == 2 else None
    if platform == "kalshi":
        return kalshi.fetch_ask_levels(market["market_id"], side)
    return None  # PredictIt: keine Tiefe in der oeffentlichen API


def _label(market, side):
    lab = market.get("yes_label") if side == "yes" else market.get("no_label")
    return lab or ("Ja" if side == "yes" else "Nein")


def _select_new_trades(profile, state, matches_by_platforms, now, depth_fn=None):
    depth_fn = depth_fn or leg_depth
    open_pair_ids = {p["pair_id"] for p in state["positions"]}
    candidates = []
    for match in matches_by_platforms:
        ma, mb = match["market_a"], match["market_b"]
        pair_id = f"{ma['platform']}:{ma['market_id']}__{mb['platform']}:{mb['market_id']}"
        if pair_id in open_pair_ids:
            continue
        open_pair_ids.add(pair_id)  # dasselbe Paar zweimal in einem Lauf: nur einmal kaufen
        chk = ac.check_pair(ma, mb, now)
        if not chk.ok:
            continue
        candidates.append({"pair_id": pair_id, "match": match, "chk": chk})

    if not candidates:
        return
    # Hoechste garantierte Jahresrendite zuerst (Kapital ist bis zur spaeteren Aufloesung gebunden).
    candidates.sort(key=lambda c: -c["chk"].annual)

    # Letzte Sicherheitsstufe (siehe llm_verify.py): FAIL-CLOSED, unverifizierte Kandidaten werden nicht gekauft.
    verdicts = llm_verify.verify_candidates(
        [(c["match"]["market_a"], c["match"]["market_b"]) for c in candidates]
    )
    candidates = [
        c for c in candidates
        if verdicts.get(llm_verify._pair_key(c["match"]["market_a"], c["match"]["market_b"])) is True
    ]
    free_slots = MAX_OPEN_POSITIONS - len(state["positions"])
    if not candidates or free_slots <= 0:
        return

    nav = state["cash"] + sum(book_value(p) for p in state["positions"])
    picks = candidates[:free_slots]
    per_slot_cash = state["cash"] / len(picks)
    for c in picks:
        if state["cash"] < fund_simulator.MIN_TRADE_NOTIONAL:
            break
        budget = min(state["cash"], per_slot_cash, ac.MAX_PAIR_FRACTION * nav)
        _execute_buy(profile, state, c, budget, now, depth_fn)


def _execute_buy(profile, state, c, budget, now, depth_fn=None):
    depth_fn = depth_fn or leg_depth
    ma, mb, chk = c["match"]["market_a"], c["match"]["market_b"], c["chk"]
    fill = ac.size_pair(ma, mb, chk, depth_fn(ma, chk.side_a), depth_fn(mb, chk.side_b), budget)
    for _ in range(5):  # Rundung der Gebuehren kann das Budget um Bruchteile eines Cents ueberschreiten
        if fill is None or fill.total <= min(budget, state["cash"]):
            break
        fill = ac.scale_fill(ma, mb, fill, 0.995 * min(budget, state["cash"]) / fill.total)
    if fill is None or fill.total > state["cash"] or fill.total < fund_simulator.MIN_TRADE_NOTIONAL:
        return False
    if not ac.fill_is_safe(fill):
        return False  # Endkontrolle mit gerundeten Gebuehren: kein garantierter Mindestgewinn -> kein Kauf

    state["cash"] -= fill.total
    pos = {
        "pair_id": c["pair_id"],
        "frage": f"[{ma['platform']}] {ma['question']}  <->  [{mb['platform']}] {mb['question']}",
        "shares": fill.shares,
        "end_date": _later_end(ma.get("end_date"), mb.get("end_date")),
        "eingestiegen": now.isoformat(),
        "preisbasis": pricing.BASIS_EXECUTABLE,
        "strategie": STRATEGY_VERSION,
        "zuordnung": chk.alignment,
        "einstand_gesamt": fill.total,
        "garantierter_endwert": fill.worst_value,
        "sport": bool(ma.get("is_sport") or mb.get("is_sport")),
        "leg_a": {
            "platform": ma["platform"], "market_id": ma["market_id"], "side": chk.side_a,
            "ausgang": _label(ma, chk.side_a), "end_date": ma.get("end_date"),
            "einstandskurs": fill.avg_a, "letzter_kurs": fill.avg_a, "fee_gezahlt": fill.fee_a,
        },
        "leg_b": {
            "platform": mb["platform"], "market_id": mb["market_id"], "side": chk.side_b,
            "ausgang": _label(mb, chk.side_b), "end_date": mb.get("end_date"),
            "einstandskurs": fill.avg_b, "letzter_kurs": fill.avg_b, "fee_gezahlt": fill.fee_b,
        },
    }
    state["positions"].append(pos)
    log_trade(profile, now.isoformat(), "KAUF", c["pair_id"], pos["frage"], fill.shares,
              fill.avg_a + fill.avg_b, -fill.total, state["cash"],
              f"Kursunterschied {chk.spread*100:.2f}%, garantiert {fill.worst_value - fill.total:+.2f} USD "
              f"({chk.annual*100:.1f}% p.a.), Zuordnung {chk.alignment}, Beine: {ma['platform']}/{chk.side_a} "
              f"({pos['leg_a']['ausgang']}) + {mb['platform']}/{chk.side_b} ({pos['leg_b']['ausgang']})",
              pricing.BASIS_EXECUTABLE)
    return True


def _later_end(a, b):
    da, db = ac._parse_time(a), ac._parse_time(b)
    if da is None:
        return b if db is not None else None
    if db is None:
        return a
    return a if da >= db else b


# ---------------------------------------------------------------------
# Bewertung / Auflösung offener Positionen
# ---------------------------------------------------------------------

def _poly_leg_mark_and_result(leg):
    """(Geldkurs oder None, Auszahlung je Anteil oder None solange offen)."""
    market = fund_simulator.fetch_market_by_id(leg["market_id"])
    if market is None:
        return None, None
    side_idx = 0 if leg["side"] == "yes" else 1
    bid = None
    sq = pricing.sell_quotes(market)
    if sq is not None and sq.basis == pricing.BASIS_EXECUTABLE:
        bid = sq.yes if side_idx == 0 else sq.no
    if not market.get("closed"):
        return bid, None
    # Ausgezahlt wird nur ein ENDKURS: 1, 0 oder 0,5 (50-50-Aufloesung bei Absage/Unklarheit). "closed" allein heisst
    # nur "Handel beendet"; Letztkurse wie 0,62/0,38 bedeuten, die Aufloesung steht noch aus -> weiter warten.
    # Ein laufender UMA-Streit ("disputed", "proposed" ohne Endkurs) ist ebenfalls noch keine Aufloesung.
    status = str(market.get("umaResolutionStatus") or "").lower()
    if status and status != "resolved":
        return bid, None
    prices = pricing.parse_outcome_prices(market)  # Endkurse je Ausgang; Sport: ["0","1"] fuer Teamnamen
    if prices is None:
        return bid, None
    # Mit bestaetigter Aufloesung ("resolved") genuegt ein Kurs nahe am Endwert; ohne Statusfeld nur exakte Endkurse
    # (sonst wuerden Letztkurse wie 0,505/0,495 als 50-50 oder 0,993 als 1 gebucht).
    tol = 0.01 if status == "resolved" else 0.001
    final_price = prices[side_idx]
    for final in (0.0, 0.5, 1.0):
        if abs(final_price - final) <= tol:
            return final, final
    return bid, None


def _kalshi_leg_mark_and_result(leg):
    market = kalshi.fetch_market_by_ticker(leg["market_id"])
    if market is None:
        return None, None
    bid_field = "yes_bid_dollars" if leg["side"] == "yes" else "no_bid_dollars"
    try:
        current_price = float(market.get(bid_field))
    except (TypeError, ValueError):
        current_price = None
    status, result = market.get("status"), market.get("result")
    if status not in ("finalized", "settled") or not result:
        return current_price, None
    if result == "scalar":
        # Kalshi-Doku (Market Settlement, Settlement Bounds): Bei "scalar" zahlt ein YES-Kontrakt den Abwicklungswert
        # (settlement_value_dollars, Anteil 0..1 des Nennwerts 1,00), ein NO-Kontrakt "1 minus Abwicklungswert".
        value = _kalshi_settlement_value(market)
        if value is None:
            return current_price, None
        return current_price, (value if leg["side"] == "yes" else 1.0 - value)
    if result not in ("yes", "no"):
        return current_price, None  # "void" o.ae.: nicht als Verlust beider Beine buchen, der Alarm meldet die Position
    return current_price, (1.0 if result == leg["side"] else 0.0)


def _kalshi_settlement_value(market):
    """Abwicklungswert der YES-Seite in USD (0..1) oder None, wenn nicht vorhanden/ungueltig.
    settlement_value_dollars ist das aktuelle Feld; das aeltere settlement_value (Cent) dient als Rueckfall."""
    raw = market.get("settlement_value_dollars")
    divisor = 1.0
    if raw in (None, ""):
        raw, divisor = market.get("settlement_value"), 100.0
    try:
        value = float(raw) / divisor
    except (TypeError, ValueError):
        return None
    if not (0.0 <= value <= 1.0):  # inkl. NaN
        return None
    return value


PREDICTIT_FINAL_TOLERANCE = 0.01


def _predictit_leg_mark_and_result(leg):
    _, contract = predictit.fetch_contract_by_market_id(leg["market_id"])
    if contract is None:
        return None, None
    sell_field = "bestSellYesCost" if leg["side"] == "yes" else "bestSellNoCost"
    try:
        current_price = float(contract.get(sell_field))
    except (TypeError, ValueError):
        current_price = None
    if contract.get("status") == "Open":
        return current_price, None
    # Frueher: "nicht Open" + Letztkurs >= 0,5 -> Ja gewinnt. Ein geschlossener, aber noch nicht aufgeloester Contract
    # (Handelsstopp vor dem Ergebnis) wurde damit nach dem Letztkurs entschieden. Jetzt nur bei eindeutigem Endkurs.
    try:
        last_price = float(contract.get("lastTradePrice"))
    except (TypeError, ValueError):
        return current_price, None
    if last_price >= 1.0 - PREDICTIT_FINAL_TOLERANCE:
        yes_won = True
    elif last_price <= PREDICTIT_FINAL_TOLERANCE:
        yes_won = False
    else:
        return current_price, None
    payout = 1.0 if (yes_won and leg["side"] == "yes") or (not yes_won and leg["side"] == "no") else 0.0
    return current_price, payout


def _sxbet_leg_mark_and_result(leg):
    book = sxbet.fetch_market_by_hash(leg["market_id"])
    prices = sxbet._best_taker_prices(book) if book else None
    current_price = None
    if prices is not None:
        yes_price, no_price, _ = prices
        # SX Bet kennt keinen Verkauf: eine Position wird durch eine Gegenwette glattgestellt.
        # Wert einer Ja-Position = 1 - Preis der Nein-Wette (und umgekehrt).
        current_price = 1.0 - (no_price if leg["side"] == "yes" else yes_price)

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
    """Netto-Auszahlung je Anteil eines Beins inkl. Gewinngebuehr (PredictIt 10 %, SX Bet 2 %)."""
    return ac.settle_value(leg["platform"], payout, leg["einstandskurs"])


def _mark_and_maybe_settle(pos, now):
    """Einzel-Lookup je Bein. Gibt (proceeds_per_share oder None, results-dict) zurueck - proceeds_per_share ist
    nur gesetzt, wenn BEIDE Beine aufgeloest sind."""
    results = {}
    for leg_key in ("leg_a", "leg_b"):
        leg = pos[leg_key]
        handler = _LEG_HANDLERS[leg["platform"]]
        current_price, payout = handler(leg)
        if current_price is not None:
            leg["geldkurs"] = current_price
            leg["letzter_kurs"] = current_price
        leg["geprueft"] = now.isoformat()
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
        note = f"Beide Beine aufgelöst (Payout A={results['leg_a']:.2f}, B={results['leg_b']:.2f})"
        if results["leg_a"] + results["leg_b"] > 1.0 + 1e-9 or results["leg_a"] + results["leg_b"] < 0.5 - 1e-9:
            # Bei echter Absicherung zahlt genau ein Bein (oder beide bei Absage teilweise). Beide 1 oder beide 0
            # heisst: die Beine waren NICHT komplementaer - Fehlpaarung, sichtbar machen statt als Erfolg verbuchen.
            note += " | WARNUNG: Beine nicht komplementaer (Fehlpaarung?)"
        log_trade(profile, now.isoformat(), "AUSZAHLUNG", pos["pair_id"], pos["frage"], pos["shares"],
                  proceeds_per_share, proceeds, state["cash"], note, pricing.basis_of(pos))
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
