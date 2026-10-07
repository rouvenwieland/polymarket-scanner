"""
Finale KI-Prüfstufe für Cross-Platform-Matches (kostenlos über OpenRouter)
==============================================================================

Letzte Sicherheitsstufe VOR dem tatsächlichen Kauf einer Cross-Platform-
Position: cross_platform.py findet Kandidaten über Textähnlichkeit/
Embeddings + harte Gegenbeweise (siehe dort) - das ist Mustererkennung,
kein echtes Verständnis der Frage. Diese Stufe schickt JEDEN Kandidaten
zusätzlich an ein KI-Modell mit der expliziten Frage, ob beide Seiten
GENAU dasselbe Realwelt-Ereignis mit IDENTISCHEN Auflösungskriterien
beschreiben - und verlangt eine begründete Ja/Nein-Antwort, bevor ein Fonds
die Position kauft.

Nutzt OpenRouters kostenlose Modelle (":free"-Suffix, eigener API-Key des
Nutzers über den Secret OPENROUTER_API_KEY) statt der bezahlten Claude-API -
das hält die Kosten bei $0, siehe Nutzer-Entscheidung. ACHTUNG: die freien
OpenRouter-Modelle sind strikt limitiert (nur 20 Anfragen/Minute, 50/Tag
ohne jemals mindestens 10 USD Guthaben gekauft zu haben - siehe
docs.openrouter.ai) UND wechseln/verschwinden regelmäßig aus dem freien
Katalog. Deshalb:
  - EIN API-Aufruf prüft IMMER mehrere Kandidaten auf einmal (Batch), nie
    einen einzelnen - das Anfrage-Budget ist die knappe Ressource, nicht
    die Tokenzahl.
  - Ergebnisse werden PERMANENT gecacht (data/llm_verification_cache.json,
    vom selben GitHub-Actions-Commit wie die anderen data/-Dateien erfasst) -
    derselbe Markt-Paar wird nie zweimal angefragt.
  - Ein fester Tageszähler (data/llm_verification_budget.json) deckelt die
    tatsächlich genutzten Anfragen konservativ unter dem offiziellen Limit.
  - Mehrere Modelle als Fallback-Liste, falls eines gerade überlastet/
    nicht mehr kostenlos ist.
  - Bei JEDEM Fehler (kein Key, Budget erschöpft, alle Modelle down,
    unparsebare Antwort) gilt FAIL-CLOSED: die Position bleibt unverifiziert
    und wird NICHT gekauft, statt im Zweifel durchzulassen - genau das war
    bisher zweimal die Fehlerquelle (nicht diese Stufe selbst, sondern ihr
    Fehlen).
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# Reihenfolge = Priorität. Freie Modelle wechseln häufig (siehe Moduldoku) -
# diese Liste braucht vermutlich gelegentliche Pflege, siehe openrouter.ai/models?max_price=0.
FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "cohere/north-mini-code:free",
]
MAX_PAIRS_PER_REQUEST = 15  # Prompt-Länge/Antwort-Zuverlässigkeit vs. Anfrage-Budget
DAILY_REQUEST_BUDGET = 35   # konservativ unter dem offiziellen 50/Tag-Limit (siehe Moduldoku)
REQUEST_TIMEOUT = 45

CACHE_PATH = os.path.join("data", "llm_verification_cache.json")
BUDGET_PATH = os.path.join("data", "llm_verification_budget.json")

_SYSTEM_PROMPT = """Du prüfst Paare von Prognosemarkt-Fragen von zwei verschiedenen Plattformen \
(Polymarket, Kalshi, PredictIt, SX Bet). Für jedes Paar: gilt "Ja" nur, wenn beide Fragen bei \
GLEICHEM Ausgang des realen Ereignisses GARANTIERT exakt gegensätzlich aufgelöst werden - \
d.h. Kaufen von "Yes" auf der einen Seite und "No" auf der anderen zahlt immer genau 1.00 \
(unabhängig vom Ausgang), weil es sich um EXAKT dieselbe Frage handelt.

Antworte "Nein", wenn IRGENDEINER dieser Unterschiede vorliegt, auch wenn Thema/Teams/Wortlaut \
ähnlich klingen:
- Unterschiedliche Kandidaten/Teilnehmer in einem Mehrkandidaten-Rennen (z.B. Kandidat A vs. \
  Kandidat B in DERSELBEN Wahl - Match nur, wenn es wortgleich derselbe Kandidat ist).
- Unterschiedliche WETTART beim selben Spiel/Ereignis (z.B. "wer gewinnt das Spiel" ist NICHT \
  dasselbe wie "wer gewinnt die 1. Halbzeit/das 7. Inning", "Endstand exakt X:Y", "Handicap/Spread", \
  "beide Teams treffen", "wer trifft zuerst", "geht es in die Verlängerung").
