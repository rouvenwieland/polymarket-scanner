"""Polymarket-Fonds (fund_simulator): Randfaelle, Buchhaltungs-Invarianten, Verkaufslogik, Teil-Abruf."""
import csv
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import fund_simulator as fs

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def gamma(mid, yes, no, liq=500.0, days=10, closed=False):
    return {
        "id": str(mid), "slug": f"m{mid}", "question": f"Frage {mid}?", "closed": closed,
        "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps([str(yes), str(no)]),
        "liquidityNum": liq, "endDate": (NOW + timedelta(days=days)).isoformat(),
    }


def hit(mid, yes, no, liq=500.0, days=10):
    return {"market_id": str(mid), "frage": f"Frage {mid}?", "slug": f"m{mid}", "summe": yes + no,
            "liquiditaet": liq, "end_date": (NOW + timedelta(days=days)).isoformat()}


# ---------------------------------------------------------------- parse_prices
@pytest.mark.parametrize("prices", [["0", "0"], ["-0.5", "0.2"], ["nan", "0.2"], ["inf", "0.1"], ["1.5", "0.1"], ["0.3", "-0.1"]])
def test_parse_prices_lehnt_unsinnige_preise_ab(prices):
    m = {"outcomes": '["Yes","No"]', "outcomePrices": json.dumps(prices)}
    assert fs.parse_prices(m) is None


@pytest.mark.parametrize("prices,expected", [(["0.4", "0.5"], (0.4, 0.5)), (["0", "1"], (0.0, 1.0)), (["1", "0"], (1.0, 0.0))])
def test_parse_prices_grenzwerte_gueltig(prices, expected):
    m = {"outcomes": '["Yes","No"]', "outcomePrices": json.dumps(prices)}
    assert fs.parse_prices(m) == expected


def test_parse_prices_reihenfolge_no_yes():
    m = {"outcomes": '["No","Yes"]', "outcomePrices": '["0.7","0.2"]'}
    assert fs.parse_prices(m) == (0.2, 0.7)


# ------------------------------------------------------- Division durch null
def test_treffer_mit_summe_null_laesst_lauf_nicht_abstuerzen(datadir):
    profile = fs.PROFILES["aggressive"]
    fs.run_fund_step(profile, [], [hit(1, 0.0, 0.0, liq=0.0)], NOW)  # darf keine ZeroDivisionError werfen
    assert fs.load_state(profile)["positions"] == []


# ------------------------------------------------------------------ Verkaufslogik
def test_kein_verkauf_unter_einstandskurs():
    """Halten garantiert 1,00/Paar. Ein Verkauf mit Impact-Abschlag unter dem Einstandskurs
    realisiert einen sicheren Verlust (im Echtbetrieb: 15 von 48 Verkaeufen des Konservativ-Fonds)."""
    profile = fs.PROFILES["conservative"]
    pos = {"market_id": "1", "shares": 10.0, "einstandskurs": 0.90, "letzter_kurs": 0.90, "liquiditaet": 5.0}
    market = gamma(1, 0.50, 0.498, liq=5.0)  # Summe 0,998 >= sell_convergence, Impact auf 30 % gedeckelt
    decision = fs._evaluate_position(pos, market, profile)
    assert decision["action"] == "hold"


def test_verkauf_ueber_einstandskurs_bleibt_moeglich():
    profile = fs.PROFILES["conservative"]
    pos = {"market_id": "1", "shares": 1.0, "einstandskurs": 0.95, "letzter_kurs": 0.95, "liquiditaet": 5000.0}
    market = dict(gamma(1, 0.50, 0.498, liq=5000.0), bestBid=0.50, bestAsk=0.502)  # ausfuehrbar: Bid-Summe 0,998
    decision = fs._evaluate_position(pos, market, profile)
    assert decision["action"] == "sell"
    assert decision["effective_sum"] >= 0.95


