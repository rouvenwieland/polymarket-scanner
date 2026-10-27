"""Preisquellen: ausfuehrbar (Orderbuch) vs. Schaetzung (outcomePrices), Kennzeichnung durch alle Schichten."""
import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

import cross_platform_fund as cpf
import fund_simulator as fs
import llm_verify
import polymarket_snapshot as ps
import pricing
from platforms import polymarket as pm

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
# Enddatum relativ zur echten Uhr: snapshot_run nutzt datetime.now(); ein festes Datum (NOW + 10 Tage) lag ab 24.10. in der
# Vergangenheit, der Markt galt dann als abgelaufen und der Test schlug zeitabhaengig fehl.
END = (max(NOW, datetime.now(timezone.utc)) + timedelta(days=30)).isoformat()


def gm(mid, mid_yes, mid_no, bid=None, ask=None, liq=500.0):
    m = {"id": str(mid), "slug": f"m{mid}", "question": f"Frage {mid}?", "closed": False, "active": True,
         "outcomes": '["Yes","No"]', "outcomePrices": json.dumps([str(mid_yes), str(mid_no)]),
         "liquidityNum": liq, "volumeNum": 10, "endDate": END}
    if bid is not None:
        m["bestBid"] = bid
    if ask is not None:
        m["bestAsk"] = ask
    return m


# ------------------------------------------------------------------ Quotes
def test_kaufen_zum_ask_verkaufen_zum_bid():
    m = gm(1, 0.45, 0.55, bid=0.44, ask=0.46)
    b = pricing.buy_quotes(m)
    s = pricing.sell_quotes(m)
    assert (b.yes, b.no, b.basis) == (pytest.approx(0.46), pytest.approx(0.56), pricing.BASIS_EXECUTABLE)
    assert (s.yes, s.no) == (pytest.approx(0.44), pytest.approx(0.54))
    assert b.yes + b.no > 1.0 > s.yes + s.no  # im konsistenten Buch gibt es keine Kauf-Arbitrage


@pytest.mark.parametrize("bid,ask", [(None, None), (0.44, None), (0.0, 0.46), (0.5, 0.4), (0.44, 1.0), ("x", 0.4), (0.44, float("nan"))])
def test_ohne_plausibles_orderbuch_wird_auf_schaetzung_zurueckgefallen(bid, ask):
    m = gm(1, 0.45, 0.50, bid=bid, ask=ask)
    q = pricing.buy_quotes(m)
    assert q.basis == pricing.BASIS_ESTIMATE
    assert (q.yes, q.no) == (0.45, 0.50)
    assert pricing.sell_quotes(m).basis == pricing.BASIS_ESTIMATE


def test_platzhalterkurs_ist_als_schaetzung_gekennzeichnet():
    q = pricing.buy_quotes(gm(1, 0.5, 0.5))
    assert q.basis == pricing.BASIS_ESTIMATE
    assert pricing.LABELS[q.basis].startswith("Schätzung, nicht ausführbar")


def test_kein_kurs_gibt_none():
    assert pricing.buy_quotes({"outcomes": '["Yes","No"]', "outcomePrices": '["0","0"]'}) is None


def test_combine_basis_nur_wenn_alle_beine_ausfuehrbar():
    E, S = pricing.BASIS_EXECUTABLE, pricing.BASIS_ESTIMATE
    assert pricing.combine_basis(E, E) == E
    assert pricing.combine_basis(E, S) == S
    assert pricing.combine_basis() == S


def test_altbestand_ohne_feld_gilt_als_schaetzung():
    assert pricing.basis_of({}) == pricing.BASIS_ESTIMATE
    assert pricing.basis_of({"preisbasis": "ausfuehrbar"}) == pricing.BASIS_EXECUTABLE
    assert pricing.basis_of({"preisbasis": "quatsch"}) == pricing.BASIS_ESTIMATE


# ------------------------------------------------------- Polymarkt-Fonds
def test_verkauf_nutzt_bid_nicht_mittelkurs(monkeypatch):
    """Mittelkurs-Summe 0,998 wuerde bei Konvergenz verkaufen; zu den echten Bids (Summe 0,98) nicht.
    Standard (REQUIRE_EXECUTABLE): ohne Orderbuch wird gar nicht verkauft."""
    profile = fs.PROFILES["conservative"]
    pos = {"market_id": "1", "shares": 1.0, "einstandskurs": 0.9, "letzter_kurs": 0.9, "liquiditaet": 5000.0}
    est = gm(1, 0.50, 0.498)
    book = gm(1, 0.50, 0.498, bid=0.49, ask=0.51)
    assert fs._evaluate_position(pos, est, profile)["action"] == "hold"
    assert fs._evaluate_position(pos, book, profile)["action"] == "hold"
    monkeypatch.setattr(pricing, "REQUIRE_EXECUTABLE", False)
    assert fs._evaluate_position(pos, est, profile)["action"] == "sell"
    assert fs._evaluate_position(pos, book, profile)["action"] == "hold"


