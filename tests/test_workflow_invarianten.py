"""Runde 2 (14.10.2026): Verhindert der WORKFLOW-Code selbst, dass die Fehler wiederkommen, die scripts/bereinigung.py
entfernt hat? Die echten Fonds-Schritte (Snapshot-Fonds, Fonds-Watch, Multi-Platform-Scan-Fonds, Cross-Watch) laufen hier
mit zufaelligen Marktdaten (Hypothesis) auf dem BEREINIGTEN Stand aus data/ weiter.

Nach jedem simulierten Lauf gilt fuer alle Polymarkt- und Cross-Fonds:
  I1 Kasse = Start + Summe der Handelsliste (Toleranz 0,02 USD wie in der Bereinigung)
  I2 Kasse >= 0
  I3 offene Positionen im Zustand = offene Lebenszyklen (KAUF ohne Schliessung) in der Handelsliste
  I4 neue Kaeufe nur auf ausfuehrbaren Preisen (keine Schaetzkurs-Trades mehr)
Am Ende (Fixpunkt): ein erneuter Bereinigungslauf entfernt oder korrigiert KEINEN der neuen Trades. Sonst erzeugt der
Workflow genau die Fehler wieder, die die Bereinigung (R1 Schaetzkurs, R2 Zuordnung, R3 Spread > 25 %, R4 Verlust,
R5 Impact) entfernt hat.
Alles offline: jeder Marktabruf ist durch ein Modell der "Welt" ersetzt."""
import contextlib
import csv
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import bereinigung as bd
import cross_platform_fund as cpf
import fund_simulator as fs
import llm_verify
import polymarket_snapshot as snap
import pricing
from datenpruefung import check_archive
from platforms import kalshi, predictit, sxbet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "data")
SIM_FUNDS = [f for f in bd.FUNDS if f["kind"] in ("poly", "cross")]
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
END = (T0 + timedelta(days=30)).isoformat()
CLUBS = ["Alpha", "Bravo", "Charlie", "Delta"]

pytestmark = pytest.mark.skipif(not os.path.isdir(os.path.join(REAL, bd.ARCHIVE_SUB)),
                                reason="keine bereinigten Fondsdaten")


def rd(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def when(row):
    return datetime.fromisoformat(row["timestamp"])


@contextlib.contextmanager
def patched(*triples):
    old = [(o, n, getattr(o, n)) for o, n, _ in triples]
    try:
        for o, n, v in triples:
            setattr(o, n, v)
        yield
    finally:
        for o, n, v in reversed(old):
            setattr(o, n, v)


_BEREINIGTER_STAND = []


def _bereinigter_stand():
    """Der BEREINIGTE Stand der echten Daten (data/ + Sicherung), einmal je Testlauf in einem Temp-Verzeichnis berechnet.
    Nicht der rohe Inhalt von data/: Workflow-Laeufe committen laufend neue Zeilen nach main (auch von altem Code oder
    waehrend eines Merges), dann stuende dort ein Zwischenstand und die Tests haengen vom Zufall des letzten Laufs ab.
    Die Bereinigung ist genau das, was nach jedem Merge ausgefuehrt wird - ihr Ergebnis ist die Ausgangslage."""
    if not _BEREINIGTER_STAND:
        import atexit
        tmp = tempfile.mkdtemp()
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)
        d = os.path.join(tmp, "data")
        os.makedirs(d)
        for f in bd.FUNDS:
            for k in ("trades", "history", "state"):
                p = os.path.join(REAL, f[k])
                if os.path.isfile(p):
                    shutil.copy2(p, os.path.join(d, f[k]))
        shutil.copytree(os.path.join(REAL, bd.ARCHIVE_SUB), os.path.join(d, bd.ARCHIVE_SUB))
        with contextlib.redirect_stdout(io.StringIO()):
            bd.run(d)
        _BEREINIGTER_STAND.append(d)
    return _BEREINIGTER_STAND[0]


