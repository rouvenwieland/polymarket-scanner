"""KI-Pruefstufe (llm_verify): Fail-closed-Garantie, Budget- und Cache-Logik. Nur gemockt, nie Netz."""
import json

import pytest

import llm_verify

MA = {"platform": "polymarket", "market_id": "1", "question": "Will A win?"}
MB = {"platform": "kalshi", "market_id": "K1", "question": "A to win"}
KEY = llm_verify._pair_key(MA, MB)


@pytest.fixture
def api(monkeypatch, datadir):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-dummy")  # nur Platzhalter, wird nie gesendet
    return datadir


def test_pair_key_ist_symmetrisch():
    assert llm_verify._pair_key(MA, MB) == llm_verify._pair_key(MB, MA)


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("false", False), ("False", False),
    ("true", True), ("True", True),
    (None, None), ("maybe", None), ("", None), (0, None), (1, None), ("no", None), ("nein", None), ("0", None),
])
def test_same_event_wird_streng_gelesen(api, monkeypatch, raw, expected):
    """Ein Modell, das "false" als STRING liefert, darf nicht als verifiziert (True) enden:
    bool("false") ist in Python True -> der Kauf wuerde freigegeben (fail-open)."""
    monkeypatch.setattr(llm_verify, "_call_model", lambda m, p: [{"id": 0, "same_event": raw, "reason": "x"}])
    res = llm_verify.verify_candidates([(MA, MB)])
    assert res[KEY] is expected
    cache = json.load(open(llm_verify.CACHE_PATH)) if (api / "llm_verification_cache.json").exists() else {}
    assert (KEY in cache) == (expected is not None)  # nicht verifizierbar -> nicht dauerhaft gecacht


def test_ohne_key_fail_closed(datadir, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: None}


def test_budget_zaehlt_jeden_tatsaechlichen_request(api, monkeypatch):
    """Fallback ueber mehrere Modelle = mehrere echte Requests. Das Budget (Schutz vor dem
    Anbieter-Limit von 50/Tag) muss jeden zaehlen, nicht nur 1 pro Batch."""
    calls = []

    def fake(model, payload):
        calls.append(model)
        if len(calls) < 4:
            raise ValueError("Modell kaputt")
        return [{"id": 0, "same_event": True, "reason": "ok"}]

    monkeypatch.setattr(llm_verify, "_call_model", fake)
    llm_verify.verify_candidates([(MA, MB)])
    used = json.load(open(llm_verify.BUDGET_PATH))["used"]
    assert len(calls) == 4
    assert used == 4


def test_budget_wird_nach_jedem_batch_gespeichert(api, monkeypatch):
    """Bricht der Lauf mitten drin ab (Timeout/Absturz), duerfen bereits verbrauchte Requests und
    Urteile nicht verloren gehen (sonst wird das Tageslimit des Anbieters real ueberschritten)."""
    pairs = [({"platform": "polymarket", "market_id": str(i), "question": f"q{i}"},
              {"platform": "kalshi", "market_id": f"K{i}", "question": f"q{i}"}) for i in range(40)]
    state = {"n": 0}

    def fake(model, payload):
        state["n"] += 1
        if state["n"] == 2:
            raise KeyboardInterrupt  # simulierter Abbruch (BaseException wird nicht abgefangen)
        return [{"id": r["id"], "same_event": False, "reason": ""} for r in payload]

    monkeypatch.setattr(llm_verify, "_call_model", fake)
    with pytest.raises(KeyboardInterrupt):
        llm_verify.verify_candidates(pairs)
    saved_budget = json.load(open(llm_verify.BUDGET_PATH))["used"]
    saved_cache = json.load(open(llm_verify.CACHE_PATH))
    assert saved_budget >= 1
    assert len(saved_cache) == 15


def test_cache_wird_wiederverwendet_und_kein_request(api, monkeypatch):
    monkeypatch.setattr(llm_verify, "_call_model", lambda m, p: [{"id": 0, "same_event": True, "reason": "r"}])
    llm_verify.verify_candidates([(MA, MB)])
    monkeypatch.setattr(llm_verify, "_call_model", lambda m, p: pytest.fail("Cache-Treffer darf keinen Request ausloesen"))
    assert llm_verify.verify_candidates([(MB, MA)]) == {KEY: True}


def test_budget_erschoepft_blockiert_und_fail_closed(api, monkeypatch):
    (api / "llm_verification_budget.json").write_text(json.dumps({"date": llm_verify._today_str(), "used": llm_verify.DAILY_REQUEST_BUDGET}))
    monkeypatch.setattr(llm_verify, "_call_model", lambda m, p: pytest.fail("kein Request bei leerem Budget"))
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: None}