def snapshot_run(monkeypatch, markets):
    monkeypatch.setattr(ps, "fetch_all_open_markets_checked", lambda *a, **k: (markets, True))
    ps.main()


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_snapshot_kennzeichnet_basis_und_ignoriert_mittelkurs_artefakte(datadir, monkeypatch):
    markets = [
        gm(1, 0.40, 0.50),                                 # nur Schaetzung, Summe 0,90 -> Treffer (Schaetzung)
        gm(2, 0.40, 0.50, bid=0.39, ask=0.35),             # unplausibles Buch -> Schaetzung
        gm(3, 0.47, 0.50, bid=0.38, ask=0.41),             # Mittelkurse 0,97, aber ASKS 0,41+0,62 = 1,03 -> KEIN Treffer
        gm(4, 0.45, 0.50, bid=0.36, ask=0.39),             # Asks 0,39 + 0,64 = 1,03 -> kein Treffer
        gm(5, 0.30, 0.40, bid=0.30, ask=0.31),             # Asks 0,31 + 0,70 = 1,01 -> kein Treffer
        gm(6, 0.30, 0.40, bid=0.40, ask=0.30 + 0.0),       # bid > ask -> Schaetzung 0,70 -> Treffer (Schaetzung)
    ]
    # ein echter ausfuehrbarer Treffer: Yes-Ask 0,30, No-Ask = 1 - bid
    markets.append(gm(7, 0.30, 0.60, bid=0.40, ask=0.30))  # bid 0,40 > ask 0,30 -> ungueltig -> Schaetzung
    snapshot_run(monkeypatch, markets)
    rows = read_rows(ps.SNAPSHOTS_CSV)
    by_id = {r["market_id"]: r for r in rows}
    assert set(by_id) == {"1", "2", "6", "7"}
    assert all(r["preisbasis"] == pricing.BASIS_ESTIMATE for r in rows)


def test_snapshot_ausfuehrbarer_treffer_traegt_basis(datadir, monkeypatch):
    # Strukturelle Folge: bei gueltigem Buch (bid <= ask) ist Yes-Ask + No-Ask = ask + 1 - bid >= 1.
    # Ein ausfuehrbarer Treffer Yes+No < 1 kann im EINZELNEN Polymarkt-Markt also nie entstehen;
    # Treffer < 1 gibt es nur auf Schaetzkursen. Mittelkurs-Summe 0,90 hier ist ein Artefakt.
    m = gm(9, 0.40, 0.50, bid=0.30, ask=0.31)
    q = pricing.buy_quotes(m)
    assert q.yes + q.no >= 1.0
    snapshot_run(monkeypatch, [m])
    assert not (datadir / "snapshots.csv").exists()  # kein Treffer, nichts geschrieben


from hypothesis import given, strategies as st


@given(st.floats(0.001, 0.998), st.floats(0.0, 0.5))
def test_gueltiges_buch_hat_nie_kauf_summe_unter_eins(bid, spread):
    ask = min(bid + spread, 0.999)
    m = gm(1, 0.5, 0.5, bid=bid, ask=ask)
    b, s = pricing.buy_quotes(m), pricing.sell_quotes(m)
    assert b.basis == pricing.BASIS_EXECUTABLE
    assert b.yes + b.no >= 1.0 - 1e-12
    assert s.yes + s.no <= 1.0 + 1e-12


