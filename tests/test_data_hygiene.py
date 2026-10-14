"""Datenhygiene: Groessenlimit der Treffer-Datei (GitHub 100 MiB), Dashboard liest nur den neuesten Lauf."""
import csv
import os
import shutil
import subprocess
import sys

import pytest

import multi_platform_scan as mps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_grosse_datei_rotiert_in_teildatei(tmp_path):
    base = str(tmp_path / "cross_platform_hits.csv")
    assert mps.hits_target_path(base, max_bytes=100) == base          # existiert noch nicht
    with open(base, "wb") as f:
        f.write(b"x" * 99)
    assert mps.hits_target_path(base, max_bytes=100) == base          # knapp darunter
    with open(base, "ab") as f:
        f.write(b"x")
    second = str(tmp_path / "cross_platform_hits_002.csv")
    assert mps.hits_target_path(base, max_bytes=100) == second        # voll -> naechste Teildatei
    with open(second, "wb") as f:
        f.write(b"x" * 100)
    assert mps.hits_target_path(base, max_bytes=100) == str(tmp_path / "cross_platform_hits_003.csv")


def test_echte_trefferdatei_im_repo_bleibt_unter_dem_github_limit():
    path = os.path.join(ROOT, "data", "cross_platform_hits.csv")
    if not os.path.isfile(path):
        pytest.skip("keine Daten")
    assert os.path.getsize(path) < 100 * 1024 * 1024, "GitHub lehnt Dateien >= 100 MiB beim Push ab"


def test_limit_der_aktiven_teildatei_liegt_deutlich_unter_github_grenze():
    assert mps.HITS_MAX_BYTES <= 60 * 1024 * 1024


def test_schreiben_nutzt_aktive_teildatei(tmp_path, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(mps, "DATA_DIR", str(tmp_path))
    base = str(tmp_path / "cross_platform_hits.csv")
    monkeypatch.setattr(mps, "CROSS_HITS_CSV", base)
    monkeypatch.setattr(mps, "HITS_MAX_BYTES", 10)
    with open(base, "w") as f:
        f.write("0123456789ABC")
    ma = {"platform": "polymarket", "question": "Q?", "yes_price": 0.3, "no_price": 0.7, "url": ""}
    mb = {"platform": "kalshi", "question": "Q?", "yes_price": 0.4, "no_price": 0.5, "url": ""}
    mps.write_cross_platform_hits([{"market_a": ma, "market_b": mb, "score": 0.9}], datetime(2026, 10, 14))
    assert open(base).read() == "0123456789ABC"                       # alte Datei unveraendert
    rows = list(csv.DictReader(open(tmp_path / "cross_platform_hits_002.csv", encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["summe"] == "0.8"


def test_dashboard_streamt_nur_den_neuesten_lauf(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "dashboard"))
    import render_dashboard as rd
    hdr = ["timestamp", "plattform_a", "frage_a", "plattform_b", "frage_b", "seite_a", "seite_b", "summe", "score", "url_a", "url_b"]
    def row(ts, s):
        return [ts, "polymarket", "A", "kalshi", "B", "yes", "no", str(s), "0.9", "", ""]
    with open(tmp_path / "cross_platform_hits.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(hdr)
        w.writerows([row("2026-10-25T02:30:00+02:00", 0.9), row("2026-10-25T02:40:00+02:00", 0.8)])
    with open(tmp_path / "cross_platform_hits_002.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(hdr)
        # nach der Zeitumstellung: spaeterer Lauf hat lexikografisch KLEINEREN Zeitstempel-String
        w.writerows([row("2026-10-25T02:10:00+01:00", 0.7), row("2026-10-25T02:10:00+01:00", 0.6)])
    latest = rd.load_latest_hits(str(tmp_path))
    assert [float(r["summe"]) for r in latest] == [0.6, 0.7]


def test_dashboard_rendert_mit_echten_daten_ohne_fehler(tmp_path):
    out = tmp_path / "out.html"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "dashboard", "render_dashboard.py"),
                        "--data-dir", os.path.join(ROOT, "data"), "--out", str(out)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-500:]
    assert out.stat().st_size > 10_000


def test_dashboard_kennzeichnet_ausfuehrbar_und_schaetzung(tmp_path):
    """Fixture-Datenverzeichnis: ein Fonds mit Schaetzbasis (Altbestand), einer mit ausfuehrbarer Basis."""
    import json
    sys.path.insert(0, os.path.join(ROOT, "dashboard"))
    import render_dashboard as rd
    d = tmp_path
    (d / "summary.csv").write_text("Frage,Slug,Erster Treffer,Letzter Treffer,Geschaetzte Dauer unter Schwelle,Min. Summe beobachtet,Zeit bis Terminierung,Max. Volumen (USD),Max. Liquiditaet (USD)\n", encoding="utf-8")
    tcols = "timestamp,aktion,market_id,frage,anteile,preis,betrag,kasse_danach,notiz"
    (d / "fund_trades.csv").write_text(tcols + "\n2026-10-01T00:00:00+02:00,KAUF,1,Alt?,1,0.9,-0.9,99,x\n", encoding="utf-8")
    (d / "fund_trades_aggressive.csv").write_text(tcols + ",preisbasis\n2026-10-15T00:00:00+02:00,KAUF,2,Neu?,1,0.9,-0.9,99,x,ausfuehrbar\n", encoding="utf-8")
    (d / "fund_state.json").write_text(json.dumps({"cash": 99.0, "started": "x", "positions": [
        {"market_id": "1", "frage": "Alt?", "shares": 1.0, "einstandskurs": 0.9, "letzter_kurs": 0.9, "end_date": None, "liquiditaet": 1}]}), encoding="utf-8")
    (d / "fund_state_aggressive.json").write_text(json.dumps({"cash": 99.0, "started": "x", "positions": [
        {"market_id": "2", "frage": "Neu?", "shares": 1.0, "einstandskurs": 0.9, "letzter_kurs": 0.9, "end_date": None, "liquiditaet": 1, "preisbasis": "ausfuehrbar"}]}), encoding="utf-8")
    html = rd.render(str(d))
    assert "Welche Zahl ist ausführbar?" in html
    assert "Schätzung, nicht ausführbar" in html
    assert "Nur Schätzung, nicht ausführbar:" in html
    assert "1 von 1 Käufen" in html
    assert html.count("ausführbare Preise</span>") >= 2


def test_basis_stats_altbestand_ist_schaetzung():
    sys.path.insert(0, os.path.join(ROOT, "dashboard"))
    import render_dashboard as rd
    st = rd.basis_stats({"positions": [{}, {"preisbasis": "ausfuehrbar"}], "trades": [{"aktion": "KAUF"}, {"aktion": "VERKAUF"}]})
    assert st == {"pos_total": 2, "pos_est": 1, "buy_total": 1, "buy_est": 1}
