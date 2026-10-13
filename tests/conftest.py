"""Gemeinsame Test-Konfiguration: scripts/ importierbar machen, DATA_DIR je Test isolieren.
Alle Tests laufen offline (kein Netz, kein API-Key)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Jeder versehentliche HTTP-Aufruf soll laut scheitern statt das Netz zu nutzen."""
    import requests

    def _boom(*a, **k):
        raise AssertionError("Netzwerkzugriff im Test: " + repr(a[:1]))

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(requests.Session, "request", _boom)


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    """Leitet alle Module, die DATA_DIR/CSV-Pfade kennen, in ein Temp-Verzeichnis um."""
    import fund_simulator
    import llm_verify
    import polymarket_snapshot

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(fund_simulator, "DATA_DIR", str(d))
    monkeypatch.setattr(polymarket_snapshot, "DATA_DIR", str(d))
    monkeypatch.setattr(polymarket_snapshot, "SNAPSHOTS_CSV", str(d / "snapshots.csv"))
    monkeypatch.setattr(polymarket_snapshot, "SUMMARY_CSV", str(d / "summary.csv"))
    monkeypatch.setattr(polymarket_snapshot, "LAST_RUN_STATS_JSON", str(d / "last_run_stats.json"))
    monkeypatch.setattr(llm_verify, "CACHE_PATH", str(d / "llm_verification_cache.json"))
    monkeypatch.setattr(llm_verify, "BUDGET_PATH", str(d / "llm_verification_budget.json"))
    return d
