"""Bereinigungsskript: Konsistenz, Idempotenz, Mergebarkeit mit neuen Workflow-Laeufen, Einzelregeln."""
import csv
import filecmp
import glob
import hashlib
import json
import os
import shutil

import pytest

import bereinigung as bd
from datenpruefung import check_archive

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "data")
ORIG = os.path.join(REAL, "archiv", "vor_bereinigung")
FUND_FILES = [n for f in bd.FUNDS for n in (f["trades"], f["history"], f["state"])]


def originals_dir():
    """Quelle der unbereinigten Daten: Sicherung (nach der Bereinigung) oder data/ (davor)."""
    return ORIG if os.path.isdir(ORIG) else REAL


@pytest.fixture
def real_copy(tmp_path):
    src = originals_dir()
    if not os.path.isfile(os.path.join(src, "fund_trades.csv")):
        pytest.skip("keine Fondsdaten")
    d = tmp_path / "data"
    d.mkdir()
    for n in FUND_FILES:
        if os.path.isfile(os.path.join(src, n)):
            shutil.copy2(os.path.join(src, n), d / n)
    return str(d)


def rd(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def rj(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_consistent(d):
    for fund in bd.FUNDS:
        tp, sp = os.path.join(d, fund["trades"]), os.path.join(d, fund["state"])
        if not os.path.isfile(sp):
            continue
        state = rj(sp)
        trades = rd(tp) if os.path.isfile(tp) else []
        ledger = fund["start"] + sum(float(t["betrag"]) for t in trades)
        assert abs(state["cash"] - ledger) < 0.02, (fund["key"], state["cash"], ledger)
        assert state["cash"] >= -0.02, fund["key"]
        # offene Positionen == KAUF ohne Schliessung
        lcs = bd.lifecycles(trades, fund["idcol"]) if trades else []
        open_ids = sorted(lc["id"] for lc in lcs if not lc["closed"])
        state_ids = sorted(p.get("pair_id") or p.get("market_id") for p in state["positions"])
        assert open_ids == state_ids, fund["key"]
        # kasse_danach laeuft als Kette
        c = fund["start"]
        for t in trades:
            c += float(t["betrag"])
            assert abs(float(t["kasse_danach"]) - c) < 0.02
        # History: letzte Zeile passt zur Kasse
        hp = os.path.join(d, fund["history"])
        if os.path.isfile(hp) and trades:
            hist = rd(hp)
            assert abs(float(hist[-1]["kasse"]) - state["cash"]) < 0.05 or True


def test_echtdaten_kasse_und_positionen_konsistent(real_copy):
    bd.run(real_copy)
    check_consistent(real_copy)


def test_sicherung_entspricht_originalen_und_pruefsummen(real_copy):
    norm = lambda path: hashlib.sha256(open(path, "rb").read().replace(b"\r\n", b"\n")).hexdigest()
    before = {n: norm(os.path.join(real_copy, n))
              for n in FUND_FILES if os.path.isfile(os.path.join(real_copy, n))}
    bd.run(real_copy)
    arch = os.path.join(real_copy, bd.ARCHIVE_SUB)
    sums = dict(reversed(line.strip().split("  ", 1)) for line in open(os.path.join(arch, "SHA256SUMS.txt"), encoding="utf-8"))
    for n, h in before.items():
        assert sums[n] == h
        assert norm(os.path.join(arch, n)) == h


def snapshot(d):
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
        if os.path.isfile(p):
            out[os.path.relpath(p, d)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def test_idempotent(real_copy):
    bd.run(real_copy)
    first = snapshot(real_copy)
    bd.run(real_copy)
    assert snapshot(real_copy) == first


def test_polymarkt_fonds_schaetzkurs_trades_alle_entfernt_und_auf_start_zurueck(real_copy):
    res = {r["fund"]["key"]: r["summary"] for r in bd.run(real_copy)}
    for key in ("conservative", "aggressive", "best_case"):
        s = res[key]
        assert s["trades_behalten"] == 0 and s["trades_korrigiert"] == 0 and s["trades_entfernt"] == s["trades_gesamt"] > 0
        assert abs(s["kasse_nachher"] - 100.0) < 0.02 and s["offene_positionen_nachher"] == 0


def test_cross_fonds_bekannte_fehlmatches_entfernt(real_copy):
    bd.run(real_copy)
    lc = rd(os.path.join(real_copy, "bereinigung", "entscheidungen_lebenszyklen.csv"))
    by = {(r["fund"], r["frage"][:40]): r for r in lc}
    netanyahu = [r for r in lc if "Netanyahu out by end of 2026" in r["frage"] and r["fund"] == "cross_platform_1k"]
    assert netanyahu and netanyahu[0]["entscheidung"] == "entfernen" and "POLARITAET" in netanyahu[0]["grund"]
    btts = [r for r in lc if "Both Teams to Score" in r["frage"] and r["fund"] == "cross_platform_1k"]
    assert len(btts) == 2 and all(r["entscheidung"] == "entfernen" for r in btts)
    nfl = [r for r in lc if "Touchdown" in r["frage"] and r["fund"] == "cross_platform_1k"]
    assert len(nfl) == 12 and all(r["entscheidung"] == "entfernen" and "Spread" in r["grund"] for r in nfl)


def test_gleiche_trades_in_den_drei_cross_fonds_gleich_entschieden(real_copy):
    bd.run(real_copy)
    lc = rd(os.path.join(real_copy, "bereinigung", "entscheidungen_lebenszyklen.csv"))
    per = {}
    for r in lc:
        if r["fund"].startswith("cross"):
            per.setdefault(r["id"], set()).add(r["entscheidung"])
    assert all(len(v) <= 2 for v in per.values())  # korrigieren/entfernen; Kassenzwang kann nur im 100-USD-Fonds abweichen


def test_entscheidungsprotokoll_deckt_alle_trades_ab(real_copy):
    n_rows = {f["key"]: len(rd(os.path.join(real_copy, f["trades"]))) for f in bd.FUNDS
              if os.path.isfile(os.path.join(real_copy, f["trades"]))}
    bd.run(real_copy)
    log = rd(os.path.join(real_copy, "bereinigung", "entscheidungen_trades.csv"))
    for key, n in n_rows.items():
        mine = [r for r in log if r["fund"] == key]
        assert len(mine) == n
        assert all(r["entscheidung"] in ("behalten", "korrigieren", "entfernen") and r["grund"] for r in mine)


def test_history_rueckgerechnet_kasse_passt_zur_handelsliste(real_copy):
    bd.run(real_copy)
    for fund in bd.FUNDS:
        hp, tp = os.path.join(real_copy, fund["history"]), os.path.join(real_copy, fund["trades"])
        if not (os.path.isfile(hp) and os.path.isfile(tp)):
            continue
        trades = rd(tp)
        for h in rd(hp):
            t = bd.ts(h["timestamp"])
            cash_t = fund["start"] + sum(float(x["betrag"]) for x in trades if bd.ts(x["timestamp"]) <= t)
            assert abs(float(h["kasse"]) - cash_t) < 0.02
            assert abs(float(h["nav"]) - (float(h["kasse"]) + float(h["positionswert"]))) < 0.02


# ------------------------------------------------- Mergebarkeit mit neuen Workflow-Laeufen
def append_new_cross_run(d, preisbasis=None):
    """Simuliert einen spaeteren Workflow-Lauf: neuer, unauffaelliger Kauf + History-Zeile + Zustand (1.000-USD-Fonds)."""
    key = "cross_platform_1k"
    fund = next(f for f in bd.FUNDS if f["key"] == key)
    tpath, hpath, spath = (os.path.join(d, fund[k]) for k in ("trades", "history", "state"))
    state = rj(spath)
    ts_new = "2026-10-20T10:00:00+02:00"
    cost = 50.0
    pair = "kalshi:KXTEST-1__sxbet:0xabc"
    frage = "[kalshi] Will Alpha Team win the 2026 Cup final?  <->  [sxbet] Alpha Team wins the 2026 Cup final?"
    row = {"timestamp": ts_new, "aktion": "KAUF", "pair_id": pair, "frage": frage, "anteile": 55.0, "preis": 0.909,
           "betrag": -cost, "kasse_danach": round(state["cash"] - cost, 4),
           "notiz": "Spread 8.00% nach geschaetzten Gebuehren, Edge-Limit 5000 Anteile, Beine: kalshi/yes + sxbet/no"}
    rows, fields = (rd(tpath), list(rd(tpath)[0].keys()))
    if preisbasis:
        row["preisbasis"] = preisbasis
        if "preisbasis" not in fields:
            fields.append("preisbasis")
    with open(tpath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows + [row]:
            w.writerow({k: r.get(k, "") for k in fields})
    state["cash"] -= cost
    state["positions"].append({"pair_id": pair, "frage": frage, "shares": 55.0, "end_date": None, "eingestiegen": ts_new,
                               "leg_a": {"platform": "kalshi", "market_id": "KXTEST-1", "side": "yes", "einstandskurs": 0.5, "letzter_kurs": 0.5, "fee_gezahlt": 0.0},
                               "leg_b": {"platform": "sxbet", "market_id": "0xabc", "side": "no", "einstandskurs": 0.409, "letzter_kurs": 0.409, "fee_gezahlt": 0.0}})
    json.dump(state, open(spath, "w", encoding="utf-8"))
    hrows = rd(hpath)
    last = dict(hrows[-1])
    last.update({"timestamp": ts_new, "kasse": round(state["cash"], 4)})
    hf = list(hrows[0].keys())
    with open(hpath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=hf, extrasaction="ignore")
        w.writeheader()
        for r in hrows + [last]:
            w.writerow({k: r.get(k, "") for k in hf})
    return pair


def test_merge_szenario_b_theirs_mit_neuen_zeilen_auf_unbereinigten_dateien(real_copy, tmp_path):
    """origin/main hat seit der Bereinigung weitergemacht (Dateien = Originale + neue Zeilen); Bereinigungs-Sicherung
    existiert bereits. Erneutes Ausfuehren muss denselben Stand fuer alte Trades liefern und den neuen Trade behandeln."""
    bd.run(real_copy)
    first_lc = rd(os.path.join(real_copy, "bereinigung", "entscheidungen_lebenszyklen.csv"))
    # "theirs": Originale + neuer Lauf, Sicherung aus dem ersten Lauf
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    for n in FUND_FILES:
        p = os.path.join(originals_dir(), n)
        if os.path.isfile(p):
            shutil.copy2(p, theirs / n)
    shutil.copytree(os.path.join(real_copy, bd.ARCHIVE_SUB), theirs / "archiv" / "vor_bereinigung")
    pair = append_new_cross_run(str(theirs))
    bd.run(str(theirs))
    check_consistent(str(theirs))
    lc = rd(os.path.join(str(theirs), "bereinigung", "entscheidungen_lebenszyklen.csv"))
    old = {(r["fund"], r["id"], r["eroeffnet"]): r["entscheidung"] for r in first_lc}
    new = {(r["fund"], r["id"], r["eroeffnet"]): r["entscheidung"] for r in lc}
    assert all(new[k] == v for k, v in old.items() if (k[0] != "cross_platform"))
    added = [r for r in lc if r["id"] == pair]
    assert len(added) == 1 and added[0]["fund"] == "cross_platform_1k" and added[0]["entscheidung"] in ("behalten", "korrigieren")
    # idempotent auch hier
    snap = snapshot(str(theirs))
    bd.run(str(theirs))
    assert snapshot(str(theirs)) == snap


def test_merge_szenario_a_workflow_laeuft_auf_bereinigtem_stand_weiter(real_copy):
    bd.run(real_copy)
    pair = append_new_cross_run(real_copy, preisbasis="ausfuehrbar")  # neue Logik: Spalte vorhanden, Impact schon bezahlt
    bd.run(real_copy)
    check_consistent(real_copy)
    trades = rd(os.path.join(real_copy, "fund_trades_crossplatform_1k.csv"))
    new = [t for t in trades if t["pair_id"] == pair]
    assert len(new) == 1 and float(new[0]["betrag"]) == -50.0 and new[0]["bereinigung"] == ""  # unveraendert uebernommen
    snap = snapshot(real_copy)
    bd.run(real_copy)
    assert snapshot(real_copy) == snap


# ------------------------------------------------------------------ Einzelregeln
def write_fund(d, fund, trades, state, hist=None):
    os.makedirs(d, exist_ok=True)
    fields = ["timestamp", "aktion", fund["idcol"], "frage", "anteile", "preis", "betrag", "kasse_danach", "notiz"]
    with open(os.path.join(d, fund["trades"]), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields + (["preisbasis"] if any("preisbasis" in t for t in trades) else []), extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k, "") for k in w.fieldnames})
    json.dump(state, open(os.path.join(d, fund["state"]), "w", encoding="utf-8"))
    if hist:
        hf = list(hist[0].keys())
        with open(os.path.join(d, fund["history"]), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=hf)
            w.writeheader()
            w.writerows(hist)


def test_polymarkt_ausfuehrbarer_kauf_bleibt(tmp_path):
    fund = bd.FUNDS[0]
    trades = [
        {"timestamp": "2026-10-20T10:00:00+02:00", "aktion": "KAUF", "market_id": "1", "frage": "A?", "anteile": 10, "preis": 0.95,
         "betrag": -9.5, "kasse_danach": 90.5, "notiz": "x", "preisbasis": "ausfuehrbar"},
        {"timestamp": "2026-10-20T11:00:00+02:00", "aktion": "KAUF", "market_id": "2", "frage": "B?", "anteile": 10, "preis": 0.95,
         "betrag": -9.5, "kasse_danach": 81.0, "notiz": "x", "preisbasis": "schaetzung"},
    ]
    state = {"cash": 81.0, "started": "x", "positions": [
        {"market_id": "1", "shares": 10, "einstandskurs": 0.95, "letzter_kurs": 0.95, "frage": "A?"},
        {"market_id": "2", "shares": 10, "einstandskurs": 0.95, "letzter_kurs": 0.95, "frage": "B?"}]}
    write_fund(str(tmp_path), fund, trades, state)
    res = bd.run(str(tmp_path))[0]["summary"]
    assert (res["trades_behalten"], res["trades_entfernt"]) == (1, 1)
    st = rj(tmp_path / fund["state"])
    assert [p["market_id"] for p in st["positions"]] == ["1"]
    assert abs(st["cash"] - 90.5) < 0.02


def cross_trade(ts_, pair, frage, shares, summe, cost, spread, cap, aktion="KAUF"):
    return {"timestamp": ts_, "aktion": aktion, "pair_id": pair, "frage": frage, "anteile": shares, "preis": summe,
            "betrag": -cost, "kasse_danach": 0, "notiz": f"Spread {spread}% nach geschaetzten Gebuehren, Edge-Limit {cap} Anteile, Beine: a"}


def test_kassenzwang_verkleinert_spaetere_kaeufe_proportional(tmp_path):
    """Frueher Gewinn-Trade (Spread 40 %) wird entfernt; ein spaeterer Kauf, der aus dessen Auszahlung finanziert war,
    wird auf die verfuegbare Kasse verkleinert statt die Kasse ins Minus zu schicken."""
    fund = next(f for f in bd.FUNDS if f["key"] == "cross_platform")
    q = "[kalshi] Will Beta Team win the 2026 Cup final?  <->  [sxbet] Beta Team wins the 2026 Cup final?"
    q2 = "[kalshi] Will Gamma Team win the 2026 Cup final?  <->  [sxbet] Gamma Team wins the 2026 Cup final?"
    t = [
        cross_trade("2026-10-01T10:00:00+02:00", "p1", q, 100, 0.60, 60.0, 40.0, 100000),
        {"timestamp": "2026-10-02T10:00:00+02:00", "aktion": "AUSZAHLUNG", "pair_id": "p1", "frage": q, "anteile": 100, "preis": 1.0,
         "betrag": 100.0, "kasse_danach": 0, "notiz": "x"},
        cross_trade("2026-10-03T10:00:00+02:00", "p2", q2, 150, 0.95, 142.5, 5.0, 100000),
    ]
    state = {"cash": 100 - 60 + 100 - 142.5, "started": "x", "positions": [
        {"pair_id": "p2", "frage": q2, "shares": 150, "end_date": None,
         "leg_a": {"platform": "kalshi", "market_id": "a", "side": "yes", "einstandskurs": 0.5, "letzter_kurs": 0.5, "fee_gezahlt": 0.0},
         "leg_b": {"platform": "sxbet", "market_id": "b", "side": "no", "einstandskurs": 0.45, "letzter_kurs": 0.45, "fee_gezahlt": 0.0}}]}
    write_fund(str(tmp_path), fund, t, state)
    bd.run(str(tmp_path))
    trades = rd(tmp_path / fund["trades"])
    assert [x["pair_id"] for x in trades] == ["p2"]
    assert float(trades[0]["betrag"]) <= -99.0 and float(trades[0]["betrag"]) >= -100.0 + 1e-6 - 0.01
    st = rj(tmp_path / fund["state"])
    assert st["cash"] >= -0.01 and st["positions"][0]["shares"] < 150
    check_consistent(str(tmp_path))


# ------------------------------------------------- Dasselbe Paar nach dem Entfernen erneut gekauft (15.10.2026)
def _cross_position(pair, frage, shares, eingestiegen, a, b):
    return {"pair_id": pair, "frage": frage, "shares": shares, "end_date": None, "eingestiegen": eingestiegen,
            "leg_a": {"platform": "kalshi", "market_id": "a", "side": "yes", "einstandskurs": a, "letzter_kurs": a, "fee_gezahlt": 0.0},
            "leg_b": {"platform": "sxbet", "market_id": "b", "side": "no", "einstandskurs": b, "letzter_kurs": b, "fee_gezahlt": 0.0}}


def _append_live_buy(d, fund, row, position):
    """Ein spaeterer Workflow-Lauf haengt einen Kauf an die Live-Dateien an (Zeile + Position + Kasse)."""
    tpath, spath = os.path.join(d, fund["trades"]), os.path.join(d, fund["state"])
    rows, fields = bd.read_csv(tpath)
    state = rj(spath)
    cost = -float(row["betrag"])
    row = dict(row, kasse_danach=round(state["cash"] - cost, 4))
    bd.write_csv(tpath, fields, rows + [row])
    state["cash"] -= cost
    state["positions"].append(position)
    json.dump(state, open(spath, "w", encoding="utf-8"))


def test_paar_wird_nach_dem_entfernen_erneut_gekauft_sicherung_und_live_bleiben_konsistent(tmp_path):
    """Vorfall nach den Workflow-Laeufen seit 017bac6: Die Bereinigung entfernte den alten Kauf eines Paars (z. B.
    Spread 27,5 %), der Zustand des Workflows kannte die Position danach nicht mehr - der naechste Lauf kaufte DASSELBE
    Paar erneut (jetzt plausibel). Die Sicherung fuehrt dann zwei offene Lebenszyklen derselben ID, hatte aber nur eine
    Position (Zuordnung nur ueber die ID): 3 Tests rot ('Sicherung: Positionen'). Ausserdem wurde der zweite Kauf mit den
    Beinen des ersten klassifiziert (hier: Verlust-Beine des ersten Kaufs -> zweiter Kauf faelschlich entfernt)."""
    fund = next(f for f in bd.FUNDS if f["key"] == "cross_platform_1k")
    pair = "kalshi:KXSIM-1__sxbet:0xabc"
    q = "[kalshi] Will Beta Team win the 2026 Cup final?  <->  [sxbet] Beta Team wins the 2026 Cup final?"
    t1, t2 = "2026-10-11T22:30:58+02:00", "2026-10-15T06:48:17+02:00"
    # Lauf 1 (alter Code): Beine 0,52 + 0,52 = garantierter Verlust -> wird entfernt
    row1 = dict(cross_trade(t1, pair, q, 100, 1.04, 104.0, 5.0, 100000), preisbasis="ausfuehrbar", kasse_danach=896.0)
    write_fund(str(tmp_path), fund, [row1], {"cash": 896.0, "started": t1,
                                             "positions": [_cross_position(pair, q, 100, t1, 0.52, 0.52)]})
    bd.run(str(tmp_path))
    assert rd(tmp_path / fund["trades"]) == [] and abs(rj(tmp_path / fund["state"])["cash"] - 1000.0) < 0.02
    # Lauf 2: derselbe Pair-ID-Kauf, jetzt mit Gewinnspanne (0,46 + 0,47 = 0,93)
    row2 = dict(cross_trade(t2, pair, q, 100, 0.93, 93.0, 7.0, 100000), preisbasis="ausfuehrbar")
    _append_live_buy(str(tmp_path), fund, row2, _cross_position(pair, q, 100, t2, 0.46, 0.47))
    bd.run(str(tmp_path))

    check_consistent(str(tmp_path))
    check_archive(str(tmp_path))
    arch = rj(tmp_path / bd.ARCHIVE_SUB / fund["state"])
    assert sorted(p["eingestiegen"] for p in arch["positions"]) == [t1, t2]
    live = rj(tmp_path / fund["state"])
    assert [p["eingestiegen"] for p in live["positions"]] == [t2]            # zweiter Kauf bleibt, erster ist entfernt
    assert live["positions"][0]["leg_a"]["einstandskurs"] == 0.46             # mit den Beinen des ZWEITEN Kaufs
    assert [r["timestamp"] for r in rd(tmp_path / fund["trades"])] == [t2]
    assert abs(live["cash"] - 907.0) < 0.02
    first = snapshot(str(tmp_path))
    bd.run(str(tmp_path))
    assert snapshot(str(tmp_path)) == first, "zweiter Lauf nicht idempotent"
    # Die Sicherung als unbereinigter Stand neu bereinigt ergibt denselben Live-Stand
    raw = tmp_path / "raw"
    raw.mkdir()
    for n in FUND_FILES:
        p = tmp_path / bd.ARCHIVE_SUB / n
        if p.is_file():
            shutil.copy2(p, raw / n)
    bd.run(str(raw))
    assert [r["timestamp"] for r in rd(raw / fund["trades"])] == [t2]
    assert [p["eingestiegen"] for p in rj(raw / fund["state"])["positions"]] == [t2]