def clean_copy(dst):
    src = _bereinigter_stand()
    for f in bd.FUNDS:
        for k in ("trades", "history", "state"):
            p = os.path.join(src, f[k])
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(dst, f[k]))
    shutil.copytree(os.path.join(src, bd.ARCHIVE_SUB), os.path.join(dst, bd.ARCHIVE_SUB))


class World:
    """Was die Plattformen gerade melden (statt Netz)."""

    def __init__(self):
        self.poly = {}
        self.kalshi = {}

    def poly_market(self, s):
        m = {"id": f"sim{s['i']}", "slug": f"sim-{s['i']}", "question": f"Simulierter Markt {s['i']}?",
             "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps([str(s["yes"]), str(s["no"])]),
             "liquidityNum": s["liq"], "volumeNum": 1000.0, "endDate": END, "closed": s["closed"]}
        if s["book"] is not None:
            bid, width = s["book"]
            m["bestBid"], m["bestAsk"] = bid, min(0.99, bid + width)
        if s["closed"]:
            m["outcomePrices"] = '["1", "0"]'
        self.poly[m["id"]] = m
        return m

    def cross_match(self, c):
        q = f"Will {CLUBS[c['j']]} win the 2026 Supercup final?"
        ma = {"platform": "kalshi", "market_id": f"KXSIM-{c['j']}", "question": q, "yes_price": c["ya"],
              "no_price": c["na"], "price_basis": pricing.BASIS_EXECUTABLE, "liquidity": c["liq_a"], "end_date": END}
        mb = {"platform": "polymarket", "market_id": f"simx{c['j']}", "question": q, "yes_price": c["yb"],
              "no_price": c["nb"], "price_basis": c["basis_b"], "liquidity": c["liq_b"], "end_date": END}
        if c["resolve"]:  # beide Beine loesen gleich auf (korrektes Paar)
            self.kalshi[ma["market_id"]] = {"status": "settled", "result": c["resolve"], "yes_ask_dollars": None}
            self.poly[mb["market_id"]] = {"id": mb["market_id"], "closed": True, "outcomes": '["Yes", "No"]',
                                          "outcomePrices": '["1", "0"]' if c["resolve"] == "yes" else '["0", "1"]'}
        return {"market_a": ma, "market_b": mb, "score": 1.0}

    def lookup(self, mid):
        m = self.poly.get(mid)
        return (m, "ok") if m else (None, "not_found")


poly_spec = st.fixed_dictionaries({
    "i": st.integers(0, 5),
    "yes": st.floats(0.02, 0.98), "no": st.floats(0.02, 0.98),
    "book": st.one_of(st.none(), st.tuples(st.floats(0.02, 0.97), st.floats(0.0, 0.2))),
    "liq": st.floats(0.0, 5000.0),
    "closed": st.sampled_from([False] * 6 + [True]),
})
cross_spec = st.fixed_dictionaries({
    "j": st.integers(0, 3),
    "ya": st.floats(0.02, 0.98), "na": st.floats(0.02, 0.98),
    "yb": st.floats(0.02, 0.98), "nb": st.floats(0.02, 0.98),
    "basis_b": st.sampled_from([pricing.BASIS_EXECUTABLE, pricing.BASIS_ESTIMATE]),
    "liq_a": st.floats(0.0, 5000.0), "liq_b": st.floats(0.0, 5000.0),
    "resolve": st.sampled_from([None, None, None, "yes", "no"]),
})
run_spec = st.tuples(st.sampled_from(["snapshot", "fund_watch", "multi_scan", "cross_watch"]),
                     st.lists(poly_spec, max_size=5), st.lists(cross_spec, max_size=3))


def check_invariants(d, strict=True):
    for f in SIM_FUNDS:
        sp = os.path.join(d, f["state"])
        if not os.path.isfile(sp):
            continue
        state = json.load(open(sp, encoding="utf-8"))
        trades = rd(os.path.join(d, f["trades"]))
        ledger = f["start"] + sum(float(t["betrag"]) for t in trades)
        assert abs(state["cash"] - ledger) < bd.TOLERANCE, (f["key"], "I1", state["cash"], ledger)
        assert state["cash"] >= -1e-9, (f["key"], "I2", state["cash"])
        open_ids = sorted(lc["id"] for lc in bd.lifecycles(trades, f["idcol"]) if not lc["closed"])
        state_ids = sorted(p.get("pair_id") or p.get("market_id") for p in state["positions"])
        assert open_ids == state_ids, (f["key"], "I3")
        for t in trades:
            if strict and t["aktion"] == "KAUF" and when(t) >= T0:
                assert t.get("preisbasis") == pricing.BASIS_EXECUTABLE, (f["key"], "I4 Schaetzkurs-Kauf", t["frage"], t["notiz"])