- Unterschiedliche Jahre, Fristen oder Gebietskörperschaften (Wahlkreis, Bundesstaat, Land).
- Eine Seite fragt nach einer GESAMT-Anzahl/Schwelle (z.B. "mind. 50 Sitze"), die andere nach dem \
  Ausgang EINES einzelnen Rennens.
- Eine Seite ist deutlich allgemeiner/generischer formuliert und nennt keine der konkreten \
  Entitäten (Namen/Teams), die die andere Seite nennt.

Antworte NUR mit kompaktem JSON: {"results": [{"id": <id>, "same_event": true|false, "reason": "<kurz>"}]} \
- ein Eintrag pro Paar, in der Reihenfolge/mit der ID der Eingabe. Kein weiterer Text."""


def _load_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _pair_key(market_a, market_b):
    a = f"{market_a['platform']}:{market_a['market_id']}"
    b = f"{market_b['platform']}:{market_b['market_id']}"
    return "__".join(sorted([a, b]))


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_budget():
    budget = _load_json(BUDGET_PATH, {"date": _today_str(), "used": 0})
    if budget.get("date") != _today_str():
        budget = {"date": _today_str(), "used": 0}
    return budget


def _call_model(model, pairs_payload):
    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    }
    user_msg = "Paare (JSON):\n" + json.dumps(pairs_payload, ensure_ascii=False)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
    }
    resp = requests.post(API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    # Manche freien Modelle umschließen JSON trotz Anweisung mit Codeblock-Markern.
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"Keine JSON-Antwort erkennbar: {content[:200]!r}")
    parsed = json.loads(match.group(0))
    return parsed.get("results", [])


def verify_candidates(candidates):
    """candidates: Liste von (market_a, market_b) Tupeln. Gibt dict
    pair_key -> True/False/None zurück (None = nicht verifizierbar - siehe
    Moduldoku, FAIL-CLOSED: vom Aufrufer wie False zu behandeln, also NICHT
    kaufen, aber beim nächsten Lauf erneut versuchen statt dauerhaft
    auszuschließen)."""
    cache = _load_json(CACHE_PATH, {})
    results = {}
    to_check = []
    for ma, mb in candidates:
        key = _pair_key(ma, mb)
        if key in cache:
            results[key] = cache[key]["same_event"]
        else:
            to_check.append((key, ma, mb))

    if not to_check:
        return results

    if "OPENROUTER_API_KEY" not in os.environ or not os.environ["OPENROUTER_API_KEY"]:
        print("[Hinweis] OPENROUTER_API_KEY nicht gesetzt - KI-Prüfstufe deaktiviert, "
              f"{len(to_check)} Kandidat(en) bleiben unverifiziert (kein Kauf).")
        for key, _, _ in to_check:
            results[key] = None
        return results

    budget = _load_budget()
    now_iso = datetime.now(timezone.utc).isoformat()

    for batch_start in range(0, len(to_check), MAX_PAIRS_PER_REQUEST):
        batch = to_check[batch_start:batch_start + MAX_PAIRS_PER_REQUEST]
        if budget["used"] >= DAILY_REQUEST_BUDGET:
            print(f"[Hinweis] Tages-Budget für die KI-Prüfstufe erschöpft ({DAILY_REQUEST_BUDGET}) - "
                  f"{len(batch)} verbleibende Kandidat(en) bleiben unverifiziert (kein Kauf).")
            for key, _, _ in batch:
                results[key] = None
            continue

        pairs_payload = [
            {"id": i, "frage_a": ma["question"], "frage_b": mb["question"]}
            for i, (_, ma, mb) in enumerate(batch)
        ]

        parsed_results = None
        last_error = None
        for model in FREE_MODELS:
            try:
                parsed_results = _call_model(model, pairs_payload)
                break
            except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
                last_error = e
                continue

        budget["used"] += 1  # zählt auch Fehlversuche - jeder Aufruf verbraucht Budget beim Anbieter

        if parsed_results is None:
            print(f"[Warnung] KI-Prüfstufe: alle Modelle fehlgeschlagen ({last_error}) - "
                  f"{len(batch)} Kandidat(en) bleiben unverifiziert (kein Kauf).")
            for key, _, _ in batch:
                results[key] = None
            continue

        verdicts_by_id = {r.get("id"): r for r in parsed_results if isinstance(r, dict)}
        for i, (key, ma, mb) in enumerate(batch):
            verdict = verdicts_by_id.get(i)
            if verdict is None or "same_event" not in verdict:
                results[key] = None
                continue
            same_event = bool(verdict["same_event"])
            results[key] = same_event
            cache[key] = {
                "same_event": same_event,
                "reason": verdict.get("reason", ""),
                "checked_at": now_iso,
                "frage_a": ma["question"],
                "frage_b": mb["question"],
            }

    _save_json(BUDGET_PATH, budget)
    _save_json(CACHE_PATH, cache)
    return results
