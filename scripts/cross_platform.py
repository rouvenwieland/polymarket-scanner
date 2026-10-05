"""
Cross-Platform-Event-Matching
================================

Findet Markt-Paare auf unterschiedlichen Plattformen, die vermutlich
dasselbe Realwelt-Ereignis abbilden - es gibt keine gemeinsame ID über
Plattformgrenzen, die Fragen sind unterschiedlich formuliert
("Will X win?" vs "X to win the..."), also wird über Textähnlichkeit plus
Enddatum-Nähe gematcht. Drei Signale, der jeweils höchste Wert gewinnt
("semantisch genug", ohne eine externe Embedding-API zu brauchen):

  1. Jaccard über Titel-Tokens (Stoppwort-gefiltert) - das ursprüngliche
     Signal, gut bei ähnlichem Wortlaut.
  2. Containment: wie viele Titel-Tokens der einen Seite tauchen irgendwo
     im Titel+Beschreibungstext ("extra_text", falls die Plattform einen
     liefert - Polymarket: description, Kalshi: subtitle/rules_primary,
     PredictIt: keiner) der anderen Seite auf. Fängt Fälle, in denen der
     Titel komplett anders klingt, der Fließtext aber dieselbe Frage
     exakt beschreibt (Jaccard allein würde hier an der schieren Länge
     des Beschreibungstexts scheitern).
  3. Fuzzy-Ratio (difflib.SequenceMatcher) auf den rohen Titeln - fängt
     Umstellungen/Umformulierungen, die weder Jaccard noch Containment
     als Wortmengen-Überlappung sehen.

Das ist KEINE echte semantische/Embedding-Suche (dafür müsste eine externe
Embeddings-API aufgerufen werden - Kosten + Latenz + API-Key-Verwaltung in
der GitHub-Action - aktuell nicht eingerichtet), sondern eine pragmatische,
kostenlose Annäherung mit mehreren sich ergänzenden Textsignalen.

Blocking: statt jeden Markt mit jedem zu vergleichen (bei ~200k
Polymarket-Märkten x ein paar tausend Kalshi/PredictIt-Märkten viel zu
teuer), wird ein inverses Token-Index über die kleinere Plattform gebaut;
nur Märkte, die mindestens ein signifikantes Titel-Token teilen, werden
überhaupt verglichen.

Für ein gefundenes Match: Yes auf Plattform A + No auf Plattform B (oder
umgekehrt) zahlt garantiert genau 1.00 USD, WENN beide Plattformen dasselbe
Ereignis wirklich identisch auflösen - das ist die Kernannahme, die durch
die Ähnlichkeitsschwelle nur approximiert, nie bewiesen wird. Ein falsches
Match (ähnlicher Wortlaut, aber andere Frage/Auflösungskriterien) ist das
Hauptrisiko dieser Strategie.
"""

import difflib
import re
import unicodedata
from datetime import datetime, timezone

STOPWORDS = {
    "will", "the", "a", "an", "of", "in", "on", "for", "to", "by", "be",
    "is", "are", "or", "and", "win", "be", "at", "as", "with", "this",
    "that", "does", "do", "who", "what", "which", "before", "after",
    "than", "more", "less", "than", "have", "has", "not", "it", "its",
}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)  # \w erfasst auch Unicode-Buchstaben (z.B. "Tōkyō")
_YEAR_RE = re.compile(r"\b(20[2-4]\d)\b")
# Wahlkreis-/Bezirkscodes (z.B. "IL-01", "CA-20") und Prozent-Bänder (z.B.
# "45%-50%", "70% or more" grob als "70%") - genau die Art Detail, die der
# Tokenizer wegen der Mindestlänge/Ziffern-Filterung verschluckt, obwohl sie
# oft der EINZIGE Unterschied zwischen einer sehr spezifischen Frage und
# einer viel allgemeineren Frage zum selben Thema ist (siehe _conflicting_specifics).
_DISTRICT_CODE_RE = re.compile(r"\b[A-Za-z]{2}-\d{1,2}\b")
_PERCENT_RE = re.compile(r"\b\d{1,3}%")

SIMILARITY_THRESHOLD = 0.55
MAX_DAYS_APART = 5
FUZZY_WEIGHT = 0.9  # Fuzzy-Ratio etwas vorsichtiger gewichtet als ein echter Token-Treffer


