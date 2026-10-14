"""Cross-Platform-Fonds: Kosten (Impact + Gebuehren), garantierte Verluste, Randpreise."""
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import cross_platform
import cross_platform_fund as cpf
import llm_verify
from platforms import kalshi, predictit

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
END = (NOW + timedelta(days=5)).isoformat()


def mk(platform, mid, yes, no, liq, q="Will X happen?"):
    # Kalshi/PredictIt/SX liefern Asks, Polymarkt hier mit Orderbuch: ausfuehrbar (pricing.REQUIRE_EXECUTABLE ist Standard)
    return {"platform": platform, "market_id": mid, "question": q, "extra_text": "",
            "yes_price": yes, "no_price": no, "volume": liq, "liquidity": liq, "end_date": END, "url": "",
            "price_basis": "ausfuehrbar"}


@pytest.fixture
def buy(monkeypatch, datadir):
    """Fuehrt _select_new_trades fuer genau ein (als echt verifiziertes) Paar aus und liefert den State."""
    monkeypatch.setattr(cpf.llm_verify, "verify_candidates",
                        lambda cands: {llm_verify._pair_key(a, b): True for a, b in cands})

    def run(ma, mb, cash=1000.0):
        profile = cpf.PROFILES["cross_platform_1k"]
        state = cpf._empty_state(profile)
        state["cash"] = cash
        cpf._select_new_trades(profile, state, [{"market_a": ma, "market_b": mb, "score": 1.0}], NOW)
        return state
    return run


def test_impact_wird_im_einstandspreis_bezahlt(buy):
    """Der Kaufimpact begrenzt nicht nur die Stueckzahl (edge-cap), er muss auch bezahlt werden:
    Kosten pro Anteil = Summe der um den linearen Impact erhoehten Beinpreise."""
    ma = mk("polymarket", "1", yes=0.40, no=0.60, liq=200.0)
    mb = mk("kalshi", "K1", yes=0.55, no=0.45, liq=200.0)
    state = buy(ma, mb)
    assert len(state["positions"]) == 1
    pos = state["positions"][0]
    shares = pos["shares"]
    imp_a = min(cpf.IMPACT_COEFFICIENT * shares * 0.40 / 200.0, 0.3)
    imp_b = min(cpf.IMPACT_COEFFICIENT * shares * 0.45 / 200.0, 0.3)
    eff = shares * (0.40 * (1 + imp_a) + 0.45 * (1 + imp_b))
    fee = kalshi.taker_fee(shares, 0.45)
    paid = 1000.0 - state["cash"]
    assert paid == pytest.approx(eff + fee, rel=1e-9)
    assert paid > shares * 0.85 + fee + 1.0  # deutlich mehr als der notierte Preis (Bug: war exakt notiert)


price = st.floats(0.01, 0.98, allow_nan=False)
liq = st.one_of(st.just(0.0), st.floats(1.0, 20000.0, allow_nan=False))


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ya=price, yb=price, la=liq, lb=liq, plat_a=st.sampled_from(["polymarket", "kalshi", "predictit", "sxbet"]),
       plat_b=st.sampled_from(["kalshi", "predictit", "sxbet"]), cash=st.floats(5.0, 20000.0))
def test_kein_garantierter_verlust_in_irgendeinem_ausgang(ya, yb, la, lb, plat_a, plat_b, cash, buy):
    """Bei korrekt gematchtem Paar zahlt genau EIN Bein 1,00. Der Fonds darf nur kaufen, wenn er in
    JEDEM der beiden Ausgaenge nicht (nennenswert) verliert - inkl. Kalshi-Einstiegsgebuehr und
    PredictIt-Gewinngebuehr bei Auszahlung."""
    ma = mk(plat_a, "A", ya, 1 - ya, la)
    mb = mk(plat_b, "B", yb, 1 - yb, lb)
    state = buy(ma, mb, cash=cash)
    assert state["cash"] >= -1e-6
    for pos in state["positions"]:
        s = pos["shares"]
        paid = cash - state["cash"]
        for winner in ("leg_a", "leg_b"):
            payout = sum(
                cpf._settle_leg(pos[k], 1.0 if k == winner else 0.0) for k in ("leg_a", "leg_b")
            ) * s
            assert payout - paid >= -0.03, (winner, payout, paid)  # 1 Cent Rundung je Beinstufe toleriert


