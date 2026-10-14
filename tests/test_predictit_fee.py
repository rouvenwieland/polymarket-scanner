"""PredictIt-Gewinngebuehr: je gewinnendem Bein, ohne Verlustverrechnung (vorsichtige Annahme, Quelle in predictit.py)."""
from datetime import datetime, timedelta, timezone

import pytest

import fund_simulator
import single_platform_fund as spf
from platforms import predictit

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
END = (NOW + timedelta(days=5)).isoformat()


def test_pair_fee_je_bein_ohne_verlustverrechnung():
    # Yes gewinnt: 1,00 - 0,45 = 0,55 Gewinn -> 0,055; No verliert (kein Abzug)
    assert predictit.pair_fee(1.0, 0.0, 0.45, 0.50) == pytest.approx(0.055)
    assert predictit.pair_fee(0.0, 1.0, 0.45, 0.50) == pytest.approx(0.05)
    assert predictit.worst_case_settle_fee(0.45, 0.50) == pytest.approx(0.055)


def test_auszahlungsgebuehr_ist_abgeschafft():
    assert predictit.FEE_RATE_WITHDRAWAL == 0.0


def mk(yes, no):
    return {"platform": "predictit", "market_id": "1:1", "question": "Q?", "yes_price": yes, "no_price": no,
            "liquidity": 0.0, "end_date": END}


def test_auswahl_beruecksichtigt_schlechtesten_gebuehrenfall(datadir):
    profile = spf.PROFILES["predictit_only"]
    state = spf._empty_state(profile)
    # Summe 0,95: "5 % Spread" nominal, aber Gewinnbein-Gebuehr 5,5 Cent -> in einem Ausgang Verlust
    spf._select_new_trades(profile, state, [mk(0.45, 0.50)], NOW)
    assert state["positions"] == []
    spf._select_new_trades(profile, state, [mk(0.30, 0.50)], NOW)  # Summe 0,80, schlechtester Fall +0,13
    assert len(state["positions"]) == 1
    assert state["positions"][0]["yes_price"] == 0.30


def test_auszahlung_zieht_schlechtesten_fall_ab(datadir):
    profile = spf.PROFILES["predictit_only"]
    pos = {"market_id": "1:1", "frage": "Q", "shares": 100.0, "einstandskurs": 0.95, "yes_price": 0.45,
           "no_price": 0.50, "letzter_kurs": 0.95, "liquiditaet": 0.0, "end_date": END}
    state = {"cash": 0.0, "positions": [pos], "started": "x"}
    closed = spf._apply_decision(profile, state, pos, {"action": "settle", "proceeds_per_share": 1.0}, NOW)
    assert closed
    assert state["cash"] == pytest.approx(100.0 - 5.5)   # frueher (netto): 99,50


def test_alte_position_ohne_beinpreise_faellt_auf_netto_rechnung_zurueck():
    pos = {"einstandskurs": 0.95}
    assert spf._settle_fee("predictit", pos, 1.0, "settle") == pytest.approx(0.005)


def test_andere_plattformen_zahlen_keine_gewinngebuehr():
    assert spf._settle_fee("kalshi", {"einstandskurs": 0.9, "yes_price": .4, "no_price": .5}, 1.0, "settle") == 0.0
