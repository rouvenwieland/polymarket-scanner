"""Cross-Platform-Fonds nach der Reparatur vom 27.10.2026: Strategie-Pruefung (arbitrage_check), Groesse nach
Orderbuch-Tiefe, garantierter Gewinn in jedem Ausgang, Abrechnung aller Beine, Bewertung zum Anschaffungswert."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

import arbitrage_check as ac
import cross_platform
import cross_platform_fund as cpf
import llm_verify
import pricing
from platforms import polymarket

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
END = (NOW + timedelta(days=5)).isoformat()


def mk(platform, mid, yes, no, liq=0.0, q="Will X happen?", fee_rate=0.0, **extra):
    """Kalshi/PredictIt/SX liefern Asks, Polymarkt hier mit Orderbuch: ausfuehrbar. Polymarkt-Gebuehr standardmaessig 0,
    damit die Rechnungen in den Tests ueberschaubar bleiben (eigene Tests pruefen die Gebuehr)."""
    m = {"platform": platform, "market_id": mid, "question": q, "extra_text": "",
         "yes_price": yes, "no_price": no, "volume": liq, "liquidity": liq, "end_date": END, "url": "",
         "price_basis": "ausfuehrbar", "yes_label": None, "no_label": None, "is_sport": False,
         "sport_market_type": "", "start_time": None}
    if platform == "polymarket":
        m["fee_rate"] = fee_rate
    m.update(extra)
    return m


@pytest.fixture
def buy(monkeypatch, datadir):
    """Fuehrt _select_new_trades fuer genau ein (als echt verifiziertes) Paar aus und liefert den State.
    Orderbuch-Tiefe: standardmaessig unbekannt (None) -> Not-Deckel je Bein."""
    monkeypatch.setattr(cpf.llm_verify, "verify_candidates",
                        lambda cands: {llm_verify._pair_key(a, b): True for a, b in cands})

    def run(ma, mb, cash=1000.0, depth=None):
        profile = cpf.PROFILES["cross_platform_1k"]
        state = cpf._empty_state(profile)
        state["cash"] = cash
        fn = (lambda m, side: depth.get((m["market_id"], side))) if depth else (lambda m, side: None)
        cpf._select_new_trades(profile, state, [{"market_a": ma, "market_b": mb, "score": 1.0}], NOW, depth_fn=fn)
        return state
    return run


# ------------------------------------------------------------------ Lamar vs. Southeastern Louisiana (17.10.2026)
def lamar_pair():
    """Die echten Daten des Vorfalls: Polymarkt 5353156 (Ausgaenge Lamar, Southeastern Louisiana) und SX Bet
    "Southeastern Louisiana vs Lamar" (outcomeOne = Southeastern Louisiana). Kurse wie beim Kauf im 1k-Fonds."""
    start = "2026-10-17T21:00:00+00:00"
    poly = mk("polymarket", "5353156", yes=0.62, no=0.381, q="Lamar vs. Southeastern Louisiana", fee_rate=0.03,
              yes_label="Lamar", no_label="Southeastern Louisiana", is_sport=True, sport_market_type="moneyline",
              start_time=start, end_date="2026-10-17T21:00:00Z")
    sx = mk("sxbet", "0x14d7", yes=0.424, no=0.59, q="Southeastern Louisiana vs Lamar - wer gewinnt?",
            yes_label="Southeastern Louisiana", no_label="Lamar", is_sport=True, sport_market_type="moneyline",
            start_time=start, end_date=start)
    return poly, sx


def test_lamar_teamreihenfolge_wird_erkannt_und_nicht_mehr_doppelt_gesetzt(buy):
    poly, sx = lamar_pair()
    assert cross_platform.outcome_alignment(poly, sx) == "flipped"
    side_a, side_b, summe = cross_platform.best_arbitrage_combo(poly, sx, "flipped")
    # Absicherung heisst: ein Bein je Team. Frueher: Polymarkt "no" (Southeastern Louisiana) + SX "yes" (dasselbe Team).
    labels = {poly[f"{side_a}_label"], sx[f"{side_b}_label"]}
    assert labels == {"Lamar", "Southeastern Louisiana"}
    # Gebucht wurde "23 % Spread" (Polymarkt-Nein 0,381 + SX-Ja 0,424). Richtig zugeordnet bleiben knapp 3 %, nach
    # Polymarkt-Gebuehr und SX-Gewinngebuehr unter dem Mindestgewinn: keine Arbitrage, kein Kauf.
    assert 1.0 - summe < 0.03
    chk = ac.check_pair(poly, sx, NOW)
    assert not chk.ok and any("garantierter Gewinn" in r for r in chk.reasons)
    assert buy(poly, sx)["positions"] == []


def test_unklare_zuordnung_kein_kauf(buy):
    a = mk("polymarket", "P", 0.40, 0.62, q="Lamar vs. Southeastern Louisiana", yes_label="Lamar",
           no_label="Southeastern Louisiana")
    b = mk("kalshi", "K", 0.45, 0.56, q="Lamar wins")  # Ja/Nein auf ein Team, kein Gegenteam
    assert cross_platform.outcome_alignment(a, b) is None
    assert buy(a, b)["positions"] == []
    with pytest.raises(ValueError):
        cross_platform.best_arbitrage_combo(a, b, None)


@pytest.mark.parametrize("ya,na,yb,nb,expected", [
    ("Atlanta Dream", "Golden State Valkyries", "Atlanta Dream", "Golden State Valkyries", "same"),
    ("Atlanta Dream", "Golden State Valkyries", "Golden State Valkyries", "Atlanta Dream", "flipped"),
    # Frauen-Kennung nur auf einer Seite: anderes Team (Herren "Duke" gegen Damen "Duke W") -> lieber unklar
    ("Duke", "North Carolina", "Duke W", "North Carolina W", None),
    ("Atlanta Dream", "Golden State Valkyries", "Atlanta Dream W", "Golden State Valkyries W", None),
    ("Tennessee-Martin", "Western Illinois", "Western Illinois", "UT Martin", "flipped"),
    ("Georgia", "Georgia State", "Georgia State", "Georgia", None),      # gemeinsamer Namensteil: nicht eindeutig
    ("Lamar", "McNeese", "Lamar", "Nicholls", None),                     # anderes Spiel
])
def test_seitenzuordnung_ueber_teamnamen(ya, na, yb, nb, expected):
    a = mk("polymarket", "P", 0.5, 0.5, yes_label=ya, no_label=na)
    b = mk("sxbet", "S", 0.5, 0.5, yes_label=yb, no_label=nb)
    assert cross_platform.outcome_alignment(a, b) == expected
    assert cross_platform.outcome_alignment(b, a) == expected  # symmetrisch


team = st.sampled_from(["Lamar", "Southeastern Louisiana", "Abilene Christian", "North Alabama", "Incarnate Word",
                        "Atlanta Dream", "Golden State Valkyries", "Delaware State", "Eastern Kentucky"])


@settings(max_examples=300, deadline=None)
@given(t1=team, t2=team, flip_a=st.booleans(), flip_b=st.booleans(),
       prices=st.tuples(*[st.floats(0.01, 0.99)] * 4), winner_first=st.booleans())
def test_eigenschaft_gewaehlte_beine_decken_genau_einen_sieger_ab(t1, t2, flip_a, flip_b, prices, winner_first):
    """Fuer jede Teamreihenfolge auf beiden Plattformen: ist die Zuordnung bestimmt, zahlt bei JEDEM Sieger genau ein
    Bein. Nie wieder zwei Beine auf dasselbe Team."""
    assume(not cross_platform.labels_match(t1, t2))
    la = (t2, t1) if flip_a else (t1, t2)
    lb = (t2, t1) if flip_b else (t1, t2)
    a = mk("polymarket", "P", prices[0], prices[1], yes_label=la[0], no_label=la[1])
    b = mk("sxbet", "S", prices[2], prices[3], yes_label=lb[0], no_label=lb[1])
    alignment = cross_platform.outcome_alignment(a, b)
    assert alignment == ("same" if flip_a == flip_b else "flipped")
    side_a, side_b, _ = cross_platform.best_arbitrage_combo(a, b, alignment)
    winner = t1 if winner_first else t2
    paying = [a[f"{side_a}_label"] == winner, b[f"{side_b}_label"] == winner]
    assert sum(paying) == 1


# ------------------------------------------------------------------ Regeln, Gebuehren, Huerden
def test_kursunterschied_ueber_zehn_prozent_wird_nicht_gekauft(buy):
    ma = mk("polymarket", "A", 0.30, 0.72)
    mb = mk("kalshi", "B", 0.74, 0.58)  # Yes_A + No_B = 0,88 -> 12 %: fast immer eine Fehlpaarung
    chk = ac.check_pair(ma, mb, NOW)
    assert not chk.ok and any("Kursunterschied" in r for r in chk.reasons)
    assert buy(ma, mb)["positions"] == []


def test_polymarkt_gebuehr_wird_eingerechnet():
    ma = mk("polymarket", "A", 0.46, 0.56, fee_rate=0.0)
    mb = mk("predictit", "B", 0.53, 0.50)  # Yes_A + No_B = 0,96
    assert not ac.check_pair(ma, mb, NOW).ok  # PredictIt-Gewinngebuehr 0,05 frisst den Vorsprung schon allein
    ma2 = mk("polymarket", "A", 0.90, 0.12, fee_rate=0.0)
    mb2 = mk("sxbet", "B", 0.96, 0.05)  # Yes_A + No_B = 0,95, garantiert 0,05 - 0,02*0,95 = 0,031
    assert ac.check_pair(ma2, mb2, NOW).ok
    ma2["fee_rate"] = 0.07  # 0,07 * 0,9 * 0,1 = 0,0063 -> unter 3 Cent
    assert not ac.check_pair(ma2, mb2, NOW).ok


def test_polymarkt_taker_gebuehr_formel():
    # docs.polymarket.com/trading/fees: 100 Anteile zu 0,50, Satz 0,05 -> 1,25 USD
    assert polymarket.taker_fee(100, 0.5, 0.05) == pytest.approx(1.25)
    assert polymarket.taker_fee(100, 0.3, 0.07) == pytest.approx(1.47)
    assert polymarket.fee_rate({"feesEnabled": False}) == 0.0
    # Markt meldet 0,03 (Lamar), Doku nennt fuer Sport 0,05: es gilt vorsichtig der hoehere Wert
    assert polymarket.fee_rate({"feesEnabled": True, "feeSchedule": {"rate": 0.03, "exponent": 1}}) == 0.05
    assert polymarket.fee_rate({"feesEnabled": True, "feeSchedule": {"rate": 0.07}}) == 0.07
    assert polymarket.fee_rate({}) == polymarket.UNKNOWN_FEE_RATE


def test_kapitalbindung_bis_2028_lohnt_drei_prozent_nicht():
    ma = mk("polymarket", "A", 0.139, 0.87, end_date="2028-08-31T00:00:00Z",
            q="Will Jon Ossoff win the 2028 Democratic presidential nomination?")
    mb = mk("predictit", "B", 0.20, 0.81, end_date=None,
            q="Who will win the 2028 Democratic presidential nomination? - Jon Ossoff")
    chk = ac.check_pair(ma, mb, NOW)
    assert chk.alignment == "same" and chk.edge > 0
    assert not chk.ok and any("p.a." in r for r in chk.reasons)


@pytest.mark.parametrize("qa,qb,grund", [
    ("Will Canada become a European Union Association Member by December 31, 2027? (copy)",
     "Will Canada become an EU Associate Member by year-end 2027?", "Kopie"),
    ("Will Gavin Newsom be the first listed Democrat to announce a run for President in 2028?",
     "Which of these Democratic candidates will announce a 2028 POTUS run first? - Gavin Newsom", "Definition"),
    ("Will Maine be the closest Senate race in 2026?", "Which 2026 Senate race will be the closest? - Maine", "Definition"),
    ("Will PSOE win the most seats in the 2026 Spanish general election?",
     "Who will win the 2026 Spain snap election? - PSOE", "Definition"),
])
def test_unterschiedliche_aufloesungsregeln_werden_ausgeschlossen(qa, qb, grund):
    ma = mk("polymarket", "A", 0.10, 0.91, q=qa)
    mb = mk("predictit", "B", 0.10, 0.84, q=qb)
    chk = ac.check_pair(ma, mb, NOW)
    assert not chk.ok and any(grund in r for r in chk.reasons), chk.reasons


def _sport(platform, mid, yes, no, yl, nl, start="2026-10-20T00:00:00+00:00", typ="moneyline"):
    return mk(platform, mid, yes, no, q=f"{yl} vs. {nl}", yes_label=yl, no_label=nl, is_sport=True,
              sport_market_type=typ, start_time=start, end_date=start)


def test_sport_abbruch_regeln_werden_eingerechnet():
    """Absage: Polymarkt zahlt 0,5 je Anteil, SX Bet erstattet den Einsatz. Kostet das Polymarkt-Bein mehr als 0,5,
    verliert das Paar bei einer Absage - kein garantierter Gewinn, kein Kauf."""
    a = _sport("polymarket", "P", 0.62, 0.40, "Alpha", "Bravo")
    b = _sport("sxbet", "S", 0.70, 0.30, "Alpha", "Bravo")  # Yes_A + No_B = 0,92
    chk = ac.check_pair(a, b, NOW)
    assert not chk.ok and any("garantierter Gewinn" in r for r in chk.reasons)
    worlds = ac.world_values("polymarket", "sxbet", 0.62, 0.30, True)
    assert worlds["Absage"] == pytest.approx(0.5 + 0.30)
    # Gemischter Fall (adversariale Pruefung): Polymarkt wertet das verlegte Spiel am Nachholtermin, SX Bet erklaert es
    # nichtig. Dann verliert selbst ein Paar mit 5 % Vorsprung - die Abbruch-Regeln sind nicht gleich, also keine Arbitrage.
    c = _sport("polymarket", "P", 0.40, 0.62, "Alpha", "Bravo")
    d = _sport("sxbet", "S", 0.47, 0.55, "Alpha", "Bravo")  # Yes_A + No_B = 0,95
    chk = ac.check_pair(c, d, NOW)
    assert not chk.ok
    worlds = ac.world_values("polymarket", "sxbet", 0.40, 0.55, True)
    assert worlds["B abgesagt, A verliert"] == pytest.approx(0.55)


def test_sport_anderes_spiel_oder_kalshi_wird_ausgeschlossen():
    a = _sport("polymarket", "P", 0.40, 0.62, "Alpha", "Bravo")
    b = _sport("sxbet", "S", 0.47, 0.55, "Alpha", "Bravo", start="2026-10-22T00:00:00+00:00")  # Spiel 2 der Serie
    assert any("Anstosszeiten" in r for r in ac.check_pair(a, b, NOW).reasons)
    k = mk("kalshi", "KXNHLGAME-1", 0.47, 0.55, is_sport=True, start_time="2026-10-20T00:00:00+00:00")
    assert not ac.check_pair(a, k, NOW).ok
    no_draw = _sport("sxbet", "S", 0.47, 0.55, "Alpha", "Bravo", typ="winner_no_draw")
    assert any("Markttyp" in r for r in ac.check_pair(a, no_draw, NOW).reasons)


def test_schaetzkurs_wird_nie_gekauft_auch_ohne_schalter(buy, monkeypatch):
    monkeypatch.setattr(pricing, "REQUIRE_EXECUTABLE", False)
    ma = mk("polymarket", "A", 0.20, 0.82, price_basis=pricing.BASIS_ESTIMATE)
    mb = mk("sxbet", "B", 0.30, 0.72)
    assert buy(ma, mb)["positions"] == []


# ------------------------------------------------------------------ Groesse: Orderbuch-Tiefe, Deckel, Kasse
def test_groesse_folgt_der_orderbuchtiefe_und_stoppt_an_unprofitablen_stufen(buy):
    ma = mk("polymarket", "A", 0.20, 0.82)
    mb = mk("sxbet", "B", 0.30, 0.72)  # Yes_A + No_B = 0,92
    depth = {("A", "yes"): [(0.20, 100.0), (0.25, 1000.0)],   # zweite Stufe: 0,25 + 0,72 = 0,97 -> zu wenig Vorsprung
             ("B", "no"): [(0.72, 1000.0)]}
    state = buy(ma, mb, cash=100000.0, depth=depth)
    pos = state["positions"][0]
    assert pos["shares"] == pytest.approx(100.0)
    assert pos["leg_a"]["einstandskurs"] == pytest.approx(0.20)


def test_hoechstens_zehn_prozent_des_fonds_je_paar(buy):
    ma = mk("polymarket", "A", 0.20, 0.82)
    mb = mk("sxbet", "B", 0.30, 0.72)
    depth = {("A", "yes"): [(0.20, 1e6)], ("B", "no"): [(0.72, 1e6)]}
    state = buy(ma, mb, cash=1000.0, depth=depth)
    assert cpf.book_value(state["positions"][0]) <= 0.10 * 1000.0 + 1e-6


def test_unbekannte_tiefe_hoechstens_zehn_usd_je_bein(buy):
    ma = mk("polymarket", "A", 0.20, 0.82)
    mb = mk("predictit", "B", 0.30, 0.72)
    state = buy(ma, mb, cash=1000.0)
    pos = state["positions"][0]
    assert pos["shares"] * pos["leg_b"]["einstandskurs"] <= ac.UNKNOWN_DEPTH_LEG_USD + 1e-6


price = st.floats(0.01, 0.99, allow_nan=False)
levels = st.lists(st.tuples(st.floats(0.01, 0.99), st.floats(1.0, 5000.0)), min_size=1, max_size=4)
plat = st.sampled_from(["polymarket", "kalshi", "predictit", "sxbet"])


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ya=price, na=price, yb=price, nb=price, pa=plat, pb=plat, fee=st.sampled_from([0.0, 0.03, 0.07]),
       sport=st.booleans(), la=st.one_of(st.none(), levels), lb=st.one_of(st.none(), levels),
       cash=st.floats(5.0, 50000.0))
def test_eigenschaft_jeder_kauf_gewinnt_in_jedem_ausgang(ya, na, yb, nb, pa, pb, fee, sport, la, lb, cash, buy):
    """Kern der Strategie: was gekauft wird, bringt in JEDEM Ausgang (A gewinnt, B gewinnt, bei Sport: Absage) nach
    allen Gebuehren mindestens MIN_EDGE je Anteilspaar - mit den Abrechnungsfunktionen des Fonds nachgerechnet."""
    assume(pa != pb)
    extra = dict(is_sport=True, sport_market_type="moneyline", start_time="2026-10-20T00:00:00+00:00") if sport else {}
    ma = mk(pa, "A", ya, na, fee_rate=fee, **extra)
    mb = mk(pb, "B", yb, nb, fee_rate=fee, **extra)
    depth = {}
    if la:
        depth[("A", "yes")] = depth[("A", "no")] = sorted(la)
    if lb:
        depth[("B", "yes")] = depth[("B", "no")] = sorted(lb)
    state = buy(ma, mb, cash=cash, depth=depth or None)
    assert state["cash"] >= -1e-9
    for pos in state["positions"]:
        s, paid = pos["shares"], cash - state["cash"]
        legs = (pos["leg_a"], pos["leg_b"])
        outcomes = [(1.0, 0.0), (0.0, 1.0)]
        if sport:
            outcomes.append(tuple(ac._cancel_payout(leg["platform"], leg["einstandskurs"]) for leg in legs))
        for pay in outcomes:
            proceeds = s * sum(cpf._settle_leg(leg, p) for leg, p in zip(legs, pay))
            assert proceeds - paid >= ac.MIN_EDGE * s - 1e-6, (pay, proceeds, paid)


# ------------------------------------------------------------------ Abrechnung
def test_preis_null_stuerzt_nicht_ab(buy):
    for plat_a, plat_b in (("polymarket", "kalshi"), ("polymarket", "predictit"), ("kalshi", "predictit")):
        ma = mk(plat_a, "A", 0.0, 1.0)
        mb = mk(plat_b, "B", 0.97, 0.01)
        assert buy(ma, mb)["positions"] == []


def test_predictit_gewinngebuehr_wird_bei_der_auswahl_beruecksichtigt(buy):
    """Summe 0,965 -> 3,5 Cent 'Spread', aber gewinnt das PredictIt-Bein (0,35 Einsatz), kostet die
    10-%-Gewinngebuehr 6,5 Cent -> Verlust. Solche Paare duerfen nicht gekauft werden."""
    ma = mk("polymarket", "A", 0.615, 0.385)
    mb = mk("predictit", "B", 0.60, 0.35)
    assert buy(ma, mb)["positions"] == []


def test_predictit_gewinngebuehr_profitables_paar_wird_gekauft(buy):
    ma = mk("polymarket", "A", 0.20, 0.82)
    mb = mk("predictit", "B", 0.30, 0.72)  # Summe 0,92; PredictIt gewinnt: 1 - 0,028 - 0,92 = +0,052
    assert len(buy(ma, mb)["positions"]) == 1


def _poly_leg(monkeypatch, outcome_prices, closed=True, outcomes='["Yes", "No"]', **extra):
    m = {"id": "P1", "closed": closed, "outcomes": outcomes, "outcomePrices": json.dumps(outcome_prices)}
    m.update(extra)
    monkeypatch.setattr(cpf.fund_simulator, "fetch_market_by_id", lambda mid: m)
    return lambda side: cpf._poly_leg_mark_and_result({"market_id": "P1", "side": side})


def test_polymarkt_bein_mit_teamnamen_wird_abgerechnet(monkeypatch):
    """Lamar-Markt (echte Gamma-Daten): Ausgaenge ["Lamar", "Southeastern Louisiana"], Endkurse ["0", "1"]. Frueher
    verlangte parse_outcome_prices "Yes"/"No" und lieferte None - das Bein wurde nie abgerechnet."""
    f = _poly_leg(monkeypatch, ["0", "1"], outcomes='["Lamar", "Southeastern Louisiana"]', umaResolutionStatus="resolved")
    assert f("yes")[1] == 0.0      # Lamar verlor
    assert f("no")[1] == 1.0       # Southeastern Louisiana gewann 34:31
    assert pricing.parse_outcome_prices({"outcomes": '["Lamar", "Southeastern Louisiana"]',
                                         "outcomePrices": '["0", "1"]'}) == (0.0, 1.0)


def test_polymarkt_umgekehrte_ja_nein_reihenfolge_wird_nie_ausfuehrbar_gehandelt():
    """["No", "Yes"]: Endkurse nach Namen zuordnen (Abrechnung), das Orderbuch des ersten Tokens aber NICHT als
    Ja-Preis nehmen - es gehoert zur Nein-Seite. Ergebnis: nur Schaetzkurs, nie handelbar."""
    m = {"outcomes": '["No", "Yes"]', "outcomePrices": '["0.3", "0.7"]', "bestBid": 0.29, "bestAsk": 0.31}
    assert pricing.parse_outcome_prices(m) == (0.7, 0.3)
    assert pricing.buy_quotes(m).basis == pricing.BASIS_ESTIMATE
    assert pricing.outcome_labels(m) == (None, None)


def test_polymarkt_bein_im_uma_streit_wartet(monkeypatch):
    f = _poly_leg(monkeypatch, ["1", "0"], umaResolutionStatus="disputed")
    assert f("yes")[1] is None


def test_polymarkt_bein_50_50_aufloesung_zahlt_die_haelfte(monkeypatch):
    f = _poly_leg(monkeypatch, ["0.5", "0.5"])
    assert f("yes")[1] == 0.5 and f("no")[1] == 0.5


def test_polymarkt_bein_geschlossen_aber_nicht_aufgeloest_wartet(monkeypatch):
    f = _poly_leg(monkeypatch, ["0.62", "0.38"])
    assert f("yes")[1] is None and f("no")[1] is None


def test_polymarkt_bein_aufgeloest(monkeypatch):
    f = _poly_leg(monkeypatch, ["1", "0"])
    assert f("yes")[1] == 1.0 and f("no")[1] == 0.0
    g = _poly_leg(monkeypatch, ["0.995", "0.005"], umaResolutionStatus="resolved")
    assert g("yes")[1] == 1.0 and g("no")[1] == 0.0
    u = _poly_leg(monkeypatch, ["0.505", "0.495"])  # geschlossen, ohne Status, nur Letztkurse: nicht als 50-50 buchen
    assert u("yes")[1] is None
    h = _poly_leg(monkeypatch, ["0.3", "0.7"], closed=False, bestBid=0.29, bestAsk=0.31)
    assert h("yes") == (pytest.approx(0.29), None)   # Geldkurs (sofort verkaufbar), nicht der Ask
    assert h("no") == (pytest.approx(0.69), None)


def test_predictit_geschlossen_ohne_endkurs_wartet(monkeypatch):
    for last, expected in ((0.62, None), (0.96, None), (0.99, 1.0), (0.01, 0.0)):
        contract = {"status": "Closed", "lastTradePrice": last, "bestSellYesCost": None}
        monkeypatch.setattr(cpf.predictit, "fetch_contract_by_market_id", lambda mid, c=contract: ({}, c))
        assert cpf._predictit_leg_mark_and_result({"market_id": "1:2", "side": "yes"})[1] == expected


def test_sxbet_gewinngebuehr_wird_bei_auszahlung_abgezogen():
    leg = {"platform": "sxbet", "einstandskurs": 0.40}
    assert cpf._settle_leg(leg, 1.0) == pytest.approx(1.0 - 0.02 * 0.60)
    assert cpf._settle_leg(leg, 0.40) == pytest.approx(0.40)  # Absage: Einsatz zurueck, kein Gewinn, keine Gebuehr


def test_nicht_komplementaere_auszahlung_wird_als_fehlpaarung_markiert(monkeypatch, datadir):
    """Zahlen beide Beine 1 (Lamar: beide auf Southeastern Louisiana), ist das kein Arbitrage-Erfolg."""
    profile = cpf.PROFILES["cross_platform_1k"]
    pos = {"pair_id": "x", "frage": "F", "shares": 10.0, "end_date": END, "preisbasis": "ausfuehrbar",
           "leg_a": {"platform": "kalshi", "market_id": "K", "side": "yes", "einstandskurs": 0.4},
           "leg_b": {"platform": "kalshi", "market_id": "K2", "side": "yes", "einstandskurs": 0.4}}
    monkeypatch.setattr(cpf.kalshi, "fetch_market_by_ticker", lambda t: {"status": "finalized", "result": "yes"})
    state = {"cash": 0.0, "positions": [pos], "started": "x"}
    cpf._mark_and_settle_all(profile, state, NOW)
    rows = (datadir / profile["trades_csv"]).read_text(encoding="utf-8")
    assert "WARNUNG: Beine nicht komplementaer" in rows


def _kalshi_leg(monkeypatch, market):
    monkeypatch.setattr(cpf.kalshi, "fetch_market_by_ticker", lambda ticker: market)
    return lambda side: cpf._kalshi_leg_mark_and_result({"market_id": "K1", "side": side})


def _kalshi_market(result, **extra):
    m = {"status": "finalized", "result": result, "yes_ask_dollars": "0.9900", "no_ask_dollars": "0.0200"}
    m.update(extra)
    return m


def test_kalshi_bein_scalar_zahlt_anteil_statt_null(monkeypatch):
    f = _kalshi_leg(monkeypatch, _kalshi_market("scalar", settlement_value_dollars="0.3060"))
    assert f("yes")[1] == pytest.approx(0.306)
    assert f("no")[1] == pytest.approx(0.694)


def test_kalshi_bein_scalar_ohne_abwicklungswert_wartet(monkeypatch):
    for extra in ({}, {"settlement_value_dollars": ""}, {"settlement_value_dollars": "abc"},
                  {"settlement_value_dollars": "1.4"}, {"settlement_value_dollars": "-0.1"}):
        f = _kalshi_leg(monkeypatch, _kalshi_market("scalar", **extra))
        assert f("yes")[1] is None and f("no")[1] is None, extra


def test_kalshi_bein_yes_no_ergebnis_bleibt_unveraendert(monkeypatch):
    f = _kalshi_leg(monkeypatch, _kalshi_market("yes"))
    assert f("yes")[1] == 1.0 and f("no")[1] == 0.0
    g = _kalshi_leg(monkeypatch, _kalshi_market("no"))
    assert g("yes")[1] == 0.0 and g("no")[1] == 1.0
    h = _kalshi_leg(monkeypatch, _kalshi_market("", status="active"))
    assert h("yes")[1] is None
    i = _kalshi_leg(monkeypatch, _kalshi_market("yes", status="closed"))
    assert i("yes")[1] is None


def test_kalshi_scalar_paar_zahlt_in_summe_ein_vollstaendiges_paar_aus(monkeypatch):
    market = _kalshi_market("scalar", settlement_value_dollars="0.25")
    monkeypatch.setattr(cpf.kalshi, "fetch_market_by_ticker", lambda ticker: market)
    leg = {"platform": "kalshi", "market_id": "K1", "einstandskurs": 0.5, "letzter_kurs": 0.5}
    pos = {"leg_a": dict(leg, side="yes"), "leg_b": dict(leg, side="no")}
    proceeds, results = cpf._mark_and_maybe_settle(pos, NOW)
    assert proceeds == pytest.approx(1.0)


def test_kalshi_bein_scalar_aelteres_cent_feld_als_rueckfall(monkeypatch):
    f = _kalshi_leg(monkeypatch, _kalshi_market("scalar", settlement_value=75))
    assert f("yes")[1] == pytest.approx(0.75) and f("no")[1] == pytest.approx(0.25)


# ------------------------------------------------------------------ Bewertung
def test_nav_zum_anschaffungswert_unabhaengig_von_kursausschlaegen(datadir):
    profile = cpf.PROFILES["cross_platform_1k"]
    pos = {"pair_id": "x", "shares": 100.0, "end_date": END, "einstand_gesamt": 92.0, "garantierter_endwert": 97.0,
           "leg_a": {"platform": "polymarket", "einstandskurs": 0.2, "letzter_kurs": 0.9, "geldkurs": 0.05},
           "leg_b": {"platform": "sxbet", "einstandskurs": 0.72, "letzter_kurs": 0.1, "geldkurs": 0.6}}
    nav = cpf.log_history(profile, NOW, 908.0, [pos])
    assert nav == pytest.approx(1000.0)
    row = (datadir / profile["history_csv"]).read_text(encoding="utf-8").splitlines()[-1]
    assert "1005.0" in row and "973.0" in row  # Terminierungswert 908 + 97, Liquidationswert 908 + 100*0,65


def test_fehlendes_enddatum_eines_beins_verlaengert_die_kapitalbindung():
    """Adversariale Pruefung: PredictIt ohne Enddatum darf die Kapitalkosten-Huerde nicht umgehen."""
    ma = mk("polymarket", "A", 0.20, 0.82, end_date=(NOW + timedelta(days=3)).isoformat())
    mb = mk("predictit", "B", 0.30, 0.72, end_date=None)
    assert ac.days_to_resolution(ma, mb, NOW) == pytest.approx(3.0 + ac.MISSING_END_DAYS)


def test_typografischer_apostroph_und_vermeiden_gelten_als_verneinung():
    assert cross_platform._opposite_polarity("will the senate pass it?", "will the senate won\u2019t pass it?")
    assert cross_platform._opposite_polarity("will the government shut down?", "will the government avoid a shutdown?")


def test_kalshi_void_wird_nicht_als_doppelter_verlust_gebucht(monkeypatch):
    f = _kalshi_leg(monkeypatch, _kalshi_market("void"))
    assert f("yes")[1] is None and f("no")[1] is None


def test_kalshi_frage_enthaelt_den_untertitel():
    from platforms import kalshi
    m = {"status": "active", "ticker": "KXBTC-1", "title": "Bitcoin price on Oct 28, 2026?", "yes_sub_title": "Above 82,000",
         "yes_ask_dollars": "0.4", "no_ask_dollars": "0.6"}
    assert kalshi.normalize(m)["question"] == "Bitcoin price on Oct 28, 2026? - Above 82,000"


def test_predictit_ergebnis_wird_nur_fuer_bestaetigte_paare_abgeleitet(monkeypatch):
    """PredictIt nimmt aufgeloeste Maerkte aus der API. Fuer von Hand bestaetigte Paare gilt das Ergebnis des
    Polymarkt-Beins, sonst bleibt die Position offen (und der Alarm meldet sie)."""
    m = {"id": "P1", "closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]',
         "umaResolutionStatus": "resolved"}
    monkeypatch.setattr(cpf.fund_simulator, "fetch_market_by_id", lambda mid: m)
    monkeypatch.setattr(cpf.predictit, "fetch_contract_by_market_id", lambda mid: (None, None))
    leg_a = {"platform": "polymarket", "market_id": "P1", "side": "yes", "einstandskurs": 0.2}
    leg_b = {"platform": "predictit", "market_id": "1:2", "side": "no", "einstandskurs": 0.72}
    pos = {"zuordnung": "same", "leg_a": dict(leg_a), "leg_b": dict(leg_b)}
    assert cpf._mark_and_maybe_settle(pos, NOW)[0] is None
    pos = {"zuordnung": "same", "predictit_ableiten": True, "leg_a": dict(leg_a), "leg_b": dict(leg_b)}
    proceeds, results = cpf._mark_and_maybe_settle(pos, NOW)
    assert results == {"leg_a": 1.0, "leg_b": 0.0} and proceeds == pytest.approx(1.0)
