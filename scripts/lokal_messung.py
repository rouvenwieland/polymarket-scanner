"""Guetemessung des lokalen Modells gegen die Referenzurteile in data/llm_verification_cache.json.
Aufruf (auf dem Server, lokales Modell erreichbar): python scripts/lokal_messung.py [anzahl_je_klasse] [ausgabe.json]
Zieht je Klasse (ja/nein) zufaellig anzahl Faelle (Seed fest), prueft sie ohne Cache ueber das lokale Modell und
schreibt Referenz + lokales Urteil + Fragen je Fall. Auswertung/Tabelle: docs/lokales-modell-vergleich.md."""
import json
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["LLM_BACKEND"] = "local"
import llm_verify  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
out = sys.argv[2] if len(sys.argv) > 2 else "lokal_messung.json"
ref = json.load(open(os.path.join("data", "llm_verification_cache.json"), encoding="utf-8"))
rnd = random.Random(20261113)
ja = sorted(k for k, v in ref.items() if v["same_event"] is True)
nein = sorted(k for k, v in ref.items() if v["same_event"] is False)
sample = rnd.sample(ja, min(n, len(ja))) + rnd.sample(nein, n)
llm_verify.CACHE_PATH = os.path.join(tempfile.mkdtemp(), "cache.json")
cands, keys = [], []
for k in sample:
    ma = {"platform": "x", "market_id": k + "#a", "question": ref[k]["frage_a"]}
    mb = {"platform": "y", "market_id": k + "#b", "question": ref[k]["frage_b"]}
    cands.append((ma, mb))
    keys.append((k, llm_verify._pair_key(ma, mb)))
t = time.time()
res = llm_verify.verify_candidates(cands)
rows = [{"key": k, "referenz": ref[k]["same_event"], "lokal": res[pk], "frage_a": ref[k]["frage_a"],
         "frage_b": ref[k]["frage_b"], "grund_ref": ref[k].get("reason", "")} for k, pk in keys]
json.dump({"sekunden": time.time() - t, "rows": rows}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("fertig", len(rows), "Sekunden", round(time.time() - t))
