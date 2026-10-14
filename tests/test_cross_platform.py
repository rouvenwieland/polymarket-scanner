"""Markt-Zuordnung (cross_platform): Regressionen aus dem Echtbetrieb, Symmetrie, Arbitrage-Kombination."""
import json
import os

import pytest
from hypothesis import given, settings, strategies as st

import cross_platform as cp

HERE = os.path.dirname(os.path.abspath(__file__))


def score(a, b):
    ta, tb = cp._tokenize(a), cp._tokenize(b)
    return cp._combined_score(ta, ta, a.lower(), tb, tb, b.lower())


MUST_NOT_MATCH = [
    ("Exact Score: CA Gimnasia y Esgrima de Mendoza 5 - 2 CA Newell's Old Boys?", "Newell's Old Boys wins"),
    ("Exact Score: Any Other Score?", "Final score Draw 3-3?"),
    ("Will Tarcisio de Freitas win the 2026 Brazilian presidential election?",
     "Who will win the 2026 Brazilian presidential election? - Flávio Bolsonaro"),
    ("Will the Republican Party candidate win the 2026 IA-02 House election by 0%-3%?",
     "What will be the national vote margin in the 2026 House midterms? - Republicans 0-3%"),
    ("Will Republicans hold 57 or more Senate seats?", "Which party wins the Ohio Senate special election? - Republican"),
]


@pytest.mark.parametrize("a,b", MUST_NOT_MATCH)
def test_bekannte_fehlmatches_werden_verworfen(a, b):
    assert score(a, b) < cp.SIMILARITY_THRESHOLD


def test_verifizierte_echte_paare_bleiben_erhalten():
    """60 Paare, die die KI-Stufe im Echtbetrieb als 'gleiches Ereignis' bestaetigt hat. Die Regelstufe darf
    nicht mehr als die 3 bekannten Embedding-/Zusatztext-Faelle verlieren."""
    pairs = json.load(open(os.path.join(HERE, "fixtures", "llm_true_pairs.json"), encoding="utf-8"))
    assert len(pairs) >= 60
    lost = [p for p in pairs if score(*p) < cp.SIMILARITY_THRESHOLD]
    # 2 der 60 "bestaetigten" Paare sind KI-Fehlurteile (generischer Kalshi-Titel "Both Teams To Score" ohne Teams):
    # die Regelstufe verwirft sie jetzt zu Recht (_conflicting_generic).
    wrong = [p for p in lost if p[1] == "Both Teams To Score"]
    assert len(wrong) == 2
    assert len(lost) - len(wrong) <= 3, lost


words = st.sampled_from(["Will", "the", "Democrats", "Republicans", "win", "2026", "2027", "Senate", "House", "Ohio",
                         "Texas", "vs", "vs.", "Exact", "Score:", "Both", "Teams", "To", "Score", "wins", "-", "Trump",
                         "Biden", "5", "2", "or", "more", "seats", "first", "half", "Lakers", "Celtics", "?"])
question = st.lists(words, min_size=2, max_size=12).map(" ".join)


@settings(max_examples=400, deadline=None)
@given(question, question)
def test_score_ist_symmetrisch(a, b):
    assert score(a, b) == pytest.approx(score(b, a))


@settings(max_examples=300, deadline=None)
@given(question)
def test_score_gegen_sich_selbst_nie_ausgeschlossen_wenn_gehaltvoll(a):
    # Ein Text gegen sich selbst hat nie einen harten Gegenbeweis und (bei >= 2 Inhaltstokens) Score 1.
    toks = cp._non_numeric(cp._tokenize(a))
    if len(toks) >= 2 and not cp._conflicting_matchup(a.lower(), a.lower(), cp._tokenize(a), cp._tokenize(a)):
        assert score(a, a) == pytest.approx(1.0)


price = st.floats(0.0, 1.0, allow_nan=False)


@given(price, price, price, price)
def test_best_arbitrage_combo_ist_das_minimum_beider_kombinationen(ya, na, yb, nb):
    ma = {"yes_price": ya, "no_price": na}
    mb = {"yes_price": yb, "no_price": nb}
    side_a, side_b, summe = cp.best_arbitrage_combo(ma, mb)
    assert side_a != side_b
    assert summe == pytest.approx(min(ya + nb, na + yb))
    pa = ma["yes_price"] if side_a == "yes" else ma["no_price"]
    pb = mb["yes_price"] if side_b == "yes" else mb["no_price"]
    assert pa + pb == pytest.approx(summe)


# Echte Beispiele aus data/llm_verification_cache.json bzw. den Fonds-Positionen
POLARITY_PAIRS = [
    ("Netanyahu out by end of 2026?", "Who will be the Prime Minister of Israel after the 2026 election? - Benjamin Netanyahu"),
    ("Mike Johnson out as Speaker by October 31?", "Will Mike Johnson remain Speaker of the House through the 119th Congress?"),
    ("Will the Buccaneers vs Cowboys game not air?", "Dallas Cowboys vs Tampa Bay Buccaneers - wer gewinnt?"),
    ("Will Lakers win the game against Celtics tonight?", "Will Lakers lose the game against Celtics tonight?"),
    ("Will Bitcoin close above 100000 on Friday?", "Will Bitcoin close below 100000 on Friday?"),
    ("Will Trump visit Alaska before the midterms?", "Will Trump visit Alaska after the midterms?"),
]


@pytest.mark.parametrize("a,b", POLARITY_PAIRS)
def test_umgekehrte_polaritaet_wird_verworfen(a, b):
    assert cp._opposite_polarity(a.lower(), b.lower())
    assert cp._opposite_polarity(b.lower(), a.lower())  # symmetrisch
    assert score(a, b) < cp.SIMILARITY_THRESHOLD


@pytest.mark.parametrize("a,b", [
    ("Will the Republicans win the Michigan Senate race in 2026?", "Which party will win the 2026 US Senate election in Michigan? - Republican"),
    ("Jordan Addison: 2+ Touchdowns", "Jordan Addison: 2+ touchdowns"),
    ("Will Lakers lose the game tonight?", "Will Lakers not win the game tonight?"),  # beide negativ -> gleiche Polaritaet
])
def test_gleiche_polaritaet_bleibt_erlaubt(a, b):
    assert not cp._opposite_polarity(a.lower(), b.lower())


def test_generischer_titel_identifiziert_kein_konkretes_spiel():
    a = "CA Gimnasia y Esgrima de Mendoza vs. CA Newell's Old Boys: Both Teams to Score"
    b = "Both Teams To Score"
    assert cp._conflicting_generic(cp._tokenize(a), cp._tokenize(b))
    assert score(a, b) < cp.SIMILARITY_THRESHOLD


def test_llm_prompt_nennt_polaritaet():
    import llm_verify
    assert "POLARITÄT" in llm_verify._SYSTEM_PROMPT


def test_gecachte_paare_stichprobe_ohne_grosse_csv():
    """Stichprobe aus data/ (nur die kleine KI-Cache-Datei, nie die 95-MB-CSV): keine echte, von der KI
    bestaetigte Gleichheit ausser dem bekannten Netanyahu-Paar wird wegen Polaritaet verworfen."""
    path = os.path.join(HERE, "..", "data", "llm_verification_cache.json")
    if not os.path.isfile(path):
        pytest.skip("kein Cache")
    cache = json.load(open(path, encoding="utf-8"))
    hit = [v for v in cache.values() if v["same_event"] and cp._opposite_polarity(v["frage_a"].lower(), v["frage_b"].lower())]
    assert [h["frage_a"] for h in hit] in ([], ["Netanyahu out by end of 2026?"])