def new_rows(d):
    return {f["key"]: [(t["timestamp"], t["aktion"], t[f["idcol"]], t["betrag"]) for t in rd(os.path.join(d, f["trades"]))
                       if when(t) >= T0] for f in SIM_FUNDS}


def simulate(d, runs, t0=T0, strict=True):
    world = World()
    with patched((fs, "DATA_DIR", d), (fs, "lookup_market", world.lookup),
                 (fs, "fetch_market_by_id", lambda mid: world.poly.get(mid)),
                 (kalshi, "fetch_market_by_ticker", lambda t: world.kalshi.get(t)),
                 (kalshi, "fetch_ask_levels", lambda t, side: None),
                 (predictit, "fetch_contract_by_market_id", lambda mid: (None, None)),
                 (sxbet, "fetch_market_by_hash", lambda h: None), (sxbet, "fetch_market_meta", lambda h: None),
                 (llm_verify, "verify_candidates", lambda cands: {llm_verify._pair_key(a, b): True for a, b in cands})):
        for k, (kind, polys, crosses) in enumerate(runs):
            now = t0 + timedelta(hours=3 * k)
            markets = [world.poly_market(s) for s in polys]
            matches = [world.cross_match(c) for c in crosses]
            if kind == "snapshot":
                fs.run_all_fund_steps(markets, snap.build_hits(markets, now), now)
            elif kind == "fund_watch":
                fs.watch_all_positions_step(now)
            elif kind == "multi_scan":
                cpf.run_all_fund_steps(matches, now)
            else:
                cpf.watch_all_positions_step(now)
            check_invariants(d, strict)


@settings(max_examples=20, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(run_spec, min_size=1, max_size=6))
def test_workflow_laeufe_halten_invarianten_und_bereinigung_ist_fixpunkt(runs):
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "data")
        os.makedirs(d)
        clean_copy(d)
        check_invariants(d)  # Ausgangslage: bereinigter Stand
        simulate(d, runs)
        before = new_rows(d)
        bd.run(d)
        assert new_rows(d) == before, "Bereinigung entfernt/korrigiert Trades, die der Workflow neu erzeugt hat"
        check_invariants(d)


# ------------------------------------------------------------------ gezielte Einzelfaelle (deterministisch)
def _one_run(kind, polys=(), crosses=()):
    """Ein einzelner Lauf auf FRISCHEN Fonds (leeres Verzeichnis, volle Kasse, alle Plaetze frei). Bewusst nicht auf den
    echten Daten: dort haengen 'es wird gekauft' (freie Plaetze, Kasse) und 'es wird nicht gekauft' (sonst waere ein Fonds mit
    15 offenen Positionen nie kaufbereit) vom jeweiligen Stand des Workflows ab."""
    tmp = tempfile.mkdtemp()
    d = os.path.join(tmp, "data")
    os.makedirs(d)
    simulate(d, [(kind, list(polys), list(crosses))])
    return d


def test_schaetzkurs_treffer_wird_nicht_gekauft():
    """R1: Yes+No < 1 nur auf outcomePrices (kein Orderbuch) ist ein Artefakt - der Snapshot-Fonds darf es nicht kaufen."""
    d = _one_run("snapshot", [{"i": 1, "yes": 0.40, "no": 0.50, "book": None, "liq": 3000.0, "closed": False}])
    for key in ("fund_trades.csv", "fund_trades_aggressive.csv", "fund_trades_bestcase.csv"):
        assert not [t for t in rd(os.path.join(d, key)) if t["aktion"] == "KAUF"], key