# --------------------------------------------------------- Buchhaltungs-Invarianten
def _trades(profile):
    p = fs.os.path.join(fs.DATA_DIR, profile["trades_csv"])
    if not fs.os.path.isfile(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


price = st.floats(0.0, 1.0, allow_nan=False)
market_spec = st.tuples(
    st.integers(1, 6), price, price, st.floats(0.0, 5000.0, allow_nan=False),
    st.integers(-2, 60), st.booleans(),
)
tick = st.lists(market_spec, min_size=0, max_size=8, unique_by=lambda m: m[0])


@pytest.mark.parametrize("profile_key", list(fs.PROFILES))
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ticks=st.lists(tick, min_size=1, max_size=5))
def test_invarianten_ueber_mehrere_laeufe(profile_key, ticks, datadir):
    """Kasse nie negativ, keine Doppelpositionen, Limit offener Positionen, Kasse == Start + Summe(Trade-Betraege),
    jeder Einstieg < MAX_EFFECTIVE_BUY_PRICE (nie zu einem Garantie-Verlust kaufen), NAV >= 0."""
    for f in datadir.iterdir():  # Hypothesis ruft den Test mehrfach pro Fixture-Instanz auf
        f.unlink()
    profile = fs.PROFILES[profile_key]
    now = NOW
    for specs in ticks:
        now += timedelta(minutes=15)
        markets = [gamma(i, y, n, liq, d, c) for i, y, n, liq, d, c in specs]
        hits = [hit(i, y, n, liq, d) for i, y, n, liq, d, c in specs if not c and 0 < y + n < 1.0]
        fs.run_fund_step(profile, markets, hits, now)
        state = fs.load_state(profile)
        ids = [p["market_id"] for p in state["positions"]]
        assert state["cash"] >= -1e-6
        assert len(ids) == len(set(ids))
        assert len(ids) <= profile["max_open_positions"]
        assert all(p["einstandskurs"] < fs.MAX_EFFECTIVE_BUY_PRICE for p in state["positions"])
        assert all(p["shares"] > 0 for p in state["positions"])
        nav = state["cash"] + sum(p["shares"] * p.get("letzter_kurs", p["einstandskurs"]) for p in state["positions"])
        assert nav >= -1e-6
        rows = _trades(profile)
        recon = fs.STARTING_CAPITAL + sum(float(r["betrag"]) for r in rows)
        assert abs(recon - state["cash"]) <= 1e-3 + 1e-4 * len(rows)
        assert all(math.isfinite(float(r["betrag"])) for r in rows)


# ------------------------------------------------- Watcher: Fehler != "aufgeloest"
class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._p is None:
            raise ValueError("kein JSON")
        return self._p


def _state_with_position(profile):
    fs.save_state(profile, {"cash": 50.0, "started": "x", "positions": [{
        "market_id": "42", "frage": "F?", "slug": "f", "shares": 50.0, "einstandskurs": 0.9, "letzter_kurs": 0.9,
        "liquiditaet": 100.0, "end_date": "2027-01-01T00:00:00Z", "eingestiegen": "x"}]})


@pytest.mark.parametrize("behavior", ["timeout", "http500", "http429", "kaputtes_json"])
def test_watcher_zahlt_bei_transienten_fehlern_nicht_aus(datadir, monkeypatch, behavior):
    import requests
    profile = fs.PROFILES["aggressive"]
    _state_with_position(profile)

    def fake_get(url, timeout=None, **kw):
        if behavior == "timeout":
            raise requests.Timeout("zu langsam")
        if behavior == "http500":
            return _Resp(500)
        if behavior == "http429":
            return _Resp(429)
        return _Resp(200, None)

    monkeypatch.setattr(requests, "get", fake_get)
    fs.watch_positions_step(profile, NOW)
    st = fs.load_state(profile)
    assert st["cash"] == 50.0 and len(st["positions"]) == 1


def test_watcher_zahlt_bei_eindeutigem_404_aus(datadir, monkeypatch):
    import requests
    profile = fs.PROFILES["aggressive"]
    _state_with_position(profile)
    monkeypatch.setattr(requests, "get", lambda url, timeout=None, **kw: _Resp(404))
    fs.watch_positions_step(profile, NOW)
    st = fs.load_state(profile)
    assert st["positions"] == [] and st["cash"] == pytest.approx(100.0)


def test_watcher_zahlt_bei_geschlossenem_markt_aus(datadir, monkeypatch):
    import requests
    profile = fs.PROFILES["aggressive"]
    _state_with_position(profile)
    monkeypatch.setattr(requests, "get", lambda url, timeout=None, **kw: _Resp(200, gamma(42, 1.0, 0.0, closed=True)))
    fs.watch_positions_step(profile, NOW)
    assert fs.load_state(profile)["positions"] == []


def test_anhaengen_behaelt_zusatzspalten_und_schreibt_die_datei_nicht_um(datadir):
    """Runde 2 (Snapshot ceddafb): jeder Workflow-Lauf schrieb die History-/Trades-Datei auf seinen eigenen Header um
    und warf die Spalten 'bereinigt'/'bereinigung' der Bereinigung weg (Hunderte geaenderte Zeilen je Lauf)."""
    path = datadir / "t.csv"
    path.write_text("timestamp,kasse,bereinigt\r\n2026-10-01,100.0,1\r\n", encoding="utf-8", newline="")
    fs._append_csv(str(path), ["timestamp", "kasse"], {"timestamp": "2026-10-02", "kasse": 99.0})
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    assert [r["bereinigt"] for r in rows] == ["1", ""]
    assert path.read_bytes().startswith(b"timestamp,kasse,bereinigt\r\n2026-10-01,100.0,1\r\n")  # nur angehaengt
    fs._append_csv(str(path), ["timestamp", "kasse", "neu"], {"timestamp": "2026-10-03", "kasse": 98.0, "neu": 7})
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    assert [r["neu"] for r in rows] == ["", "", "7"] and rows[0]["bereinigt"] == "1"
