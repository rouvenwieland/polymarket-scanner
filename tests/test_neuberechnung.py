"""Neuberechnung (Reparatur 27.10.2026): Ergebnisdateien sind in sich stimmig, nur von Hand bestaetigte Paare werden
gekauft, abgelehnte nie; die Bereinigung vom 14.10. laesst neu berechnete Fonds in Ruhe."""
import csv
import json
import os
import shutil
from datetime import datetime, timezone

import pytest

import bereinigung as bd
import neuberechnung as nb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KORR = os.path.join(ROOT, "data", "korrigiert")
needs = pytest.mark.skipif(not os.path.isfile(os.path.join(KORR, "zusammenfassung.json")), reason="keine Neuberechnung")


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@needs
@pytest.mark.parametrize("fund", nb.CROSS_FUNDS + nb.POLY_FUNDS, ids=lambda f: f[0])
def test_kasse_gleich_start_plus_handelsliste_und_positionen_offen(fund):
    key, start, state_n, trades_n, hist_n, _ = fund
    state = json.load(open(os.path.join(KORR, state_n), encoding="utf-8"))
    trades = rows(os.path.join(KORR, trades_n))
    assert state["cash"] == pytest.approx(start + sum(float(t["betrag"]) for t in trades), abs=0.02)
    assert state["cash"] >= 0
    opened = [t["pair_id"] for t in trades if t["aktion"] == "KAUF"]
    closed = [t["pair_id"] for t in trades if t["aktion"] == "AUSZAHLUNG"]
    assert sorted(set(opened) - set(closed)) == sorted(p["pair_id"] for p in state["positions"])
    assert state["neuberechnung"]["strategie"] == nb.STRATEGIE


@needs
def test_nur_von_hand_bestaetigte_paare_werden_gekauft():
    review = {r["paar"]: r["entscheidung"] for r in rows(os.path.join(KORR, "paarpruefung.csv"))}
    for _, _, state_n, trades_n, _, _ in nb.CROSS_FUNDS:
        for t in rows(os.path.join(KORR, trades_n)):
            if t["aktion"] != "KAUF":
                continue
            a, b = t["pair_id"].split("__")
            assert review.get("__".join(sorted([a, b]))) == "echt", t["pair_id"]
            assert t["preisbasis"] == "ausfuehrbar"


@needs
def test_garantierter_endwert_nie_unter_einstand():
    for _, _, state_n, _, _, _ in nb.CROSS_FUNDS:
        for p in json.load(open(os.path.join(KORR, state_n), encoding="utf-8"))["positions"]:
            assert p["garantierter_endwert"] - p["einstand_gesamt"] >= 0.03 * p["shares"] - 1e-6


def test_simulation_kauft_abgelehnte_paare_nicht():
    """Synthetisch: ein Paar besteht die automatische Pruefung, ist von Hand aber abgelehnt -> kein Kauf."""
    import arbitrage_check as ac
    now = "2026-10-20T12:00:00+00:00"
    ma = {"platform": "polymarket", "market_id": "1", "question": "Q?", "yes_price": 0.2, "no_price": 0.999,
          "price_basis": "ausfuehrbar", "fee_rate": 0.0, "end_date": "2026-11-04T00:00:00Z", "yes_label": None,
          "no_label": None, "is_sport": False}
    mb = dict(ma, platform="predictit", market_id="2:3", yes_price=0.999, no_price=0.72, end_date=None)
    chk = ac.check_pair(ma, mb, datetime.fromisoformat(now))
    assert chk.ok
    cand = {"timestamp": now, "plattform_a": "polymarket", "id_a": "1", "frage_a": "Q?", "plattform_b": "predictit",
            "id_b": "2:3", "frage_b": "Q?", "seite_a": "yes", "seite_b": "no", "summe": 0.92,
            "paar": "polymarket:1__predictit:2:3", "eval": {"ok": True, "chk": chk, "ma": ma, "mb": mb}}
    src = {"gamma": {}, "sx": {}, "pi": {}, "kalshi": {}, "kurse": {}, "abgerufen": now}
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for decision, bought in (("abgelehnt", 0), ("echt", 1), (None, 0)):
        review = {cand["paar"]: {"entscheidung": decision}} if decision else {}
        _, trades, state = nb.simulate([now], [cand], src, review, 1000.0, start, 10.0)
        assert len(state["positions"]) == bought


def test_bereinigung_ueberspringt_neu_berechnete_fonds(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    fund = next(f for f in bd.FUNDS if f["key"] == "cross_platform_10k")
    (d / fund["state"]).write_text(json.dumps({"cash": 10000.0, "positions": [], "neuberechnung": {"strategie": "x"}}),
                                   encoding="utf-8")
    (d / fund["trades"]).write_text("timestamp,aktion,pair_id,frage,anteile,preis,betrag,kasse_danach,notiz,preisbasis\n",
                                    encoding="utf-8")
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    bd.run(str(d))
    assert bd.superseded(fund, str(d))
    assert {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()} == before
    assert not (d / bd.ARCHIVE_SUB / fund["trades"]).exists()