def test_cross_paar_mit_unplausiblem_spread_wird_nicht_gekauft():
    """R3: > 25 % 'Spread' zwischen zwei Plattformen ist (laut Bereinigung) ein Daten-/Fehlmatch-Artefakt."""
    c = {"j": 0, "ya": 0.30, "na": 0.72, "yb": 0.74, "nb": 0.30, "basis_b": pricing.BASIS_EXECUTABLE,
         "liq_a": 4000.0, "liq_b": 4000.0, "resolve": None}
    d = _one_run("multi_scan", crosses=[c])
    for f in SIM_FUNDS:
        if f["kind"] == "cross":
            assert not [t for t in rd(os.path.join(d, f["trades"])) if when(t) >= T0], f["key"]


def test_plausibles_ausfuehrbares_cross_paar_wird_weiter_gekauft():
    """Gegenprobe: die Schutzregeln blockieren nicht alles."""
    c = {"j": 1, "ya": 0.46, "na": 0.56, "yb": 0.55, "nb": 0.47, "basis_b": pricing.BASIS_EXECUTABLE,
         "liq_a": 4000.0, "liq_b": 4000.0, "resolve": None}
    d = _one_run("multi_scan", crosses=[c])
    bought = [t for t in rd(os.path.join(d, "fund_trades_crossplatform_1k.csv")) if when(t) >= T0]
    assert bought and bought[0]["preisbasis"] == pricing.BASIS_EXECUTABLE


def test_dasselbe_paar_zweimal_in_einem_lauf_wird_einmal_gekauft():
    """Doppelte Maerkte in einem Abruf (Paginierung) duerfen kein zweites, ueberlappendes Paar mit derselben ID erzeugen:
    sonst bleibt nach der Auszahlung ein Lebenszyklus fuer immer offen (I3). Real nie beobachtet, von Hypothesis gefunden."""
    c = {"j": 2, "ya": 0.46, "na": 0.56, "yb": 0.55, "nb": 0.47, "basis_b": pricing.BASIS_EXECUTABLE,
         "liq_a": 4000.0, "liq_b": 4000.0, "resolve": None}
    d = _one_run("multi_scan", crosses=[c, dict(c, nb=0.46)])
    bought = [t for t in rd(os.path.join(d, "fund_trades_crossplatform_1k.csv")) if when(t) >= T0 and t["aktion"] == "KAUF"]
    assert len(bought) == 1
    check_invariants(d)


def test_spread_schwelle_mindestens_so_streng_wie_in_der_bereinigung():
    """Die Bereinigung vom 14.10. verwarf Spreads ueber 25 %; seit der Reparatur vom 27.10. kauft der Fonds nur bis 10 %.
    Strenger ist erlaubt (die Bereinigung bleibt Fixpunkt), lockerer nicht."""
    assert cpf.MAX_PLAUSIBLE_SPREAD <= bd.MAX_PLAUSIBLE_SPREAD


# ------------------------------------------------------------------ Merge mit Workflow-Laeufen (Snapshot ceddafb, 14.10.)
def rebuild_from_archive(d, tmp):
    """Wie tests/test_bereinigung.py (real_copy): die Sicherung als unbereinigte Live-Daten neu bereinigen."""
    o = os.path.join(tmp, "orig")
    os.makedirs(o)
    for f in bd.FUNDS:
        for k in ("trades", "history", "state"):
            p = os.path.join(d, bd.ARCHIVE_SUB, f[k])
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(o, f[k]))
    bd.run(o)
    check_invariants(o)


def snapshot_hashes(d):
    import glob
    import hashlib
    return {os.path.relpath(p, d): hashlib.sha256(open(p, "rb").read()).hexdigest()
            for p in glob.glob(os.path.join(d, "**", "*"), recursive=True) if os.path.isfile(p)}


