"""Alarm fuer Positionen, die nach ihrem Enddatum offen bleiben (Lamar-Position hing 10 Tage unbemerkt)."""
import json
from datetime import datetime, timedelta, timezone

import haengende_positionen as hp

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def write(tmp_path, positions, name="fund_state_crossplatform_10k.json"):
    (tmp_path / name).write_text(json.dumps({"cash": 0, "positions": positions}), encoding="utf-8")


def test_lamar_position_loest_alarm_aus(tmp_path):
    write(tmp_path, [{"pair_id": "polymarket:5353156__sxbet:0x14d7", "frage": "Lamar vs. Southeastern Louisiana",
                      "end_date": "2026-10-17T21:00:00Z", "leg_a": {"end_date": "2026-10-17T21:00:00Z"},
                      "leg_b": {"end_date": "2026-10-17T21:00:00+00:00"}}])
    stale = hp.find_stale(str(tmp_path), NOW)
    assert len(stale) == 1 and "Lamar" in stale[0][1]
    assert hp.main(["--data", str(tmp_path)]) == 1


def test_innerhalb_der_karenz_oder_ohne_enddatum_kein_alarm(tmp_path):
    write(tmp_path, [{"pair_id": "a", "end_date": (NOW - timedelta(hours=10)).isoformat()},
                     {"pair_id": "b", "end_date": "NA"},
                     {"pair_id": "c", "end_date": None, "leg_a": {"end_date": None}, "leg_b": {}}])
    assert hp.find_stale(str(tmp_path), NOW) == []


def test_spaetestes_bein_zaehlt(tmp_path):
    """Ein Cross-Paar ist erst faellig, wenn BEIDE Beine es sind (PredictIt-Bein ohne Datum zaehlt nicht)."""
    write(tmp_path, [{"pair_id": "a", "end_date": None, "leg_a": {"end_date": "2026-10-01T00:00:00Z"},
                      "leg_b": {"end_date": "2026-11-03T00:00:00Z"}}])
    assert hp.find_stale(str(tmp_path), NOW) == []


def test_quittierte_position_bis_zum_datum_still(tmp_path):
    pos = {"pair_id": "a", "end_date": "2026-10-01T00:00:00Z", "alarm_quittiert_bis": "2026-11-01T00:00:00+00:00"}
    write(tmp_path, [pos])
    assert hp.find_stale(str(tmp_path), NOW) == []
    assert len(hp.find_stale(str(tmp_path), NOW + timedelta(days=10))) == 1


def test_naives_datum_gilt_als_utc(tmp_path):
    write(tmp_path, [{"market_id": "1", "end_date": "2026-10-23T12:00:00"}])
    assert len(hp.find_stale(str(tmp_path), NOW)) == 1        # 96 h > 72 h
    assert hp.find_stale(str(tmp_path), NOW, grace_hours=100) == []
