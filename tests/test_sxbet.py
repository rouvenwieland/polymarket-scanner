"""SX Bet: Fragetext des Zwei-Wege-Sieger-Markts. Typ 52 ("12", Fussball) hat keinen Remis-Ausgang und ist KEIN Gegenstueck zu
"Will X win?" (Remis zaehlt dort als Nein) - Typ 226 ("12 inkl. Verlaengerung", NFL/NBA) kann nie remis enden."""
import cross_platform as cp
from platforms import sxbet

ODDS = 10 ** 20


def market(type_id, sport, league, one, two):
    return {"marketHash": "0x" + "ab" * 32, "type": type_id, "sportLabel": sport, "leagueLabel": league,
            "outcomeOneName": one, "outcomeTwoName": two, "teamOneName": one, "teamTwoName": two, "gameTime": 1792090800}


BOOK = {"outcomeOne": [{"percentageOdds": str(int(0.55 * ODDS)), "size": str(50 * 10 ** 6)}],
        "outcomeTwo": [{"percentageOdds": str(int(0.45 * ODDS)), "size": str(50 * 10 ** 6)}]}


def question(type_id, sport, league, one, two):
    return sxbet.normalize(market(type_id, sport, league, one, two), BOOK)["question"]


def test_fussball_zwei_wege_markt_traegt_den_remis_vermerk():
    q = question(52, "Soccer", "UEFA Conference League", "FC Kopenhagen", "Sporting Braga")
    assert q.startswith("FC Kopenhagen vs Sporting Braga - wer gewinnt")
    assert cp._bet_aspect(q.lower()) == "draw_no_bet"


def test_markt_ohne_remis_moeglichkeit_bleibt_unveraendert():
    for t, sport, league, a, b in ((226, "Football", "NFL", "Dallas Cowboys", "Philadelphia Eagles"),
                                   (226, "Basketball", "NBA", "Boston Celtics", "New York Knicks")):
        q = question(t, sport, league, a, b)
        assert q == f"{a} vs {b} - wer gewinnt?"
        assert cp._bet_aspect(q.lower()) == "winner"


def test_beide_teams_treffen_bleibt_ein_btts_markt():
    assert cp._bet_aspect(question(17, "Soccer", "MLS", "A FC", "B FC").lower()) == "btts"


def _score(a, b):
    ta, tb = cp._tokenize(a), cp._tokenize(b)
    return cp._combined_score(ta, ta, a.lower(), tb, tb, b.lower())


def test_will_x_win_wird_nie_mit_dem_fussball_zwei_wege_markt_gepaart():
    """Will X win? (Remis = Nein) gegen 'wer gewinnt' ohne Remis-Ausgang ist kein Hedge: bei Remis zahlt die eine Seite
    nichts, die andere nur die Rueckerstattung. Echte Treffer aus data/: West Ham gegen QPR (SX Bet, Typ 52)."""
    sx = question(52, "Soccer", "Championship", "West Ham United", "Queens Park Rangers")
    for poly in ("Will West Ham United FC win on 2026-10-09?", "Will Queens Park Rangers FC win on 2026-10-09?",
                 "West Ham United vs. Queens Park Rangers: Winner"):
        assert cp._conflicting_bet_aspect(poly.lower(), sx.lower()), poly
        assert _score(poly, sx) < cp.SIMILARITY_THRESHOLD, poly
        assert _score(sx, poly) < cp.SIMILARITY_THRESHOLD, poly


def test_sieger_markt_ohne_remis_wird_weiter_mit_sieger_frage_gepaart():
    """Gegenprobe: NFL/NBA (Typ 226, kein Remis moeglich) bleiben mit einer gleichwertigen Sieger-Frage paarbar."""
    sx = question(226, "Football", "NFL", "Dallas Cowboys", "Philadelphia Eagles")
    poly = "Will the Dallas Cowboys beat the Philadelphia Eagles?"
    assert not cp._conflicting_bet_aspect(poly.lower(), sx.lower())