def test_alter_workflow_kauft_auf_schaetzkurs_bereinigung_bleibt_konsistent():
    """Genau der Vorfall: der Snapshot auf main (alter Code, REQUIRE_EXECUTABLE aus) kaufte im bereinigten Best-Case-
    Fonds zwei Schaetzkurs-Positionen. Merge + Bereinigung: die zwei Kaeufe fliegen raus, Kasse wieder 100,0094.
    Vorher blieb die Sicherung inkonsistent (Zeilen angehaengt, Kasse nicht) -> 10 Tests rot ('Kasse 200,0188 weicht
    von Handelsliste 100,0000 ab'), sobald jemand die Sicherung als unbereinigten Stand liest."""
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "data")
        os.makedirs(d)  # frische Fonds: der Test verlangt Kaeufe, die Kasse/Plaetze der echten Daten duerfen dabei nicht zaehlen
        cash0 = 100.0
        with patched((pricing, "REQUIRE_EXECUTABLE", False)):
            simulate(d, [("snapshot", [{"i": 1, "yes": 0.40, "no": 0.595, "book": None, "liq": 180.0, "closed": False},
                                       {"i": 2, "yes": 0.30, "no": 0.695, "book": None, "liq": 10.0, "closed": False}], [])],
                     strict=False)
        assert [t for t in rd(os.path.join(d, "fund_trades_bestcase.csv")) if t["aktion"] == "KAUF"]
        bd.run(d)
        check_invariants(d)
        check_archive(d)
        assert abs(json.load(open(os.path.join(d, "fund_state_bestcase.json"), encoding="utf-8"))["cash"] - cash0) < 1e-6
        first = snapshot_hashes(d)
        bd.run(d)
        assert snapshot_hashes(d) == first, "zweiter Lauf nicht idempotent"
        rebuild_from_archive(d, tmp)


def test_schliessung_spaeterer_laeufe_entfernt_position_aus_der_sicherung():
    """Kauf in Lauf 1, Auszahlung in Lauf 2 (je mit Bereinigung dazwischen): die Sicherung fuehrt die Position danach
    nicht mehr als offen (sonst zaehlte sie 'Positionen vorher' falsch und waere nicht mehr fuer sich konsistent)."""
    c = {"j": 3, "ya": 0.46, "na": 0.56, "yb": 0.55, "nb": 0.47, "basis_b": pricing.BASIS_EXECUTABLE,
         "liq_a": 4000.0, "liq_b": 4000.0, "resolve": None}
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "data")
        os.makedirs(d)  # frische Fonds: der Test verlangt einen Kauf (freier Platz, Kasse), unabhaengig vom echten Stand
        simulate(d, [("multi_scan", [], [c])])
        bd.run(d)
        check_archive(d)
        arch_state = bd.read_json(os.path.join(d, bd.ARCHIVE_SUB, "fund_state_crossplatform_1k.json"))
        assert any("KXSIM-3" in p["pair_id"] for p in arch_state["positions"])
        simulate(d, [("cross_watch", [], [dict(c, resolve="yes")])], t0=T0 + timedelta(days=1))
        bd.run(d)
        check_invariants(d)
        check_archive(d)
        arch_state = bd.read_json(os.path.join(d, bd.ARCHIVE_SUB, "fund_state_crossplatform_1k.json"))
        assert not any("KXSIM-3" in p["pair_id"] for p in arch_state["positions"])
        rebuild_from_archive(d, tmp)


@settings(max_examples=10, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(run_spec, min_size=1, max_size=4), st.lists(run_spec, min_size=1, max_size=4))
def test_mehrere_merges_mit_workflow_laeufen_bleiben_idempotent_und_konsistent(phase1, phase2):
    """Workflow-Laeufe -> Bereinigung -> weitere Laeufe -> Bereinigung: Live-Daten und Sicherung bleiben konsistent,
    jeder Bereinigungslauf ist idempotent, und die Sicherung laesst sich als unbereinigter Stand neu bereinigen."""
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "data")
        os.makedirs(d)
        clean_copy(d)
        for n, phase in enumerate((phase1, phase2)):
            simulate(d, phase, t0=T0 + timedelta(days=n))
            bd.run(d)
            check_invariants(d)
            check_archive(d)
            first = snapshot_hashes(d)
            bd.run(d)
            assert snapshot_hashes(d) == first
        rebuild_from_archive(d, tmp)
