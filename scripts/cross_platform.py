"""
Cross-Platform-Event-Matching
================================

Findet Markt-Paare auf unterschiedlichen Plattformen, die vermutlich
dasselbe Realwelt-Ereignis abbilden - es gibt keine gemeinsame ID über
Plattformgrenzen, die Fragen sind unterschiedlich formuliert
("Will X win?" vs "X to win the..."), also wird über Textähnlichkeit
(Jaccard über Tokens, nach Stoppwort-Filter) plus Enddatum-Nähe gematcht.

Blocking: statt jeden Markt mit jedem zu vergleichen (bei ~200k
Polymarket-Märkten x ein paar tausend Kalshi/PredictIt-Märkten viel zu
teuer), wird ein inverses Token-Index über die kleinere Plattform gebaut;
nur Märkte, die mindestens ein signifikantes Token teilen, werden überhaupt
verglichen.

Für ein gefundenes Match: Yes auf Plattform A + No auf Plattform B (oder
umgekehrt) zahlt garantiert genau 1.00 USD, WENN beide Plattformen dasselbe
Ereignis wirklich identisch auflösen - das ist die Kernannahme, die durch
die Ähnlichkeitsschwelle nur approximiert, nie bewiesen wird. Ein falsches
Match (ähnlicher Wortlaut, aber andere Frage/Auflösungskriterien) ist das
Hauptrisiko dieser Strategie.
"""

import re
from datetime import datetime

STOPWORDS = {
    "will", "the", "a", "an", "of", "in", "on", "for", "to", "by", "be",
    "is", "are", "or", "and", "win", "be", "at", "as", "with", "this",
    "that", "does", "do", "who", "what", "which", "before", "after",
    "than", "more", "less", "than", "have", "has", "not", "it", "its",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

SIMILARITY_THRESHOLD = 0.55
MAX_DAYS_APART = 5


def _tokenize(text):
    tokens = _TOKEN_RE.findall((text or "").lower())
    return {t for t in tokens if t not in STOPWORDS and len(t) > 2}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _dates_close(d1, d2, max_days=MAX_DAYS_APART):
    if d1 is None or d2 is None:
        return True  # unbekannt -> nicht ausschliessen, Textähnlichkeit entscheidet allein
    return abs((d1 - d2).total_seconds()) <= max_days * 86400


def _build_token_index(markets):
    index = {}
    for i, m in enumerate(markets):
        for tok in _tokenize(m["question"]):
            index.setdefault(tok, set()).add(i)
    return index


def find_matches(markets_a, markets_b, threshold=SIMILARITY_THRESHOLD, max_days=MAX_DAYS_APART):
    """Bestes Match pro Markt aus markets_a innerhalb markets_b (andere
    Plattform), sofern Score >= threshold. Gibt Liste (market_a, market_b, score)."""
    index_b = _build_token_index(markets_b)
    tokens_b = [_tokenize(m["question"]) for m in markets_b]
    dates_b = [_parse_date(m.get("end_date")) for m in markets_b]

    matches = []
    for ma in markets_a:
        toks_a = _tokenize(ma["question"])
        if not toks_a:
            continue
        date_a = _parse_date(ma.get("end_date"))
        candidate_idxs = set()
        for tok in toks_a:
            candidate_idxs |= index_b.get(tok, set())

        best_idx, best_score = None, 0.0
        for j in candidate_idxs:
            if not _dates_close(date_a, dates_b[j], max_days):
                continue
            score = _jaccard(toks_a, tokens_b[j])
            if score > best_score:
                best_score, best_idx = score, j

        if best_idx is not None and best_score >= threshold:
            matches.append((ma, markets_b[best_idx], best_score))
    return matches


def find_cross_platform_matches(markets_by_platform, threshold=SIMILARITY_THRESHOLD, max_days=MAX_DAYS_APART):
    """markets_by_platform: dict platform -> Liste normalisierter Märkte.
    Vergleicht jedes Plattform-Paar einmal. Gibt Liste von dicts zurück:
    {"market_a":..., "market_b":..., "score":...}"""
    platforms = list(markets_by_platform.keys())
    out = []
    for i in range(len(platforms)):
        for j in range(i + 1, len(platforms)):
            pa, pb = platforms[i], platforms[j]
            for ma, mb, score in find_matches(
                markets_by_platform[pa], markets_by_platform[pb], threshold, max_days
            ):
                out.append({"market_a": ma, "market_b": mb, "score": score})
    return out


def best_arbitrage_combo(market_a, market_b):
    """Günstigste Kombination aus beiden Märkten, die bei identischer
    Auflösung garantiert genau 1.00 USD auszahlt: Yes auf der einen Seite +
    No auf der anderen. Gibt (side_a, side_b, summe) zurück."""
    combo_yes_a_no_b = market_a["yes_price"] + market_b["no_price"]
    combo_no_a_yes_b = market_a["no_price"] + market_b["yes_price"]
    if combo_yes_a_no_b <= combo_no_a_yes_b:
        return "yes", "no", combo_yes_a_no_b
    return "no", "yes", combo_no_a_yes_b