@pytest.mark.parametrize("plat_a,plat_b", [("polymarket", "kalshi"), ("polymarket", "predictit"), ("kalshi", "predictit")])
def test_preis_null_stuerzt_nicht_ab(buy, plat_a, plat_b):
    ma = mk(plat_a, "A", 0.0, 1.0, 0.0)  # Yes-Kurs 0 ("kein Angebot"), keine Liquiditaet
    mb = mk(plat_b, "B", 0.97, 0.01, 0.0)
    state = buy(ma, mb)  # darf keine ZeroDivisionError werfen
    assert state["positions"] == []


def test_predictit_gewinngebuehr_wird_bei_der_auswahl_beruecksichtigt(buy):
    """Summe 0,965 -> 3,5 Cent 'Spread', aber gewinnt das PredictIt-Bein (0,35 Einsatz), kostet die
    10-%-Gewinngebuehr 6,5 Cent -> Verlust. Solche Paare duerfen nicht gekauft werden."""
    ma = mk("polymarket", "A", 0.615, 0.385, 1e6)
    mb = mk("predictit", "B", 0.60, 0.35, 0.0)  # Kombi Yes_A + No_B = 0.615 + 0.35 = 0.965
    state = buy(ma, mb)
    assert state["positions"] == []


def test_predictit_gewinngebuehr_profitables_paar_wird_gekauft(buy):
    ma = mk("polymarket", "A", 0.50, 0.50, 1e6)
    mb = mk("predictit", "B", 0.60, 0.30, 0.0)  # Summe 0.80, schlechtester Fall: 1 - 0.8 - 0.07 = +0.13
    state = buy(ma, mb)
    assert len(state["positions"]) == 1


# ------------------------------------------------------------ Runde 2: Aufloesung des Polymarkt-Beins
def _poly_leg(monkeypatch, outcome_prices, closed=True):
    import json
    m = {"id": "P1", "closed": closed, "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps(outcome_prices)}
    monkeypatch.setattr(cpf.fund_simulator, "fetch_market_by_id", lambda mid: m)
    return lambda side: cpf._poly_leg_mark_and_result({"market_id": "P1", "side": side})


def test_polymarkt_bein_50_50_aufloesung_zahlt_die_haelfte(monkeypatch):
    """Polymarket loest abgesagte/unklare Maerkte 50-50 auf (beide Seiten 0,50). Die Schwelle '>= 0,5 -> 1,00' zahlte
    das Bein voll aus: ein Paar brachte 1,50 bis 2,00 statt 1,00 bis 1,50 je Anteil."""
    f = _poly_leg(monkeypatch, ["0.5", "0.5"])
    assert f("yes")[1] == 0.5
    assert f("no")[1] == 0.5


def test_polymarkt_bein_geschlossen_aber_nicht_aufgeloest_wartet(monkeypatch):
    """'closed' heisst nur: Handel beendet. Mit Letztkursen 0,62/0,38 ist das Ergebnis noch offen (UMA-Aufloesung folgt)."""
    f = _poly_leg(monkeypatch, ["0.62", "0.38"])
    assert f("yes")[1] is None
    assert f("no")[1] is None


def test_polymarkt_bein_aufgeloest(monkeypatch):
    f = _poly_leg(monkeypatch, ["1", "0"])
    assert f("yes")[1] == 1.0 and f("no")[1] == 0.0
    g = _poly_leg(monkeypatch, ["0.995", "0.005"])  # praktisch entschieden
    assert g("yes")[1] == 1.0 and g("no")[1] == 0.0
    h = _poly_leg(monkeypatch, ["0.3", "0.7"], closed=False)
    assert h("yes") == (0.3, None)