def _stem(token):
    """Sehr grobe Suffix-Normalisierung (kein echter Stemmer/NLTK), nur um
    die häufigsten Plural-/Verbform-Mismatches abzufangen ("rate" vs
    "rates", "cut" vs "cuts/cutting") - ohne zusätzliche Abhängigkeit."""
    for suffix in ("ing", "ed", "es"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _strip_accents(text):
    """Nur Akzente/diakritische Zeichen entfernen (NFKD + combining-Filter,
    reine Stdlib) - macht "Tōkyō" zu "tokyo", damit Namen in unterschiedlicher
    Schreibweise über Plattformen hinweg trotzdem denselben Token ergeben."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _tokenize(text):
    tokens = _TOKEN_RE.findall(_strip_accents((text or "").lower()))
    return {_stem(t) for t in tokens if t not in STOPWORDS and len(t) > 2}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _containment(a, b):
    """Anteil von a's Tokens, die auch in b vorkommen - robust gegen
    Längenunterschiede (anders als Jaccard, das bei einem sehr viel
    längeren Beschreibungstext auf der anderen Seite sonst fast immer
    niedrig ausfällt)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def _parse_date(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Manche Plattformen liefern Enddaten ohne Zeitzone (naiv) - ohne
        # diese Normalisierung crasht die Differenzbildung in _dates_close,
        # sobald eine Seite aware und die andere naiv ist. Annahme: UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _dates_close(d1, d2, max_days=MAX_DAYS_APART):
    if d1 is None or d2 is None:
        return True  # unbekannt -> nicht ausschliessen, Textähnlichkeit entscheidet allein
    return abs((d1 - d2).total_seconds()) <= max_days * 86400


def _build_token_index(title_tokens_list):
    index = {}
    for i, toks in enumerate(title_tokens_list):
        for tok in toks:
            index.setdefault(tok, set()).add(i)
    return index


def _conflicting_years(question_a, question_b):
    """Harter Gegenbeweis: enthalten beide Titel je eine (unterschiedliche)
    Jahreszahl 2020-2049, handelt es sich fast sicher um verschiedene
    Ereignisse ("... 2026" vs "... 2027") - selbst wenn der restliche Text
    sehr ähnlich ist (z.B. dieselbe Liga/Mannschaft, anderes Jahr). Ohne
    diese Prüfung könnte das Enddatum-Fehlen auf einer Seite (dann greift
    der Datums-Filter nicht) ein solches Fehl-Match durchlassen."""
    years_a = set(_YEAR_RE.findall(question_a))
    years_b = set(_YEAR_RE.findall(question_b))
    return bool(years_a) and bool(years_b) and years_a.isdisjoint(years_b)


def _non_numeric(tokens):
    return {t for t in tokens if not t.isdigit()}


_TRAILING_SUBJECT_RE = re.compile(r"-\s*([A-Za-z][A-Za-z.'\s]{2,40})$")
_LEADING_SUBJECT_RE = re.compile(r"\bwill\s+([A-Za-z][A-Za-z.'\s]{2,40}?)\s+(?:win|be)\b", re.IGNORECASE)
_GENERIC_SUBJECTS = {"democratic", "republican", "democrat", "yes", "no"}


def _extract_subject(question):
    """Versucht, das konkrete Subjekt einer Frage zu extrahieren - entweder
    als "- Name"-Suffix (gängiges Format für Mehrfachauswahl-Märkte, siehe
    platforms/predictit.py normalize_contract) oder als "Will <Name> win/be"-
    Konstruktion. None, wenn kein Muster passt."""
    m = _TRAILING_SUBJECT_RE.search(question)
    if m:
        return m.group(1).strip().lower()
    m = _LEADING_SUBJECT_RE.search(question)
    if m:
        return m.group(1).strip().lower()
    return None


def _conflicting_subjects(question_a, question_b):
    """Harter Gegenbeweis für Mehrkandidaten-Märkte: dieselbe Rennen-
    Beschreibung ("... Governor Election ...") kann für JEDEN Kandidaten
    einen eigenen Contract haben - Yes auf Kandidat A + No auf Kandidat B
    ist KEINE Arbitrage (beide können verlieren). Wenn beide Seiten ein
    klar unterschiedliches, nicht-generisches Subjekt nennen, das auf der
    jeweils anderen Seite nirgends im Text vorkommt, ist das so ein Fall."""
    subj_a = _extract_subject(question_a)
    subj_b = _extract_subject(question_b)
    if not subj_a or not subj_b or subj_a == subj_b:
        return False
    if subj_a in _GENERIC_SUBJECTS or subj_b in _GENERIC_SUBJECTS:
        return False
    if subj_a in question_b.lower() or subj_b in question_a.lower():
        return False
    return True


def _conflicting_specifics(question_a, question_b):
    """Zweiter harter Gegenbeweis, analog zu _conflicting_years: nennt eine
    Seite einen Wahlkreis-/Bezirkscode (z.B. "IL-01") oder ein Prozent-Band
    (z.B. "45%"), den die andere Seite gar nicht erwähnt, handelt es sich
    um eine spezifischere/andere Frage zum selben Oberthema - z.B. "Wer
    gewinnt IL-01?" vs. "Wer gewinnt insgesamt das Repräsentantenhaus?".
    Durch die Tokenisierung (Mindestlänge, Ziffern-Filter) wäre dieser
    Unterschied sonst für die Jaccard-/Containment-Scores unsichtbar, weil
    genau diese Codes herausgefiltert werden."""
    codes_a = set(_DISTRICT_CODE_RE.findall(question_a)) | set(_PERCENT_RE.findall(question_a))
    codes_b = set(_DISTRICT_CODE_RE.findall(question_b)) | set(_PERCENT_RE.findall(question_b))
    return bool(codes_a) != bool(codes_b) or (bool(codes_a) and codes_a.isdisjoint(codes_b))


def _combined_score(title_tokens_a, full_tokens_a, question_a, title_tokens_b, full_tokens_b, question_b):
    if (
        _conflicting_years(question_a, question_b)
        or _conflicting_specifics(question_a, question_b)
        or _conflicting_subjects(question_a, question_b)
    ):
        return 0.0
    # Schutz gegen entartete Kurz-Titel: wenn nach Tokenisierung auf einer
    # Seite kaum mehr als eine Jahreszahl übrig bleibt (z.B. weil der Titel
    # fast nur aus kurzen/nicht lateinischen Wörtern bestand, die der
    # Stoppwort-/Mindestlängen-Filter verschluckt), wäre jede gemeinsame
    # Jahreszahl allein schon ein "perfektes" Containment-Match - ohne echten
    # inhaltlichen Bezug. Ohne mindestens 2 NICHT-numerische Tokens pro Seite
    # lieber kein Match als ein Zufallstreffer über ein gemeinsames Jahr.
    if len(_non_numeric(title_tokens_a)) < 2 or len(_non_numeric(title_tokens_b)) < 2:
        return 0.0
    jaccard_score = _jaccard(title_tokens_a, title_tokens_b)
    containment_score = (
        _containment(title_tokens_a, full_tokens_b) + _containment(title_tokens_b, full_tokens_a)
    ) / 2
    fuzzy_score = difflib.SequenceMatcher(None, question_a, question_b).ratio() * FUZZY_WEIGHT
    return max(jaccard_score, containment_score, fuzzy_score)


def find_matches(markets_a, markets_b, threshold=SIMILARITY_THRESHOLD, max_days=MAX_DAYS_APART):
    """Bestes Match pro Markt aus markets_a innerhalb markets_b (andere
    Plattform), sofern Score >= threshold. Gibt Liste (market_a, market_b, score)."""
    title_tokens_b = [_tokenize(m["question"]) for m in markets_b]
    full_tokens_b = [title_tokens_b[i] | _tokenize(m.get("extra_text", "")) for i, m in enumerate(markets_b)]
    questions_b = [(m.get("question") or "").lower() for m in markets_b]
    dates_b = [_parse_date(m.get("end_date")) for m in markets_b]
    index_b = _build_token_index(title_tokens_b)

    matches = []
    for ma in markets_a:
        title_toks_a = _tokenize(ma["question"])
        if not title_toks_a:
            continue
        full_toks_a = title_toks_a | _tokenize(ma.get("extra_text", ""))
        question_a = (ma.get("question") or "").lower()
        date_a = _parse_date(ma.get("end_date"))
        candidate_idxs = set()
        for tok in title_toks_a:
            candidate_idxs |= index_b.get(tok, set())

        best_idx, best_score = None, 0.0
        for j in candidate_idxs:
            if not _dates_close(date_a, dates_b[j], max_days):
                continue
            score = _combined_score(
                title_toks_a, full_toks_a, question_a,
                title_tokens_b[j], full_tokens_b[j], questions_b[j],
            )
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
