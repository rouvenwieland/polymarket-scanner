import sqlite3
from datetime import datetime, timezone

import pricing


def mk_db(tmp_path, bid, ask, alter_s=0, token="111"):
    from datetime import timedelta
    p = tmp_path / "kurs.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE kurs (quelle, instrument, zeit_utc, bid, ask, last, bid_menge, ask_menge, ref)")
    z = (datetime.now(timezone.utc) - timedelta(seconds=alter_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c.execute("INSERT INTO kurs VALUES ('polymarket',?,?,?,?,NULL,10,10,NULL)", (token, z, bid, ask))
    c.commit()
    c.close()
    return str(p)


MARKT = {"outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]', "clobTokenIds": '["111","222"]',
         "bestBid": "0.40", "bestAsk": "0.45"}


def test_live_kurs_ersetzt_gamma(tmp_path, monkeypatch):
    monkeypatch.setenv("KURS_DB", mk_db(tmp_path, 0.30, 0.32))
    q = pricing.buy_quotes(MARKT)
    assert (q.yes, round(q.no, 4), q.basis) == (0.32, 0.70, pricing.BASIS_EXECUTABLE)
    assert pricing.sell_quotes(MARKT).yes == 0.30


def test_veralteter_oder_fehlender_live_kurs_laesst_gamma(tmp_path, monkeypatch):
    monkeypatch.setenv("KURS_DB", mk_db(tmp_path, 0.30, 0.32, alter_s=120))
    assert pricing.buy_quotes(MARKT).yes == 0.45
    monkeypatch.setenv("KURS_DB", str(tmp_path / "nix.db"))
    assert pricing.buy_quotes(MARKT).yes == 0.45


def test_unplausibler_live_kurs_wird_verworfen(tmp_path, monkeypatch):
    monkeypatch.setenv("KURS_DB", mk_db(tmp_path, 0.60, 0.50))  # bid > ask
    assert pricing.buy_quotes(MARKT).basis == pricing.BASIS_ESTIMATE


def test_nicht_yes_zuerst_nutzt_kein_live_buch(tmp_path, monkeypatch):
    monkeypatch.setenv("KURS_DB", mk_db(tmp_path, 0.30, 0.32))
    m = dict(MARKT, outcomes='["No","Yes"]')
    assert pricing.buy_quotes(m).basis == pricing.BASIS_ESTIMATE