def test_snapshot_migriert_altbestand_ohne_basisspalte(datadir, monkeypatch):
    old = ["timestamp", "market_id", "frage", "slug", "yes_preis", "no_preis", "summe", "volumen", "liquiditaet", "end_date"]
    with open(ps.SNAPSHOTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(old)
        w.writerow(["2026-10-13T00:00:00+02:00", "7", "Alt?", "alt", 0.4, 0.5, 0.9, 1, 1, ""])
    snapshot_run(monkeypatch, [gm(1, 0.40, 0.50)])
    rows = read_rows(ps.SNAPSHOTS_CSV)
    assert [r["market_id"] for r in rows] == ["7", "1"]
    assert rows[0]["preisbasis"] == ""                                    # Altbestand: leer
    assert pricing.basis_of(rows[0]) == pricing.BASIS_ESTIMATE            # ... gilt als Schaetzung
    assert rows[1]["preisbasis"] == pricing.BASIS_ESTIMATE


def test_fonds_protokolliert_basis_in_trades_history_und_position(datadir, monkeypatch):
    monkeypatch.setattr(pricing, "REQUIRE_EXECUTABLE", False)  # Kennzeichnung, falls der Schalter abgeschaltet wird
    snapshot_run(monkeypatch, [gm(1, 0.40, 0.50, liq=2000.0)])
    profile = fs.PROFILES["conservative"]
    state = fs.load_state(profile)
    assert [p["preisbasis"] for p in state["positions"]] == [pricing.BASIS_ESTIMATE]
    trades = read_rows(datadir / profile["trades_csv"])
    assert trades[0]["aktion"] == "KAUF" and trades[0]["preisbasis"] == pricing.BASIS_ESTIMATE
    hist = read_rows(datadir / profile["history_csv"])
    assert int(hist[-1]["positionen_schaetzung"]) == 1
    assert float(hist[-1]["positionswert_schaetzung"]) > 0


def test_require_executable_handelt_keine_schaetzpreise(datadir, monkeypatch):
    monkeypatch.setattr(pricing, "REQUIRE_EXECUTABLE", True)
    snapshot_run(monkeypatch, [gm(1, 0.40, 0.50, liq=2000.0)])
    assert fs.load_state(fs.PROFILES["conservative"])["positions"] == []


# ------------------------------------------------------------ Cross-Fonds
def mk(platform, mid, yes, no, liq, basis):
    return {"platform": platform, "market_id": mid, "question": "Will X happen?", "extra_text": "",
            "yes_price": yes, "no_price": no, "volume": liq, "liquidity": liq, "end_date": END, "url": "",
            "price_basis": basis}


def cross_buy(monkeypatch, ma, mb):
    monkeypatch.setattr(cpf.llm_verify, "verify_candidates",
                        lambda c: {llm_verify._pair_key(a, b): True for a, b in c})
    profile = cpf.PROFILES["cross_platform_1k"]
    state = cpf._empty_state(profile)
    cpf._select_new_trades(profile, state, [{"market_a": ma, "market_b": mb, "score": 1.0}], NOW)
    return profile, state


def test_polymarkt_normalize_markiert_basis():
    est = pm.normalize(gm(1, 0.5, 0.5, liq=1.0))
    book = pm.normalize(gm(2, 0.5, 0.5, bid=0.49, ask=0.51))
    assert est["price_basis"] == pricing.BASIS_ESTIMATE
    assert book["price_basis"] == pricing.BASIS_EXECUTABLE
    assert book["yes_price"] == pytest.approx(0.51) and book["no_price"] == pytest.approx(0.51)


def test_cross_paar_wird_nur_auf_ausfuehrbaren_preisen_gekauft(datadir, monkeypatch):
    """Seit der Reparatur (27.10.2026) verlangt die Strategie-Pruefung ausfuehrbare Preise auf BEIDEN Beinen, auch wenn
    der alte Schalter REQUIRE_EXECUTABLE abgeschaltet wird. Gekauft wird dann mit Basis 'ausfuehrbar'."""
    monkeypatch.setattr(pricing, "REQUIRE_EXECUTABLE", False)
    E, S = pricing.BASIS_EXECUTABLE, pricing.BASIS_ESTIMATE
    for basis_a, bought in ((E, True), (S, False)):
        ma = mk("polymarket", "A", 0.40, 0.62, 1e6, basis_a)  # Yes_A + No_B = 0,92
        ma["fee_rate"] = 0.0
        mb = mk("kalshi", "B", 0.50, 0.52, 1e6, E)
        monkeypatch.setattr(cpf, "leg_depth", lambda m, side: None)
        profile, state = cross_buy(monkeypatch, ma, mb)
        assert bool(state["positions"]) == bought
        if bought:
            assert state["positions"][0]["preisbasis"] == E
            assert read_rows(datadir / profile["trades_csv"])[-1]["preisbasis"] == E
        for f in datadir.iterdir():
            f.unlink()


def test_cross_require_executable(datadir, monkeypatch):
    monkeypatch.setattr(pricing, "REQUIRE_EXECUTABLE", True)
    ma = mk("polymarket", "A", 0.30, 0.70, 1e6, pricing.BASIS_ESTIMATE)
    mb = mk("kalshi", "B", 0.60, 0.40, 1e6, pricing.BASIS_EXECUTABLE)
    _, state = cross_buy(monkeypatch, ma, mb)
    assert state["positions"] == []
