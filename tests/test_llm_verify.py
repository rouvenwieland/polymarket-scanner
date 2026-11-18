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


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_GUT = {"choices": [{"message": {"content": '```json\n{"results": [{"id": 0, "same_event": true, "reason": "ok"}]}\n```'}}]}


@pytest.mark.parametrize("kaputt", [
    {"choices": [{"message": {"content": None}}]},           # 17.10.2026: TypeError in re.search -> Scan-Absturz
    {"choices": [{"message": {}}]},                          # content fehlt ganz
    {"choices": [{"message": {"content": [{"type": "text", "text": "{}"}]}}]},  # content kein String
    {"choices": []},                                         # IndexError
    {"choices": [{"message": None}]},                        # AttributeError
    {"choices": [{"message": {"content": '{"results": 5}'}}]},  # results keine Liste
], ids=["content_null", "content_fehlt", "content_liste", "choices_leer", "message_null", "results_keine_liste"])
def test_kaputte_modellantwort_weicht_auf_naechstes_modell_aus(api, monkeypatch, kaputt):
    """Eine kaputte Antwort des ersten freien Modells darf den Scan nicht abbrechen: es wird das naechste
    Modell gefragt, jeder Versuch zaehlt im Budget, und dessen Urteil gilt."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["model"])
        return _FakeResp(kaputt if len(calls) == 1 else _GUT)

    monkeypatch.setattr(llm_verify.requests, "post", fake_post)
    res = llm_verify.verify_candidates([(MA, MB)])
    assert res == {KEY: True}
    assert calls == llm_verify.FREE_MODELS[:2]
    assert json.load(open(llm_verify.BUDGET_PATH))["used"] == 2


def test_alle_modelle_liefern_null_fail_closed_ohne_absturz(api, monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["model"])
        return _FakeResp({"choices": [{"message": {"content": None}}]})

    monkeypatch.setattr(llm_verify.requests, "post", fake_post)
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: None}
    assert calls == llm_verify.FREE_MODELS


# --- Lokales Modell, Schluessel-Zugriff (13.11.2026) ---------------------------------------------------

class _LokalResp(_FakeResp):
    pass


def _lokal_antwort(inhalt):
    return {"choices": [{"message": {"content": inhalt}}]}


@pytest.fixture
def lokal(monkeypatch, datadir):
    monkeypatch.setenv("LLM_BACKEND", "local")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(llm_verify.requests, "get",
                        lambda url, timeout=None: _FakeResp({"data": [{"id": "test-modell"}]}))
    return datadir


@pytest.mark.parametrize("urteil,erwartet", [(True, True), (False, False)])
def test_lokal_gleich_ungleich_ohne_schluessel(lokal, monkeypatch, urteil, erwartet):
    gesehen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        gesehen["url"], gesehen["body"] = url, json
        return _FakeResp(_lokal_antwort(
            '{"results": [{"id": 0, "same_event": %s, "reason": "r"}]}' % ("true" if urteil else "false")))

    monkeypatch.setattr(llm_verify.requests, "post", fake_post)
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: erwartet}
    assert gesehen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert gesehen["body"]["model"] == "test-modell"  # Modell-ID aus /models, nicht fest
    assert gesehen["body"]["response_format"]["type"] == "json_schema"
    assert json.load(open(llm_verify.CACHE_PATH))[KEY]["same_event"] is erwartet
    assert not (lokal / "llm_verification_budget.json").exists()  # lokal kein Tageslimit


def test_lokal_ungueltiges_json_ist_nein_und_wird_nicht_gecacht(lokal, monkeypatch):
    monkeypatch.setattr(llm_verify.requests, "post", lambda *a, **k: _FakeResp(_lokal_antwort("das ist kein json")))
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: None}
    assert not (lokal / "llm_verification_cache.json").exists()


def test_lokal_unerreichbar_ohne_schluessel_fail_closed(lokal, monkeypatch):
    def boom(*a, **k):
        raise llm_verify.requests.ConnectionError("refused")

    monkeypatch.setattr(llm_verify.requests, "get", boom)
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: None}


def test_auto_unerreichbar_mit_schluessel_faellt_auf_openrouter_free_zurueck(lokal, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "auto")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-dummy")

    def boom(*a, **k):
        raise llm_verify.requests.ConnectionError("refused")

    monkeypatch.setattr(llm_verify.requests, "get", boom)
    modelle = []
    monkeypatch.setattr(llm_verify, "_call_model",
                        lambda m, p: modelle.append(m) or [{"id": 0, "same_event": True, "reason": "ok"}])
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: True}
    assert modelle[0].endswith(":free")


def test_auto_lokal_erreichbar_aber_ungueltig_kein_rueckfall(lokal, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "auto")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-dummy")
    monkeypatch.setattr(llm_verify.requests, "post", lambda *a, **k: _FakeResp(_lokal_antwort("{}")))
    monkeypatch.setattr(llm_verify, "_call_model", lambda m, p: pytest.fail("kein OpenRouter bei lebendem lokalem Endpunkt"))
    assert llm_verify.verify_candidates([(MA, MB)]) == {KEY: None}


def test_schluessel_aus_credential_datei(tmp_path, monkeypatch):
    (tmp_path / "openrouter").write_text("sk-test-platzhalter\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "aus-env")
    assert llm_verify.schluessel("openrouter") == "sk-test-platzhalter"  # Datei hat Vorrang


def test_schluessel_aus_env_und_fehlend(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "aus-env")
    assert llm_verify.schluessel("openrouter") == "aus-env"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert llm_verify.schluessel("openrouter") is None
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))  # Verzeichnis ohne Datei
    assert llm_verify.schluessel("openrouter") is None


def test_nicht_free_modell_bricht_ab():
    with pytest.raises(ValueError, match=":free"):
        llm_verify._call_model("openai/gpt-4o", [])


def test_alle_konfigurierten_modelle_sind_free():
    assert all(m.endswith(":free") for m in llm_verify.FREE_MODELS)
