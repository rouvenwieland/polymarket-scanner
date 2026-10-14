"""polymarket_snapshot: Teil-Abruf, Zeitumstellung, Dauer-Schaetzung, Summary."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import fund_simulator
import polymarket_snapshot as ps

BERLIN = ZoneInfo("Europe/Berlin")
FIELDS = ["timestamp", "market_id", "frage", "slug", "yes_preis", "no_preis", "summe", "volumen", "liquiditaet", "end_date"]


def write_snapshots(path, stamps):
    rows = [{"timestamp": ts.isoformat(), "market_id": "1", "frage": "F?", "slug": "f", "yes_preis": 0.4,
             "no_preis": 0.5, "summe": 0.9, "volumen": 10, "liquiditaet": 5, "end_date": ""} for ts in stamps]
    pd.DataFrame(rows, columns=FIELDS).to_csv(path, index=False)


def test_summary_ueberlebt_zeitumstellung_gemischte_utc_offsets(datadir):
    """Europe/Berlin wechselt am 25.10.2026 von +02:00 auf +01:00. snapshots.csv enthaelt dann gemischte
    Offsets; pandas liefert ohne utc=True eine object-Spalte und .strftime scheitert -> der ganze
    Snapshot-Lauf (inkl. Fonds-Schritt) bricht ab."""
    before = datetime(2026, 10, 25, 1, 30, tzinfo=BERLIN)
    after = datetime(2026, 10, 25, 4, 30, tzinfo=BERLIN)
    assert before.utcoffset() != after.utcoffset()
    write_snapshots(ps.SNAPSHOTS_CSV, [before, after])
    ps.rebuild_summary(BERLIN)
    out = pd.read_csv(ps.SUMMARY_CSV)
    assert len(out) == 1
    assert out.loc[0, "Erster Treffer"] == "2026-10-25 01:30"
    assert out.loc[0, "Letzter Treffer"] == "2026-10-25 04:30"


def test_dauer_nutzt_tatsaechlichen_abstand_der_laeufe(datadir):
    """Die Schaetzung 'Treffer * 15 min' stimmt nur, wenn der Cron wirklich alle 15 min laeuft. Real liegen
    die Laeufe ~4-5 h auseinander (GitHub drosselt Schedules): 6 Treffer im 4-h-Abstand = ~24 h, nicht 90 min."""
    base = datetime(2026, 10, 1, 0, 0, tzinfo=BERLIN)
    write_snapshots(ps.SNAPSHOTS_CSV, [base + timedelta(hours=4 * i) for i in range(6)])
    ps.rebuild_summary(BERLIN)
    out = pd.read_csv(ps.SUMMARY_CSV)
    assert out.loc[0, "Geschaetzte Dauer unter Schwelle"] == "1d 0min"  # 6 * 4 h = 24 h


def test_format_timedelta_randfaelle():
    assert ps.format_timedelta(None) == "-"
    assert ps.format_timedelta(timedelta(minutes=0)) == "0min"
    assert ps.format_timedelta(timedelta(minutes=-90)) == "-1h 30min"
    assert ps.format_timedelta(timedelta(days=2, hours=3, minutes=4)) == "2d 3h 4min"


class FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_teil_abruf_wird_als_unvollstaendig_gemeldet(monkeypatch):
    pages = [FakeResp({"markets": [{"id": "1", "active": True}], "next_cursor": "c1"})]

    def fake_get(url, params=None, timeout=None):
        if pages:
            return pages.pop(0)
        raise requests.ConnectionError("netz weg")

    monkeypatch.setattr(requests, "get", fake_get)
    markets, complete = ps.fetch_all_open_markets_checked()
    assert [m["id"] for m in markets] == ["1"]
    assert complete is False


def test_vollstaendiger_abruf_wird_als_vollstaendig_gemeldet(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp({"markets": [{"id": "1", "active": True}], "next_cursor": None}))
    markets, complete = ps.fetch_all_open_markets_checked()
    assert complete is True and len(markets) == 1


def test_unvollstaendiger_abruf_bucht_keine_phantom_auszahlung(datadir, monkeypatch):
    """Bricht die Pagination mittendrin ab, fehlen offene Positions-Maerkte in der Liste. Der Fonds behandelt
    'nicht auffindbar' als aufgeloest und schreibt 1,00 USD/Paar gut -> Kapital wird zu frueh frei.
    Bei unvollstaendigem Abruf muss der Fonds-Schritt uebersprungen werden."""
    profile = fund_simulator.PROFILES["aggressive"]
    state = {"cash": 50.0, "started": "x", "positions": [{
        "market_id": "42", "frage": "F?", "slug": "f", "shares": 50.0, "einstandskurs": 0.9, "letzter_kurs": 0.9,
        "liquiditaet": 100.0, "end_date": "2027-01-01T00:00:00Z", "eingestiegen": "x"}]}
    fund_simulator.save_state(profile, state)
    monkeypatch.setattr(ps, "fetch_all_open_markets_checked", lambda *a, **k: ([], False))
    ps.main()
    after = fund_simulator.load_state(profile)
    assert after["cash"] == 50.0
    assert len(after["positions"]) == 1
