"""
Neuberechnung aller Fonds mit der korrigierten Strategie (Reparatur 27.10.2026)
=================================================================================

Rechnet die Fonds von Anfang an neu: nur echte Arbitrage (arbitrage_check.check_pair), realistische Gebuehren,
Groesse wie im Live-Fonds bei unbekannter Orderbuch-Tiefe (10 USD je Bein), Abrechnung zum tatsaechlichen Ergebnis
(aufgeloeste Maerkte ueber die oeffentlichen APIs nachgeschlagen). Ergebnis: eigene Reihe in data/korrigiert/.

Aufruf (reproduzierbar, offline aus den gespeicherten Quellen):
    python scripts/neuberechnung.py                 # rechnet aus data/korrigiert/quellen/*.json
    python scripts/neuberechnung.py --abrufen       # holt vorher die Quellen (Gamma, CLOB-Kursverlauf, SX Bet,
                                                    # PredictIt, Kalshi) - laeuft in GitHub Actions, weil
                                                    # clob.polymarket.com von Deutschland aus nicht erreichbar ist
    python scripts/neuberechnung.py --uebernehmen   # zusaetzlich: Live-Fonds archivieren und durch die
                                                    # korrigierte Reihe ersetzen (einmalig, siehe uebernehmen())

Datengrundlage und Grenzen (stehen auch in data/korrigiert/zusammenfassung.md):
  - Kandidaten: alle Zeilen der Treffer-Dateien data/cross_platform_hits*.csv (jeder Scan seit 03.10.2026).
  - Die alten Treffer-Dateien speichern nur die Kombi-Summe, nicht die Einzelpreise. Der Preis des Polymarkt-Beins
    kommt aus dem CLOB-Kursverlauf (Stundenwerte) des gekauften Tokens, das andere Bein = Summe - Polymarkt-Preis.
    Weil der Kursverlauf kein Ask ist, muss die Pruefung bei Polymarkt-Preis -2, 0, +2 und +4 Cent bestehen
    (Kurs hoechstens 2 h alt).
  - Polymarkt-Preise sind erst ab dem Scan nach 14.10.2026 12:00 Orderbuch-Asks (Commit 914bc77); davor nur
    Mittel-/Letztkurse, also nicht ausfuehrbar: keine Kaeufe vor CUT_EXECUTABLE.
  - Paare mit vertauschter Seitenzuordnung ("flipped") sind nicht nachrechenbar: gespeichert ist die falsche
    Kombination, die Preise der richtigen fehlen. Sie werden nicht gekauft (gezaehlt in der Zusammenfassung).
  - Gleiches Ereignis: wie im Live-Fonds nur Paare, die die KI-Stufe bestaetigt hat (data/llm_verification_cache.json),
    UND zusaetzlich eine Pruefung von Hand je Paar (data/korrigiert/paarpruefung.csv; fehlt ein Paar dort, gilt es
    als nicht geprueft und wird nicht gekauft). Die KI hat nachweislich Fehlpaare bestaetigt (Remis gegen Sieger,
    generisches "Both Teams To Score", Lamar).
  - Orderbuch-Tiefe ist historisch unbekannt: je Bein hoechstens LEG_CAP_USD (wie der Live-Fonds ohne Tiefe).
    Zum Vergleich rechnet --empfindlichkeit dieselbe Reihe mit 100 und 1.000 USD je Bein.
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import arbitrage_check as ac  # noqa: E402
import cross_platform as cp  # noqa: E402
import pricing  # noqa: E402
from platforms import kalshi, polymarket  # noqa: E402

DATA = "data"
OUT_SUB = "korrigiert"
QUELLEN_SUB = os.path.join(OUT_SUB, "quellen")
ARCHIV_SUB = os.path.join("archiv", "vor_reparatur_2026-10-27")
CUT_EXECUTABLE = datetime.fromisoformat("2026-10-14T13:00:00+02:00")
LEG_CAP_USD = ac.UNKNOWN_DEPTH_LEG_USD
PRICE_BAND = (-0.02, 0.0, 0.02, 0.04)  # Kursverlauf ist kein Ask: auch 4 Cent teurer muss es reichen
MAX_PRICE_AGE_H = 2.0
STRATEGIE = "v2-2026-10-27"
PREDICTIT_LAG_DAYS = 3.0

CROSS_FUNDS = [
    ("cross_platform", 100.0, "fund_state_crossplatform.json", "fund_trades_crossplatform.csv",
     "fund_history_crossplatform.csv", "Cross-Platform (100 USD)"),
    ("cross_platform_1k", 1000.0, "fund_state_crossplatform_1k.json", "fund_trades_crossplatform_1k.csv",
     "fund_history_crossplatform_1k.csv", "Cross-Platform (1.000 USD)"),
    ("cross_platform_10k", 10000.0, "fund_state_crossplatform_10k.json", "fund_trades_crossplatform_10k.csv",
     "fund_history_crossplatform_10k.csv", "Cross-Platform (10.000 USD)"),
]
POLY_FUNDS = [
    ("conservative", 100.0, "fund_state.json", "fund_trades.csv", "fund_history.csv", "Konservativ"),
    ("aggressive", 100.0, "fund_state_aggressive.json", "fund_trades_aggressive.csv", "fund_history_aggressive.csv",
     "Aggressiv"),
    ("best_case", 100.0, "fund_state_bestcase.json", "fund_trades_bestcase.csv", "fund_history_bestcase.csv",
     "Best Case (unrealistisch)"),
]
TRADE_FIELDS = ["timestamp", "aktion", "pair_id", "frage", "anteile", "preis", "betrag", "kasse_danach", "notiz",
                "preisbasis"]
HIST_FIELDS = ["timestamp", "kasse", "positionswert", "nav", "rendite_pct", "offene_positionen", "terminierungswert",
               "diskontierter_terminierungswert", "diskontierte_rendite_pct", "positionen_schaetzung",
               "positionswert_schaetzung", "liquidationswert", "bewertung"]


# ---------------------------------------------------------------------------- Hilfen
def ts(s):
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_json(path, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def text_conflicts(qa, qb):
    """Harte Gegenbeweise des heutigen Matchers (wie bereinigung.counter_evidence)."""
    a, b = qa.lower(), qb.lower()
    ta, tb = cp._tokenize(qa), cp._tokenize(qb)
    return (cp._conflicting_years(a, b) or cp._conflicting_specifics(a, b) or cp._conflicting_subjects(a, b)
            or cp._conflicting_race_type(a, b) or cp._conflicting_matchup(a, b, ta, tb)
            or cp._conflicting_bet_aspect(a, b) or cp._opposite_polarity(a, b) or cp._conflicting_generic(ta, tb))


# ---------------------------------------------------------------------------- Kandidaten aus den Treffer-Dateien
def llm_verdicts(data_dir):
    """(frage_a, frage_b) -> (cache_key, same_event). Der Cache-Schluessel enthaelt die Markt-IDs beider Seiten."""
    cache = read_json(os.path.join(data_dir, "llm_verification_cache.json"), {}) or {}
    out = {}
    for key, v in cache.items():
        out[(v.get("frage_a"), v.get("frage_b"))] = (key, v.get("same_event"))
        out[(v.get("frage_b"), v.get("frage_a"))] = (key, v.get("same_event"))
    return out


def ids_from_key(key, plat_a, plat_b):
    """Cache-Schluessel "plattform:id__plattform:id" (sortiert) -> (id_a, id_b) passend zu den Plattformen der Zeile."""
    parts = dict(p.split(":", 1) for p in key.split("__"))
    return parts.get(plat_a), parts.get(plat_b)


def collect(data_dir):
    """Liest alle Treffer-Zeilen. Gibt (runs, kandidaten, statistik) zurueck. kandidaten: Zeilen, die die
    textbasierten Pruefungen bestehen und von der KI bestaetigt sind (mit Markt-IDs)."""
    verdicts = llm_verdicts(data_dir)
    runs, cands = set(), []
    stats = {"zeilen": 0, "kursunterschied_ausserhalb": 0, "schaetzkurs_vor_14_10": 0, "matcher_gegenbeweis": 0,
             "regel_text": 0, "kalshi_sport": 0, "ki_nicht_bestaetigt": 0, "kandidaten_zeilen": 0}
    memo = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "cross_platform_hits*.csv"))):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                stats["zeilen"] += 1
                t = ts(r["timestamp"])
                runs.add(r["timestamp"])
                s = float(r["summe"])
                if not (1.0 - ac.MAX_PLAUSIBLE_SPREAD <= s <= 1.0 - ac.MIN_EDGE):
                    stats["kursunterschied_ausserhalb"] += 1
                    continue
                if "polymarket" in (r["plattform_a"], r["plattform_b"]) and t < CUT_EXECUTABLE:
                    stats["schaetzkurs_vor_14_10"] += 1
                    continue
                q = (r["frage_a"], r["frage_b"])
                if q not in memo:
                    ma = {"platform": r["plattform_a"], "question": q[0]}
                    mb = {"platform": r["plattform_b"], "question": q[1]}
                    if text_conflicts(*q):
                        memo[q] = "matcher_gegenbeweis"
                    elif [x for x in ac.rule_conflicts(ma, mb) if "Sport" not in x and "Anstoss" not in x
                          and "Abbruch" not in x]:
                        memo[q] = "regel_text"
                    elif any(kalshi.is_sport_ticker(u.rstrip("/").rsplit("/", 1)[-1])
                             for u, p in ((r["url_a"], r["plattform_a"]), (r["url_b"], r["plattform_b"]))
                             if p == "kalshi"):
                        memo[q] = "kalshi_sport"
                    elif verdicts.get(q, (None, None))[1] is not True:
                        memo[q] = "ki_nicht_bestaetigt"
                    else:
                        memo[q] = "ok"
                if memo[q] != "ok":
                    stats[memo[q]] += 1
                    continue
                key = verdicts[q][0]
                id_a, id_b = ids_from_key(key, r["plattform_a"], r["plattform_b"])
                stats["kandidaten_zeilen"] += 1
                cands.append({"timestamp": r["timestamp"], "plattform_a": r["plattform_a"], "id_a": id_a,
                              "frage_a": q[0], "plattform_b": r["plattform_b"], "id_b": id_b, "frage_b": q[1],
                              "seite_a": r["seite_a"], "seite_b": r["seite_b"], "summe": s, "paar": key})
    return sorted(runs), cands, stats


# ---------------------------------------------------------------------------- Quellen abrufen (nur --abrufen)
def _get(url, params=None, tries=4):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "polymarket-scanner-neuberechnung"})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - Netzfehler jeder Art: erneut versuchen, am Ende None
            if i == tries - 1:
                print(f"[Warnung] {url} {params or ''}: {e}")
                return None
            time.sleep(1.5 * (i + 1))


def abrufen(data_dir, cands):
    qdir = os.path.join(data_dir, QUELLEN_SUB)
    gamma = read_json(os.path.join(qdir, "gamma.json"), {}) or {}
    sx = read_json(os.path.join(qdir, "sxbet.json"), {}) or {}
    pi = read_json(os.path.join(qdir, "predictit.json"), {}) or {}
    ks = read_json(os.path.join(qdir, "kalshi.json"), {}) or {}
    kurse = read_json(os.path.join(qdir, "kurse_polymarket.json"), {}) or {}
    windows = {}
    for c in cands:
        for plat, mid, side in ((c["plattform_a"], c["id_a"], c["seite_a"]), (c["plattform_b"], c["id_b"], c["seite_b"])):
            if plat == "polymarket":
                w = windows.setdefault((mid, side), [ts(c["timestamp"]), ts(c["timestamp"])])
                w[0], w[1] = min(w[0], ts(c["timestamp"])), max(w[1], ts(c["timestamp"]))
                if mid not in gamma:
                    gamma[mid] = _get(f"{polymarket.GAMMA_URL}/{mid}")
            elif plat == "sxbet" and mid not in sx:
                d = _get("https://api.sx.bet/markets/find", {"marketHashes": mid})
                sx[mid] = (d or {}).get("data", [None])[0] if d else None
            elif plat == "predictit":
                outer = mid.split(":")[0]
                if outer not in pi:
                    pi[outer] = _get(f"https://www.predictit.org/api/marketdata/markets/{outer}")
            elif plat == "kalshi" and mid not in ks:
                d = _get(f"{kalshi.BASE_URL}/{mid}")
                ks[mid] = (d or {}).get("market")
    for (mid, side), (t0, t1) in sorted(windows.items()):
        m = gamma.get(mid) or {}
        tokens = pricing._json_list(m.get("clobTokenIds")) or []
        idx = pricing.outcome_indices(m)
        if len(tokens) != 2 or idx is None:
            continue
        token = str(tokens[idx[0] if side == "yes" else idx[1]])
        key = f"{mid}:{side}"
        have = {int(p["t"]) for p in kurse.get(key, [])}
        start, end = int((t0 - timedelta(hours=12)).timestamp()), int((t1 + timedelta(hours=1)).timestamp())
        pts = list(kurse.get(key, []))
        cur = start
        while cur < end:  # in Wochenstuecken, die API begrenzt den Zeitraum je Abfrage
            nxt = min(end, cur + 7 * 86400)
            d = _get("https://clob.polymarket.com/prices-history",
                     {"market": token, "startTs": cur, "endTs": nxt, "fidelity": 60})
            for p in (d or {}).get("history", []):
                if int(p["t"]) not in have:
                    pts.append({"t": int(p["t"]), "p": float(p["p"])})
                    have.add(int(p["t"]))
            cur = nxt
        kurse[key] = sorted(pts, key=lambda p: p["t"])
    stamp = datetime.now(timezone.utc).isoformat()
    for name, obj in (("gamma.json", gamma), ("sxbet.json", sx), ("predictit.json", pi), ("kalshi.json", ks),
                      ("kurse_polymarket.json", kurse)):
        write_json(os.path.join(qdir, name), obj)
    write_json(os.path.join(qdir, "abgerufen.json"), {"zeitpunkt": stamp, "maerkte_polymarket": len(gamma),
                                                      "kursreihen": len(kurse), "sxbet": len(sx), "predictit": len(pi),
                                                      "kalshi": len(ks)})


# ---------------------------------------------------------------------------- Maerkte rekonstruieren
def _pi_contract(pi_markets, mid):
    outer, _, contract = mid.partition(":")
    m = (pi_markets or {}).get(outer) or {}
    for c in m.get("contracts", []) or []:
        if str(c.get("id")) == contract:
            return m, c
    return m, None


def leg_market(plat, mid, question, q, src):
    """Normalisierter Markt (Schema wie platforms/*) aus den gespeicherten Metadaten, ohne Preise."""
    base = {"platform": plat, "market_id": mid, "question": question, "price_basis": pricing.BASIS_EXECUTABLE,
            "yes_label": None, "no_label": None, "is_sport": False, "sport_market_type": "", "start_time": None,
            "end_date": None}
    if plat == "polymarket":
        m = src["gamma"].get(mid)
        if not m:
            return None
        yl, nl = pricing.outcome_labels(m)
        base.update(yes_label=yl, no_label=nl, is_sport=bool(m.get("sportsMarketType") or m.get("gameStartTime")),
                    sport_market_type=m.get("sportsMarketType") or "", start_time=polymarket._iso_utc(m.get("gameStartTime")),
                    fee_rate=polymarket.fee_rate(m), end_date=m.get("endDate"), slug=m.get("slug") or "")
    elif plat == "sxbet":
        m = src["sx"].get(mid)
        if not m:
            return None
        two = m.get("type") in (52, 226)
        start = datetime.fromtimestamp(m["gameTime"], tz=timezone.utc).isoformat() if m.get("gameTime") else None
        base.update(yes_label=m.get("outcomeOneName") if two else None, no_label=m.get("outcomeTwoName") if two else None,
                    is_sport=True, sport_market_type={52: "winner_no_draw", 226: "moneyline", 17: "btts"}.get(m.get("type"), ""),
                    start_time=start, end_date=start)
    elif plat == "kalshi":
        m = src["kalshi"].get(mid) or {}
        base.update(is_sport=kalshi.is_sport_ticker(mid), end_date=m.get("close_time"))
    elif plat == "predictit":
        _, c = _pi_contract(src["pi"], mid)
        if c and c.get("dateEnd") not in (None, "", "NA", "N/A"):
            base["end_date"] = c["dateEnd"]
    return base


def resolution(plat, mid, src, cost):
    """(Auszahlung je Anteil fuer "yes", fuer "no", Zeitpunkt) wenn aufgeloest, sonst None. Polymarkt 50-50 -> 0,5;
    SX Bet void -> Einsatz zurueck (cost je Seite)."""
    if plat == "polymarket":
        m = src["gamma"].get(mid) or {}
        status = str(m.get("umaResolutionStatus") or "").lower()
        if not m.get("closed") or (status and status != "resolved"):
            return None
        prices = pricing.parse_outcome_prices(m)
        if prices is None:
            return None
        finals = []
        for p in prices:
            f = next((x for x in (0.0, 0.5, 1.0) if abs(p - x) <= 0.01), None)
            if f is None:
                return None
            finals.append(f)
        when = m.get("closedTime") or m.get("umaEndDate") or m.get("endDate")
        when = str(when).replace(" ", "T").replace("+00", "+00:00") if when and str(when).endswith("+00") else when
        return finals[0], finals[1], ts(when)
    if plat == "sxbet":
        m = src["sx"].get(mid) or {}
        out = m.get("outcome")
        if out is None or not m.get("reportedDate"):
            return None
        when = datetime.fromtimestamp(m["reportedDate"], tz=timezone.utc)
        if out == 0:
            return "refund", "refund", when
        return (1.0, 0.0, when) if out == 1 else (0.0, 1.0, when)
    if plat == "kalshi":
        m = src["kalshi"].get(mid) or {}
        if m.get("status") not in ("finalized", "settled") or m.get("result") not in ("yes", "no"):
            return None
        when = ts(m.get("settlement_ts") or m.get("close_time"))
        return (1.0, 0.0, when) if m["result"] == "yes" else (0.0, 1.0, when)
    if plat == "predictit":
        _, c = _pi_contract(src["pi"], mid)
        if not c or c.get("status") == "Open":
            return None
        last = c.get("lastTradePrice")
        try:
            last = float(last)
        except (TypeError, ValueError):
            return None
        when = ts(src["abgerufen"])
        if last >= 0.95:
            return 1.0, 0.0, when
        if last <= 0.05:
            return 0.0, 1.0, when
    return None


def poly_price(src, mid, side, at):
    pts = src["kurse"].get(f"{mid}:{side}") or []
    t = at.timestamp()
    before = [p for p in pts if p["t"] <= t]
    if not before or t - before[-1]["t"] > MAX_PRICE_AGE_H * 3600:
        return None
    return before[-1]["p"]


def evaluate(c, src):
    """Prueft eine Kandidaten-Zeile gegen die Strategie. Gibt dict mit ok/grund/Maerkten/Preisen zurueck."""
    t = ts(c["timestamp"])
    ma = leg_market(c["plattform_a"], c["id_a"], c["frage_a"], None, src)
    mb = leg_market(c["plattform_b"], c["id_b"], c["frage_b"], None, src)
    if ma is None or mb is None:
        return {"ok": False, "grund": "Marktdaten nicht abrufbar"}
    alignment = cp.outcome_alignment(ma, mb)
    if alignment is None:
        return {"ok": False, "grund": "Seitenzuordnung unklar"}
    if alignment == "flipped":
        return {"ok": False, "grund": "vertauschte Seiten: gespeichert ist die falsche Kombination, nicht nachrechenbar"}
    # Polymarkt-Bein bepreisen, anderes Bein = Summe - Polymarkt (siehe Moduldoku)
    if c["plattform_a"] == "polymarket":
        pp = poly_price(src, c["id_a"], c["seite_a"], t)
    elif c["plattform_b"] == "polymarket":
        pp = poly_price(src, c["id_b"], c["seite_b"], t)
    else:
        pp = None
    if pp is None:
        return {"ok": False, "grund": "kein Polymarkt-Kurs zum Zeitpunkt (Kursverlauf)"}
    results = []
    for d in PRICE_BAND:
        p_poly = min(0.99, max(0.01, pp + d))
        p_other = c["summe"] - p_poly
        pa, pb = (p_poly, p_other) if c["plattform_a"] == "polymarket" else (p_other, p_poly)
        mma, mmb = dict(ma), dict(mb)
        for m, side, p in ((mma, c["seite_a"], pa), (mmb, c["seite_b"], pb)):
            m["yes_price"], m["no_price"] = (p, 0.999) if side == "yes" else (0.999, p)
        results.append((ac.check_pair(mma, mmb, t, alignment), mma, mmb))
    bad = [r for r, _, _ in results if not r.ok]
    if bad:
        return {"ok": False, "grund": "; ".join(sorted(set(bad[0].reasons)))}
    chk, mma, mmb = results[1]
    if (chk.side_a, chk.side_b) != (c["seite_a"], c["seite_b"]):
        return {"ok": False, "grund": "Seitenwahl weicht ab"}
    return {"ok": True, "grund": "", "chk": chk, "ma": mma, "mb": mmb}


# ---------------------------------------------------------------------------- Simulation
def simulate(runs, cands, src, review, start_capital, start_time, leg_cap):
    """Wie cross_platform_fund: je Scan erst abrechnen, dann kaufen. Gibt (history, trades, state, kaeufe) zurueck."""
    by_run = {}
    for c in cands:
        by_run.setdefault(c["timestamp"], []).append(c)
    cash, positions, trades, hist = start_capital, [], [], []

    def nav_of():
        return cash + sum(p["einstand_gesamt"] for p in positions)

    for run in runs:
        now = ts(run)
        if now < start_time:
            continue
        # 1) abrechnen: beide Beine aufgeloest, und zwar spaetestens jetzt
        still = []
        for p in positions:
            ra = resolution(p["leg_a"]["platform"], p["leg_a"]["market_id"], src, p["leg_a"]["einstandskurs"])
            rb = resolution(p["leg_b"]["platform"], p["leg_b"]["market_id"], src, p["leg_b"]["einstandskurs"])
            # PredictIt-Maerkte verschwinden nach der Aufloesung aus der API. Ist das andere Bein aufgeloest und das Paar
            # von Hand als regelgleich bestaetigt, gilt fuer PredictIt dasselbe Ergebnis (Zuordnung "same"), ausgezahlt
            # PREDICTIT_LAG_DAYS spaeter. Gekennzeichnet in der Notiz ("abgeleitet").
            derived = False
            for mine, other, leg in ((ra, rb, "leg_a"), (rb, ra, "leg_b")):
                if mine is None and other is not None and p[leg]["platform"] == "predictit" and other[0] != "refund":
                    d = (other[0], other[1], other[2] + timedelta(days=PREDICTIT_LAG_DAYS))
                    ra, rb = (d, rb) if leg == "leg_a" else (ra, d)
                    derived = True
            if ra is None or rb is None or max(ra[2], rb[2]) > now:
                still.append(p)
                continue
            pay = []
            for leg, r in (("leg_a", ra), ("leg_b", rb)):
                v = r[0] if p[leg]["side"] == "yes" else r[1]
                v = p[leg]["einstandskurs"] if v == "refund" else v
                pay.append(ac.settle_value(p[leg]["platform"], v, p[leg]["einstandskurs"]))
            proceeds = p["shares"] * sum(pay)
            cash += proceeds
            trades.append({"timestamp": run, "aktion": "AUSZAHLUNG", "pair_id": p["pair_id"], "frage": p["frage"],
                           "anteile": round(p["shares"], 4), "preis": round(sum(pay), 4), "betrag": round(proceeds, 4),
                           "kasse_danach": round(cash, 4), "preisbasis": pricing.BASIS_EXECUTABLE,
                           "notiz": f"Neuberechnung: aufgeloest (netto A={pay[0]:.3f}, B={pay[1]:.3f} je Anteil)"
                                    + (", PredictIt-Ergebnis abgeleitet" if derived else "")})
        positions = still
        # 2) kaufen
        open_ids = {p["pair_id"] for p in positions}
        picks = []
        for c in by_run.get(run, []):
            pid = f"{c['plattform_a']}:{c['id_a']}__{c['plattform_b']}:{c['id_b']}"
            if pid in open_ids or c.get("eval") is None or not c["eval"]["ok"]:
                continue
            if review.get(c["paar"], {}).get("entscheidung") != "echt":
                continue
            open_ids.add(pid)
            picks.append((pid, c))
        picks.sort(key=lambda x: -x[1]["eval"]["chk"].annual)
        picks = picks[:max(0, 15 - len(positions))]
        if picks:
            nav0, per_slot = nav_of(), cash / len(picks)
            for pid, c in picks:
                ev = c["eval"]
                budget = min(cash, per_slot, ac.MAX_PAIR_FRACTION * nav0)
                if budget < 2.0:
                    continue
                la = [(ev["chk"].price_a, leg_cap / ev["chk"].price_a)]
                lb = [(ev["chk"].price_b, leg_cap / ev["chk"].price_b)]
                fill = ac.size_pair(ev["ma"], ev["mb"], ev["chk"], la, lb, budget)
                if fill is None or fill.total > cash or fill.total < 2.0 or not ac.fill_is_safe(fill):
                    continue
                cash -= fill.total
                frage = f"[{c['plattform_a']}] {c['frage_a']}  <->  [{c['plattform_b']}] {c['frage_b']}"
                positions.append({
                    "pair_id": pid, "frage": frage, "shares": fill.shares, "eingestiegen": run,
                    "end_date": max([d for d in (ev["ma"].get("end_date"), ev["mb"].get("end_date")) if d] or [None],
                                    key=lambda d: ts(d) if d else datetime.min.replace(tzinfo=timezone.utc)),
                    "preisbasis": pricing.BASIS_EXECUTABLE, "strategie": STRATEGIE, "zuordnung": ev["chk"].alignment,
                    "einstand_gesamt": fill.total, "garantierter_endwert": fill.worst_value,
                    "sport": bool(ev["ma"].get("is_sport") or ev["mb"].get("is_sport")),
                    "leg_a": {"platform": c["plattform_a"], "market_id": c["id_a"], "side": c["seite_a"],
                              "end_date": ev["ma"].get("end_date"), "einstandskurs": fill.avg_a,
                              "letzter_kurs": fill.avg_a, "fee_gezahlt": fill.fee_a},
                    "leg_b": {"platform": c["plattform_b"], "market_id": c["id_b"], "side": c["seite_b"],
                              "end_date": ev["mb"].get("end_date"), "einstandskurs": fill.avg_b,
                              "letzter_kurs": fill.avg_b, "fee_gezahlt": fill.fee_b},
                })
                trades.append({"timestamp": run, "aktion": "KAUF", "pair_id": pid, "frage": frage,
                               "anteile": round(fill.shares, 4), "preis": round(fill.avg_a + fill.avg_b, 4),
                               "betrag": round(-fill.total, 4), "kasse_danach": round(cash, 4),
                               "preisbasis": pricing.BASIS_EXECUTABLE,
                               "notiz": f"Neuberechnung: Kursunterschied {ev['chk'].spread*100:.2f}%, garantiert "
                                        f"{fill.worst_value - fill.total:+.2f} USD ({ev['chk'].annual*100:.1f}% p.a.), "
                                        f"Beine {c['plattform_a']}/{c['seite_a']} + {c['plattform_b']}/{c['seite_b']}"})
        book = sum(p["einstand_gesamt"] for p in positions)
        guaranteed = sum(p["garantierter_endwert"] for p in positions)
        nav = cash + book
        hist.append({"timestamp": run, "kasse": round(cash, 4), "positionswert": round(book, 4), "nav": round(nav, 4),
                     "rendite_pct": round((nav / start_capital - 1) * 100, 3), "offene_positionen": len(positions),
                     "terminierungswert": round(cash + guaranteed, 4),
                     "diskontierter_terminierungswert": round(cash + guaranteed, 4),
                     "diskontierte_rendite_pct": round(((cash + guaranteed) / start_capital - 1) * 100, 3),
                     "positionen_schaetzung": 0, "positionswert_schaetzung": 0, "liquidationswert": round(nav, 4),
                     "bewertung": "anschaffungswert (neuberechnet)"})
    state = {"cash": cash, "positions": positions, "started": start_time.isoformat(),
             "neuberechnung": {"strategie": STRATEGIE, "skript": "scripts/neuberechnung.py",
                               "bein_deckel_usd": leg_cap}}
    return hist, trades, state


def start_time_of(data_dir, history_name):
    """Startzeitpunkt eines Fonds = erste Zeile seiner History (Sicherung vor der Bereinigung, sonst live)."""
    for base in (os.path.join(data_dir, "archiv", "vor_bereinigung"), os.path.join(data_dir, ARCHIV_SUB), data_dir):
        rows = read_csv(os.path.join(base, history_name))
        if rows:
            return ts(rows[0]["timestamp"])
    return datetime(2026, 10, 1, tzinfo=timezone.utc)


def poly_only(data_dir):
    """Polymarkt-Fonds (Yes+No < 1 im selben Markt): Kandidaten = Snapshot-Zeilen auf ausfuehrbaren Preisen. Auf einem
    gueltigen Orderbuch ist Ask(Yes) + Ask(No) = Ask + 1 - Bid >= 1 (verify/lean: book_buy_sum_ge_one) - es kann keine
    geben. Gezaehlt wird trotzdem, damit eine Datenluecke auffiele."""
    rows = read_csv(os.path.join(data_dir, "snapshots.csv"))
    exe = [r for r in rows if r.get("preisbasis") == pricing.BASIS_EXECUTABLE and float(r.get("summe") or 9) < 1.0]
    return len(rows), len(exe)


# ---------------------------------------------------------------------------- Hauptablauf
def load_sources(data_dir):
    q = os.path.join(data_dir, QUELLEN_SUB)
    meta = read_json(os.path.join(q, "abgerufen.json"), {}) or {}
    return {"gamma": read_json(os.path.join(q, "gamma.json"), {}) or {},
            "sx": read_json(os.path.join(q, "sxbet.json"), {}) or {},
            "pi": read_json(os.path.join(q, "predictit.json"), {}) or {},
            "kalshi": read_json(os.path.join(q, "kalshi.json"), {}) or {},
            "kurse": read_json(os.path.join(q, "kurse_polymarket.json"), {}) or {},
            "abgerufen": meta.get("zeitpunkt") or datetime.now(timezone.utc).isoformat()}


def load_review(data_dir):
    return {r["paar"]: r for r in read_csv(os.path.join(data_dir, OUT_SUB, "paarpruefung.csv"))}


def run(data_dir=DATA, fetch=False, sensitivity=True):
    runs, cands, stats = collect(data_dir)
    if fetch:
        abrufen(data_dir, cands)
    src = load_sources(data_dir)
    review = load_review(data_dir)
    for c in cands:
        c["eval"] = evaluate(c, src)
    out = os.path.join(data_dir, OUT_SUB)
    # Kandidatenliste je Paar (fuer die Pruefung von Hand und die Zusammenfassung)
    per_pair = {}
    for c in cands:
        e = per_pair.setdefault(c["paar"], {"paar": c["paar"], "frage_a": c["frage_a"], "frage_b": c["frage_b"],
                                            "zeilen": 0, "bestanden": 0, "min_summe": 9.0, "erste": c["timestamp"],
                                            "letzte": c["timestamp"], "grund_beispiel": ""})
        e["zeilen"] += 1
        e["min_summe"] = min(e["min_summe"], c["summe"])
        e["letzte"] = c["timestamp"]
        if c["eval"]["ok"]:
            e["bestanden"] += 1
        elif not e["grund_beispiel"]:
            e["grund_beispiel"] = c["eval"]["grund"][:240]
    for e in per_pair.values():
        e["pruefung_von_hand"] = review.get(e["paar"], {}).get("entscheidung", "")
    write_csv(os.path.join(out, "kandidaten.csv"),
              ["paar", "frage_a", "frage_b", "zeilen", "bestanden", "min_summe", "erste", "letzte", "grund_beispiel",
               "pruefung_von_hand"], sorted(per_pair.values(), key=lambda e: (-e["bestanden"], e["paar"])))
    summary = {"statistik": stats, "scans": len(runs), "paare_kandidaten": len(per_pair),
               "paare_bestanden_automatisch": sum(1 for e in per_pair.values() if e["bestanden"]),
               "paare_echt_nach_pruefung": sum(1 for e in per_pair.values()
                                                if e["bestanden"] and e["pruefung_von_hand"] == "echt"),
               "fonds": {}, "empfindlichkeit": {}}
    for key, cap, state_n, trades_n, hist_n, label in CROSS_FUNDS:
        start = start_time_of(data_dir, hist_n)
        hist, trades, state = simulate(runs, cands, src, review, cap, start, LEG_CAP_USD)
        write_csv(os.path.join(out, hist_n), HIST_FIELDS, hist)
        write_csv(os.path.join(out, trades_n), TRADE_FIELDS, trades)
        write_json(os.path.join(out, state_n), state)
        last = hist[-1] if hist else {"nav": cap, "terminierungswert": cap}
        summary["fonds"][key] = {"label": label, "start": cap, "nav": last["nav"],
                                 "terminierungswert": last["terminierungswert"],
                                 "kaeufe": sum(1 for t in trades if t["aktion"] == "KAUF"),
                                 "auszahlungen": sum(1 for t in trades if t["aktion"] == "AUSZAHLUNG"),
                                 "offen": len(state["positions"]),
                                 "gebunden_usd": round(sum(p["einstand_gesamt"] for p in state["positions"]), 2)}
        if sensitivity:
            for leg_cap in (100.0, 1000.0):
                h2, t2, s2 = simulate(runs, cands, src, review, cap, start, leg_cap)
                l2 = h2[-1] if h2 else {"nav": cap, "terminierungswert": cap}
                summary["empfindlichkeit"][f"{key}@{leg_cap:.0f}"] = {
                    "nav": l2["nav"], "terminierungswert": l2["terminierungswert"],
                    "kaeufe": sum(1 for t in t2 if t["aktion"] == "KAUF")}
    n_snap, n_exe = poly_only(data_dir)
    summary["polymarkt_einzelmarkt"] = {"snapshot_zeilen": n_snap, "ausfuehrbar_unter_eins": n_exe}
    for key, cap, state_n, trades_n, hist_n, label in POLY_FUNDS:
        rows = read_csv(os.path.join(data_dir, "archiv", "vor_bereinigung", hist_n)) or read_csv(os.path.join(data_dir, hist_n))
        hist = [{"timestamp": r["timestamp"], "kasse": cap, "positionswert": 0, "nav": cap, "rendite_pct": 0.0,
                 "offene_positionen": 0, "terminierungswert": cap, "diskontierter_terminierungswert": cap,
                 "diskontierte_rendite_pct": 0.0, "positionen_schaetzung": 0, "positionswert_schaetzung": 0,
                 "liquidationswert": cap, "bewertung": "anschaffungswert (neuberechnet)"} for r in rows]
        write_csv(os.path.join(out, hist_n), HIST_FIELDS, hist)
        write_csv(os.path.join(out, trades_n), TRADE_FIELDS, [])
        write_json(os.path.join(out, state_n), {"cash": cap, "positions": [], "started": hist[0]["timestamp"] if hist else None,
                                                 "neuberechnung": {"strategie": STRATEGIE,
                                                                   "skript": "scripts/neuberechnung.py"}})
        summary["fonds"][key] = {"label": label, "start": cap, "nav": cap, "terminierungswert": cap, "kaeufe": 0,
                                 "auszahlungen": 0, "offen": 0, "gebunden_usd": 0.0}
    write_json(os.path.join(out, "zusammenfassung.json"), summary)
    return summary, per_pair


# ---------------------------------------------------------------------------- Uebernahme in die Live-Fonds
def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def uebernehmen(data_dir=DATA):
    """Einmalig: aktuelle Live-Dateien der sechs neu berechneten Fonds nach data/archiv/vor_reparatur_2026-10-27/
    sichern (nicht loeschen) und durch die korrigierte Reihe ersetzen. Die Live-Fonds laufen danach mit der neuen
    Strategie weiter. Ein zweiter Aufruf sichert nichts doppelt (vorhandene Sicherungsdateien bleiben)."""
    arch = os.path.join(data_dir, ARCHIV_SUB)
    os.makedirs(arch, exist_ok=True)
    names = [n for f in CROSS_FUNDS + POLY_FUNDS for n in f[2:5]]
    for n in names:
        src, dst = os.path.join(data_dir, n), os.path.join(arch, n)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)
    with open(os.path.join(arch, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as f:
        for n in sorted(os.listdir(arch)):
            if n.endswith((".csv", ".json")):
                f.write(f"{_sha(os.path.join(arch, n))}  {n}\n")
    with open(os.path.join(arch, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Sicherung vor der Reparatur vom 27.10.2026\n\nUnveraenderter Stand der Fonds-Dateien unmittelbar vor "
                "der Uebernahme der Neuberechnung (`scripts/neuberechnung.py --uebernehmen`). Diese Reihen enthalten "
                "die Fehler, die die Reparatur behebt (Fehlpaarung Lamar vs. Southeastern Louisiana, nicht "
                "abrechenbare Sportbeine, Schaetzkurs-Positionen, Bewertung zu Ask-Kursen, fehlende Polymarkt-"
                "Gebuehren). Sie bleiben zum Nachvollziehen erhalten und werden nicht mehr fortgeschrieben.\n\n"
                "Die gueltige Reihe steht wieder unter `data/` (fortgeschrieben von den Workflows) und als "
                "eingefrorene Neuberechnung unter `data/korrigiert/`. Pruefsummen: SHA256SUMS.txt.\n")
    out = os.path.join(data_dir, OUT_SUB)
    for n in names:
        shutil.copy2(os.path.join(out, n), os.path.join(data_dir, n))


def write_markdown(data_dir, summary, per_pair):
    s = summary
    lines = ["# Neuberechnung der Fonds (automatisch erzeugt von scripts/neuberechnung.py)", "",
             "Nur Papierhandel, keine Anlageberatung. Strategie: echte Arbitrage nach `scripts/arbitrage_check.py`.", "",
             f"- Scans ausgewertet: {s['scans']}, Trefferzeilen: {s['statistik']['zeilen']}",
             f"- verworfen: Kursunterschied ausserhalb {ac.MIN_EDGE:.0%}-{ac.MAX_PLAUSIBLE_SPREAD:.0%}: "
             f"{s['statistik']['kursunterschied_ausserhalb']}, Schaetzkurs (vor 14.10.): {s['statistik']['schaetzkurs_vor_14_10']}, "
             f"Matcher-Gegenbeweis: {s['statistik']['matcher_gegenbeweis']}, Regeln (Text): {s['statistik']['regel_text']}, "
             f"Kalshi-Sport: {s['statistik']['kalshi_sport']}, KI nicht bestaetigt: {s['statistik']['ki_nicht_bestaetigt']}",
             f"- Kandidaten-Paare (KI bestaetigt, Textregeln bestanden): {s['paare_kandidaten']}, davon automatisch "
             f"bestanden: {s['paare_bestanden_automatisch']}, nach Pruefung von Hand echt: {s['paare_echt_nach_pruefung']}",
             f"- Polymarkt-Einzelmarkt (Konservativ/Aggressiv/Best Case): {s['polymarkt_einzelmarkt']['ausfuehrbar_unter_eins']} "
             f"ausfuehrbare Treffer von {s['polymarkt_einzelmarkt']['snapshot_zeilen']} Snapshot-Zeilen", "",
             "| Fonds | Start | NAV (Anschaffungswert) | garantierter Endwert | Kaeufe | Auszahlungen | offen | gebunden USD |",
             "|---|---|---|---|---|---|---|---|"]
    for f in s["fonds"].values():
        lines.append(f"| {f['label']} | {f['start']:.0f} | {f['nav']:.2f} | {f['terminierungswert']:.2f} | {f['kaeufe']} | "
                     f"{f['auszahlungen']} | {f['offen']} | {f['gebunden_usd']:.2f} |")
    if s["empfindlichkeit"]:
        lines += ["", "Empfindlichkeit (gleiche Reihe, mehr Tiefe je Bein angenommen):", "",
                  "| Fonds@Bein-Deckel USD | NAV | garantierter Endwert | Kaeufe |", "|---|---|---|---|"]
        for k, v in s["empfindlichkeit"].items():
            lines.append(f"| {k} | {v['nav']:.2f} | {v['terminierungswert']:.2f} | {v['kaeufe']} |")
    lines += ["", "## Kandidaten-Paare", "", "| Paar | min. Summe | Zeilen | bestanden | von Hand | Grund (Beispiel) |",
              "|---|---|---|---|---|---|"]
    for e in sorted(per_pair.values(), key=lambda e: (-e["bestanden"], e["min_summe"])):
        lines.append(f"| {e['frage_a'][:60]} / {e['frage_b'][:60]} | {e['min_summe']:.3f} | {e['zeilen']} | {e['bestanden']} | "
                     f"{e['pruefung_von_hand'] or '-'} | {e['grund_beispiel'][:120]} |")
    lines += ["", "Grenzen der Rekonstruktion: siehe Moduldoku von scripts/neuberechnung.py.", ""]
    with open(os.path.join(data_dir, OUT_SUB, "zusammenfassung.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--abrufen", action="store_true", help="Quellen ueber die oeffentlichen APIs holen")
    ap.add_argument("--uebernehmen", action="store_true", help="Live-Fonds archivieren und ersetzen (einmalig)")
    ap.add_argument("--ohne-empfindlichkeit", action="store_true")
    a = ap.parse_args(argv)
    summary, per_pair = run(a.data, fetch=a.abrufen, sensitivity=not a.ohne_empfindlichkeit)
    write_markdown(a.data, summary, per_pair)
    if a.uebernehmen:
        uebernehmen(a.data)
    for k, f in summary["fonds"].items():
        print(f"{f['label']:<28} NAV {f['nav']:>10.2f}  Endwert {f['terminierungswert']:>10.2f}  Kaeufe {f['kaeufe']:>3}  offen {f['offen']}")
    print(f"Kandidaten-Paare {summary['paare_kandidaten']}, automatisch bestanden {summary['paare_bestanden_automatisch']}, "
          f"echt nach Pruefung {summary['paare_echt_nach_pruefung']}")


if __name__ == "__main__":
    main()
