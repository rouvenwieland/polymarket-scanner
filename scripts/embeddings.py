"""
Lokales Embedding-Modell für semantisches Matching (kostenlos)
==================================================================

Ersatz/Ergänzung für den in cross_platform.py beschriebenen Jaccard-/
Containment-Ansatz: wandelt Markt-Fragen in Vektoren um, die BEDEUTUNG statt
reiner Wortüberlappung erfassen - fängt z.B. "Will the Fed cut rates?" vs.
"Federal Reserve interest rate decision" (kein gemeinsames informatives
Token, aber dieselbe Frage).

Bewusst KEINE externe API (OpenAI/Cohere/etc.) - das wäre laufende Kosten,
ein API-Key in GitHub Actions und zusätzliche Latenz pro Lauf. Stattdessen
läuft ein kleines Modell lokal im Runner: dieses Repo ist öffentlich, GitHub
Actions-Minuten sind für öffentliche Repos unbegrenzt/kostenlos, die einzige
"Kosten" ist zusätzliche Laufzeit (siehe Nutzer-Entscheidung).

Modell: BAAI/bge-small-en-v1.5 über fastembed (ONNX Runtime, reine CPU-
Inferenz, kein Torch/CUDA) - lokal benchmarkt mit ~350-420 Texten/Sekunde,
~200-250MB Gesamt-Installationsgröße (vs. 1GB+ für sentence-transformers,
das standardmäßig den vollen GPU-Torch-Build zieht).

Empirische Erkenntnis aus der Kalibrierung: reine Embedding-Ähnlichkeit
kann NICHT alle Fehl-Matches ausschließen (ein Jahres-Konflikt-Fall erzielte
0.918 - höher als ein echtes Match mit 0.901). Embeddings werden daher in
cross_platform.py NUR als zusätzliches Signal neben den bestehenden harten
Gegenbeweisen (_conflicting_*) verwendet, nie als deren Ersatz.
"""

MODEL_NAME = "BAAI/bge-small-en-v1.5"

_model = None
_unavailable_reason = None


def available():
    """True, wenn fastembed installiert ist und sich das Modell laden lässt.
    Lokale Tests/Dev-Umgebungen ohne die (optionale) Abhängigkeit sollen
    nicht crashen - cross_platform.py fällt dann auf reines Jaccard/
    Containment zurück, exakt wie vor dieser Erweiterung."""
    return _get_model() is not None


def _get_model():
    global _model, _unavailable_reason
    if _model is not None or _unavailable_reason is not None:
        return _model
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        _unavailable_reason = str(e)
        print(f"[Hinweis] fastembed nicht installiert, semantisches Matching deaktiviert: {e}")
        return None
    try:
        _model = TextEmbedding(model_name=MODEL_NAME)
    except Exception as e:  # Modell-Download/Ladefehler - nicht fatal für den restlichen Scan
        _unavailable_reason = str(e)
        print(f"[Warnung] Embedding-Modell konnte nicht geladen werden: {e}")
        return None
    return _model


def encode(texts):
    """Gibt eine Liste normierter Embedding-Vektoren (Länge 1, numpy-Arrays)
    zurück, oder None, falls das Modell nicht verfügbar ist. Normiert, damit
    das Skalarprodukt direkt die Kosinus-Ähnlichkeit ergibt (siehe
    cosine_sim)."""
    model = _get_model()
    if model is None:
        return None
    import numpy as np

    vectors = list(model.embed(list(texts)))
    normed = []
    for v in vectors:
        norm = np.linalg.norm(v)
        normed.append(v / norm if norm > 0 else v)
    return normed


def cosine_sim(vec_a, vec_b):
    """Kosinus-Ähnlichkeit zweier bereits normierter Vektoren (siehe encode) -
    reduziert sich dadurch auf ein einfaches Skalarprodukt."""
    import numpy as np

    return float(np.dot(vec_a, vec_b))
