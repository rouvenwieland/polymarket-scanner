"""
Rueckwirkende Bereinigung der 9 Papier-Fonds (ohne Neustart)
=============================================================

Aufruf:   python scripts/bereinigung.py [--data data] [--dry-run]

Was passiert (alles deterministisch und IDEMPOTENT - mehrfaches Ausfuehren ergibt denselben Stand):

 1. SICHERUNG: Die Originale (fund_trades/history/state) werden einmalig nach
    data/archiv/vor_bereinigung/ kopiert (+ SHA256SUMS.txt). Existiert die Sicherung, werden vorhandene
    Zeilen NIE veraendert; neue Rohzeilen spaeterer Workflow-Laeufe werden nur angehaengt (append-only).
 2. EINGABE = Sicherung (Originale) + alle Live-Zeilen, die noch nicht in der Sicherung stehen
    (z. B. neue Workflow-Laeufe seit der Bereinigung). Jede Handelszeile hat den Schluessel
    (Zeitstempel, Aktion, ID, n-tes Vorkommen).
 3. KLASSIFIKATION je Lebenszyklus (KAUF ... AUSZAHLUNG/VERKAUF/UMSCHICHTUNG, alles-oder-nichts):
      behalten | korrigieren | entfernen     - Regeln und Begruendungen: siehe classify_*().
 4. AUSGABE: bereinigte trades/history/state (Kasse = Start + Summe der verbleibenden Trades),
    Entscheidungsprotokoll data/bereinigung/entscheidungen_trades.csv (je Trade mit Grund),
    entscheidungen_lebenszyklen.csv, zusammenfassung.csv/.md.

Mergebarkeit mit laufenden Workflows (die alle paar Stunden neue Zeilen nach main committen):
  Nach dem Merge von origin/main in den Bereinigungs-Zweig koennen die Fonds-Dateien in data/ konfliktieren
  (beide Seiten haben dieselben Dateien veraendert). Loesung: fuer data/fund_* die Version von origin/main
  nehmen (`git checkout --theirs data/fund_*` bzw. `git checkout origin/main -- data/fund_*`) und dieses
  Skript erneut ausfuehren. Es erkennt neue Zeilen ueber den Schluessel, wendet dieselben Regeln an und
  baut Kasse/Positionen/History neu auf. Die Sicherung (data/archiv/...) wird nur ergaenzt, nie veraendert
  (neue Originaldateien, die dort fehlen, werden nachgetragen).

NAV-Rueckrechnung: History-Zeilen, deren Kasse nach der Bereinigung NICHT mehr zur Handelsliste passt
(Abweichung > 0,02), werden neu berechnet: Positionen zum EINSTANDSWERT (keine unrealisierten Kursgewinne),
Terminierungswert = Kasse + Anteile. Konsistente Zeilen bleiben unveraendert (inkl. ihrer Marktbewertung).
Der diskontierte Terminierungswert wird historisch NICHT rekonstruiert (end_date steht nicht in den
Trades) und gleich dem Terminierungswert gesetzt.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cross_platform as cp  # noqa: E402
import pricing  # noqa: E402

TOLERANCE = 0.02                 # erlaubte Abweichung Kasse vs. Handelsliste (USD)
MAX_PLAUSIBLE_SPREAD = 0.25      # Cross-Paare mit "Spread" > 25 % beim Einstieg gelten als Daten-/Fehlmatch-Artefakt
MAX_EFFECTIVE_BUY_PRICE = 0.999  # wie fund_simulator
MIN_TRADE_NOTIONAL = 2.0         # wie fund_simulator
CLOSERS = {"AUSZAHLUNG", "VERKAUF", "UMSCHICHTUNG"}
ARCHIVE_SUB = os.path.join("archiv", "vor_bereinigung")
OUT_SUB = "bereinigung"

FUNDS = [
    {"key": "conservative", "kind": "poly", "start": 100.0, "state": "fund_state.json",
     "trades": "fund_trades.csv", "history": "fund_history.csv", "idcol": "market_id", "label": "Konservativ"},
    {"key": "aggressive", "kind": "poly", "start": 100.0, "state": "fund_state_aggressive.json",
     "trades": "fund_trades_aggressive.csv", "history": "fund_history_aggressive.csv", "idcol": "market_id", "label": "Aggressiv"},
    {"key": "best_case", "kind": "poly", "start": 100.0, "state": "fund_state_bestcase.json",
     "trades": "fund_trades_bestcase.csv", "history": "fund_history_bestcase.csv", "idcol": "market_id", "label": "Best Case"},
    {"key": "cross_platform", "kind": "cross", "start": 100.0, "state": "fund_state_crossplatform.json",
     "trades": "fund_trades_crossplatform.csv", "history": "fund_history_crossplatform.csv", "idcol": "pair_id", "label": "Cross 100"},
    {"key": "cross_platform_1k", "kind": "cross", "start": 1000.0, "state": "fund_state_crossplatform_1k.json",
     "trades": "fund_trades_crossplatform_1k.csv", "history": "fund_history_crossplatform_1k.csv", "idcol": "pair_id", "label": "Cross 1.000"},
    {"key": "cross_platform_10k", "kind": "cross", "start": 10000.0, "state": "fund_state_crossplatform_10k.json",
     "trades": "fund_trades_crossplatform_10k.csv", "history": "fund_history_crossplatform_10k.csv", "idcol": "pair_id", "label": "Cross 10.000"},
    {"key": "kalshi_only", "kind": "solo", "start": 100.0, "state": "fund_state_kalshi.json",
     "trades": "fund_trades_kalshi.csv", "history": "fund_history_kalshi.csv", "idcol": "market_id", "label": "Kalshi Solo"},
    {"key": "predictit_only", "kind": "solo", "start": 100.0, "state": "fund_state_predictit.json",
     "trades": "fund_trades_predictit.csv", "history": "fund_history_predictit.csv", "idcol": "market_id", "label": "PredictIt Solo"},
    {"key": "sxbet_only", "kind": "solo", "start": 100.0, "state": "fund_state_sxbet.json",
     "trades": "fund_trades_sxbet.csv", "history": "fund_history_sxbet.csv", "idcol": "market_id", "label": "SX Bet Solo"},
]

EXTRA_TRADE_COLS = ["preisbasis", "bereinigung"]


# ----------------------------------------------------------------------------- Ein-/Ausgabe
def read_csv(path):
    if not os.path.isfile(path):
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
        return rows, list(r.fieldnames or [])


def write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def read_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sha256(path):
    """SHA-256 ueber den Inhalt MIT auf LF normalisierten Zeilenenden (Git speichert LF; unter Windows liegt
    die Arbeitskopie als CRLF vor - so stimmt die Pruefsumme auf jedem System)."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def ts(s):
    return datetime.fromisoformat(s)


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------- Sicherung
def superseded(fund, data_dir):
    """Reparatur 27.10.2026: Fonds, deren Live-State die Marke "neuberechnung" traegt, wurden von
    scripts/neuberechnung.py vollstaendig neu berechnet. Diese Bereinigung (14.10.) darf sie nicht mehr anfassen: sie
    wuerde die Zeilen ihrer Sicherung (vor_bereinigung) wieder mit den neuen Daten vereinigen."""
    st = read_json(os.path.join(data_dir, fund["state"]))
    return bool(st and st.get("neuberechnung"))


def archive_originals(data_dir):
    """Kopiert fehlende Originaldateien einmalig; vorhandene Sicherungsdateien bleiben unveraendert."""
    arch = os.path.join(data_dir, ARCHIVE_SUB)
    os.makedirs(arch, exist_ok=True)
    copied = []
    for fund in FUNDS:
        if superseded(fund, data_dir):
            continue
        for name in (fund["trades"], fund["history"], fund["state"]):
            src, dst = os.path.join(data_dir, name), os.path.join(arch, name)
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copy2(src, dst)
                copied.append(name)
    copied += extend_archive(data_dir)
    sums = {}
    for name in sorted(os.listdir(arch)):
        if name.endswith(".csv") or name.endswith(".json"):
            sums[name] = sha256(os.path.join(arch, name))
    with open(os.path.join(arch, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as f:
        for name, h in sums.items():
            f.write(f"{h}  {name}\n")
    readme = os.path.join(arch, "README.md")
    if not os.path.isfile(readme):
        with open(readme, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Sicherung vor der Bereinigung\n\nUnveraenderte Originale der Fonds-Dateien (trades, history, state) "
                    "vom Stand vor `scripts/bereinigung.py`. Vorhandene Zeilen werden nie veraendert; neue Rohzeilen spaeterer "
                    "Workflow-Laeufe werden nur ANGEHAENGT (append-only, siehe extend_archive). Pruefsummen: SHA256SUMS.txt. "
                    "Die Bereinigung berechnet ihren Stand immer aus dieser Sicherung. Damit die Sicherung fuer sich "
                    "konsistent bleibt, bucht das Anhaengen die Betraege auch auf die Kasse in den state-Dateien und "
                    "entfernt Positionen, deren Lebenszyklus inzwischen geschlossen ist (Kasse = Start + Summe der "
                    "Handelsliste, Positionen = offene Lebenszyklen).\n")
    return copied


def extend_archive(data_dir):
    """Haengt Live-ROHZEILEN, die noch nicht in der Sicherung stehen (neue Workflow-Laeufe), append-only an die
    Sicherung an (Trades und offene Positionen). Bereits vorhandene Originalzeilen werden nie veraendert. So bleibt
    die Eingabe der Bereinigung reproduzierbar und ein erneuter Lauf liefert dasselbe Protokoll.
    Die Sicherung bleibt dabei FUER SICH konsistent: Kasse der Sicherung = Start + Summe ihrer Handelsliste
    (angehaengte Betraege werden auf ihre Kasse gebucht) und ihre Positionen = offene Lebenszyklen ihrer Handelsliste
    (angehaengte Schliessungen entfernen die Position). Frueher wurden nur Zeilen und Positionen angehaengt, die Kasse
    nicht: nach den zwei Kaeufen des Snapshots ceddafb (14.10.) wich die Best-Case-Sicherung um 100 USD ab.
    Positionen und Lebenszyklen gehoeren ueber (ID, Einstiegszeit == Zeit des KAUF) zusammen, nicht ueber die ID allein:
    kauft der Workflow ein Paar erneut, dessen erster Kauf die Bereinigung aus den Live-Daten entfernt hat (15.10.), ist
    die ID in der Sicherung zweimal offen und braucht zwei Positionen (match_positions)."""
    touched = []
    for fund in FUNDS:
        if superseded(fund, data_dir):
            continue
        arch_t = os.path.join(data_dir, ARCHIVE_SUB, fund["trades"])
        live_t = os.path.join(data_dir, fund["trades"])
        idcol = fund["idcol"]
        appended, a_rows = [], None
        if os.path.isfile(arch_t) and os.path.isfile(live_t):
            a_rows, a_fields = read_csv(arch_t)
            l_rows, l_fields = read_csv(live_t)
            a_keys = set(row_keys(a_rows, idcol))
            appended = [r for r, k in zip(l_rows, row_keys(l_rows, idcol)) if k not in a_keys]
            if appended:
                fields = list(a_fields) + [f for f in l_fields if f not in a_fields]
                a_rows = a_rows + appended
                write_csv(arch_t, fields, a_rows)
                touched.append(fund["trades"])
        arch_s = os.path.join(data_dir, ARCHIVE_SUB, fund["state"])
        live_s = os.path.join(data_dir, fund["state"])
        if os.path.isfile(arch_s) and os.path.isfile(live_s):
            a, l = read_json(arch_s), read_json(live_s)
            before = json.dumps(a, sort_keys=True)
            if appended:
                a["cash"] = a.get("cash", fund["start"]) + sum(num(r["betrag"]) for r in appended)
            # Eine Position ist ueber (ID, Einstiegszeit) bestimmt, nicht ueber die ID allein: dasselbe Paar kann nach
            # dem Entfernen seines ersten Kaufs erneut gekauft werden und ist dann zweimal offen.
            have = {position_key(p, idcol) for p in a.get("positions", [])}
            positions = a.get("positions", []) + [p for p in l.get("positions", [])
                                                  if not p.get("bereinigt") and position_key(p, idcol) not in have]
            if a_rows is not None:
                open_lcs = [lc for lc in lifecycles(a_rows, idcol) if not lc["closed"] and lc["rows"]]
                matched = {id(p) for _lc, p in match_positions(open_lcs, positions, idcol) if p is not None}
                positions = [p for p in positions if id(p) in matched]
            a["positions"] = positions
            if json.dumps(a, sort_keys=True) != before:
                write_json(arch_s, a)
                touched.append(fund["state"])
    return touched


# ----------------------------------------------------------------------------- Lebenszyklen
def row_keys(rows, idcol):
    seen, keys = {}, []
    for r in rows:
        base = (r["timestamp"], r["aktion"], r[idcol])
        seen[base] = seen.get(base, 0) + 1
        keys.append(base + (seen[base],))
    return keys


def position_id(p, idcol):
    return p.get(idcol) or p.get("pair_id") or p.get("market_id")


def position_key(p, idcol):
    """Identitaet einer Position: (ID, Einstiegszeit). Die Einstiegszeit ist der Zeitstempel des KAUF (der Workflow
    schreibt beides mit demselben now.isoformat()). Nur die ID reicht nicht: dasselbe Paar kann mehrfach offen sein."""
    return (position_id(p, idcol), p.get("eingestiegen"))


def match_positions(open_lcs, positions, idcol):
    """Ordnet jedem offenen Lebenszyklus (Reihenfolge wie uebergeben) hoechstens eine Position zu und gibt
    [(lc, position oder None), ...] zurueck. Erst exakt ueber (ID, Einstiegszeit == Zeit des KAUF), danach je ID der
    Reihe nach (aeltere/handgebaute Daten ohne Einstiegszeit). Jede Position wird hoechstens einmal vergeben."""
    free = list(range(len(positions)))
    chosen = [None] * len(open_lcs)
    for i, lc in enumerate(open_lcs):
        t = lc["rows"][0][1]["timestamp"]
        for j in free:
            if position_id(positions[j], idcol) == lc["id"] and positions[j].get("eingestiegen") == t:
                chosen[i] = j
                free.remove(j)
                break
    for i, lc in enumerate(open_lcs):
        if chosen[i] is None:
            for j in free:
                if position_id(positions[j], idcol) == lc["id"]:
                    chosen[i] = j
                    free.remove(j)
                    break
    return [(lc, positions[j] if j is not None else None) for lc, j in zip(open_lcs, chosen)]


def build_union(fund, data_dir):
    """Sicherung + Live-Zeilen, die dort nicht stehen. Gibt (zeilen, feldnamen, quelle_live_keys) zurueck."""
    arch_rows, arch_fields = read_csv(os.path.join(data_dir, ARCHIVE_SUB, fund["trades"]))
    live_rows, live_fields = read_csv(os.path.join(data_dir, fund["trades"]))
    idcol = fund["idcol"]
    arch_keys = set(row_keys(arch_rows, idcol))
    new_live = [r for r, k in zip(live_rows, row_keys(live_rows, idcol)) if k not in arch_keys]
    rows = arch_rows + new_live
    fields = list(arch_fields or live_fields)
    for f in live_fields:
        if f not in fields:
            fields.append(f)
    live_keys = set(row_keys(live_rows, idcol))
    return rows, fields, live_keys


def lifecycles(rows, idcol):
    """Sequenzielle Lebenszyklen je ID. Gibt Liste von dicts {id, rows:[(index,row)], closed:bool}."""
    open_lc, out = {}, []
    for i, r in enumerate(rows):
        pid = r[idcol]
        if r["aktion"] == "KAUF":
            lc = {"id": pid, "rows": [(i, r)], "closed": False}
            open_lc[pid] = lc
            out.append(lc)
        else:
            lc = open_lc.get(pid)
            if lc is None:  # Schliessung ohne Eroeffnung (sollte nicht vorkommen) -> eigener Zyklus
                lc = {"id": pid, "rows": [], "closed": False, "orphan": True}
                out.append(lc)
                open_lc[pid] = lc
            lc["rows"].append((i, r))
            if r["aktion"] in CLOSERS:
                lc["closed"] = True
                open_lc.pop(pid, None)
    return out


# ----------------------------------------------------------------------------- Klassifikation
PAIR_RE = re.compile(r"^\[(\w+)\] (.*?)\s+<->\s+\[(\w+)\] (.*)$")


def split_pair(frage):
    m = PAIR_RE.match(frage or "")
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def counter_evidence(qa, qb):
    """Aktuelle harte Gegenbeweise des Matchings (cross_platform.py) auf die Fragen eines Paars."""
    a, b = qa.lower(), qb.lower()
    ta, tb = cp._tokenize(qa), cp._tokenize(qb)
    checks = [
        ("Jahre widersprechen sich", cp._conflicting_years(a, b)),
        ("Bezirks-/Prozent-Code widerspricht", cp._conflicting_specifics(a, b)),
        ("Subjekt/Kandidat widerspricht", cp._conflicting_subjects(a, b)),
        ("Amtstyp/Sitzzahl widerspricht", cp._conflicting_race_type(a, b)),
        ("Spielpaarung widerspricht (generischer Titel ohne Teams?)", cp._conflicting_matchup(a, b, ta, tb)),
        ("Wettart widerspricht", cp._conflicting_bet_aspect(a, b)),
        ("UMGEKEHRTE POLARITAET (Yes/No vertauscht, keine Absicherung)", cp._opposite_polarity(a, b)),
        ("generischer Titel ohne Teams/Namen kann kein konkretes Spiel identifizieren", cp._conflicting_generic(ta, tb)),
    ]
    return [name for name, hit in checks if hit]


def kauf_values(lc):
    kauf = lc["rows"][0][1]
    shares = num(kauf["anteile"])
    return kauf, shares, num(kauf["preis"]), -num(kauf["betrag"])


def classify_poly(lc):
    kauf = lc["rows"][0][1] if lc["rows"] else {}
    basis = pricing.basis_of(kauf)
    if basis != pricing.BASIS_EXECUTABLE:
        return ("entfernen",
                "Schaetzkurs-Artefakt: Yes+No<1 im einzelnen Markt auf Mittel-/Letztkurs (outcomePrices). Auf ausfuehrbaren "
                "Preisen ist die Kauf-Summe bei gueltigem Orderbuch immer >= 1 (Lean: book_buy_sum_ge_one), der Trade war "
                "so nicht ausfuehrbar.", 0.0)
    return ("behalten", "Preisbasis ausfuehrbar (Orderbuch-Ask)", 0.0)


def impact_correction(lc, pi_fee_ps=0.0):
    """Nachtraeglich anzurechnender Kaufimpact. REGEL: der KASSENABFLUSS des Trades bleibt unveraendert (so bleibt die
    Kasse nie negativ und alles mergebar); der Impact verteuert stattdessen jeden Anteil, die Stueckzahl sinkt.
    Modell wie cross_platform_fund: Kosten(s) = s*c1 + q*s^2, c1 = Summe + Gebuehr/Anteil, q = headroom/Cap
    (Cap aus der KAUF-Notiz, gerundet -> Naeherung). Loest q*s'^2 + c1*s' = C (C = urspruenglicher Abfluss).
    Gibt (neue_anteile, notiz) zurueck; neue_anteile == alte, wenn nicht korrigierbar."""
    kauf, shares, summe, total_cost = kauf_values(lc)
    if shares <= 0:
        return shares, "keine Stueckzahl"
    m = re.search(r"Edge-Limit (\d+) Anteile", kauf.get("notiz", ""))
    if not m or int(m.group(1)) <= 0:
        return shares, "kein Edge-Limit in der Notiz (Cap unbekannt) -> keine Korrektur moeglich"
    cap = int(m.group(1))
    c1 = total_cost / shares                      # Summe + Gebuehr je Anteil (aus dem tatsaechlichen Abfluss)
    fee_ps = max(0.0, c1 - summe)
    headroom = MAX_EFFECTIVE_BUY_PRICE - 1e-6 - summe - fee_ps - pi_fee_ps  # wie _max_shares_for_edge (inkl. PredictIt-Worst-Case)
    if headroom <= 0:
        return shares, "kein Spielraum bis 0,999"
    q = headroom / cap
    s_new = (-c1 + math.sqrt(c1 * c1 + 4 * q * total_cost)) / (2 * q)
    return min(shares, s_new), f"Naeherung: Cap {cap} (gerundet), Auslastung {min(1.0, shares / cap):.2f}"


def classify_cross(lc, state_pos):
    kauf, shares, summe, total_cost = kauf_values(lc)
    pair = split_pair(kauf.get("frage", ""))
    if pair is None:
        return ("behalten", "Frage nicht parsebar - ungeprueft behalten (Unsicherheit)", 0.0, "ungeprueft")
    pa, qa, pb, qb = pair
    hits = counter_evidence(qa, qb)
    if hits:
        return ("entfernen", "Markt-Zuordnung nach heutigen Regeln widerlegt: " + "; ".join(hits), 0.0, "")
    m = re.search(r"Spread ([\d.]+)%", kauf.get("notiz", ""))
    spread = float(m.group(1)) / 100 if m else 1 - summe
    if spread > MAX_PLAUSIBLE_SPREAD:
        return ("entfernen",
                f"Unplausibler Spread {spread*100:.1f}% (> {MAX_PLAUSIBLE_SPREAD*100:.0f}%): Polymarkt-Seite vermutlich Platzhalter-/"
                "Schaetzkurs ohne Gegenseite (z. B. 0,50 bei neuem Markt), nicht ausfuehrbar", 0.0, "")
    worst_fee = 0.0
    if state_pos is not None and not lc["closed"]:
        la, lb = state_pos["leg_a"], state_pos["leg_b"]
        worst_fee = max(
            0.1 * max(0.0, 1 - la["einstandskurs"]) if la["platform"] == "predictit" else 0.0,
            0.1 * max(0.0, 1 - lb["einstandskurs"]) if lb["platform"] == "predictit" else 0.0,
        )
        fees_ps = (la.get("fee_gezahlt", 0) + lb.get("fee_gezahlt", 0)) / max(shares, 1e-9)
        worst = 1 - (la["einstandskurs"] + lb["einstandskurs"]) - worst_fee - fees_ps
        if worst < -0.005:
            return ("entfernen", f"Garantierter Verlust in einem Ausgang: schlechtester Fall {worst*100:+.1f} Cent/Anteil "
                    "(PredictIt-Gewinngebuehr nicht eingepreist)", 0.0, "")
    basis = pricing.basis_of(kauf, default=None)
    if basis is None:
        plats = {pa, pb}
        basis = pricing.BASIS_ESTIMATE if "polymarket" in plats else pricing.BASIS_EXECUTABLE
    shares_new, note = shares, ""
    legacy = not (kauf.get("preisbasis") or kauf.get("bereinigung"))
    if legacy:
        shares_new, note = impact_correction(lc, worst_fee)
    if shares_new < shares * (1 - 1e-6):
        return ("korrigieren", f"Kaufimpact war nicht eingepreist: bei gleichem Kassenabfluss nur {shares_new:.3f} statt "
                f"{shares:.3f} Anteile ({note})", shares_new, basis)
    return ("behalten", "Zuordnung bestaetigt (keine Gegenbeweise), Spread plausibel" +
            ("; " + note if note else ""), 0.0, basis)


# ----------------------------------------------------------------------------- Kern
def process_fund(fund, data_dir):
    idcol, start = fund["idcol"], fund["start"]
    rows, fields, live_keys = build_union(fund, data_dir)
    live_state = read_json(os.path.join(data_dir, fund["state"]))
    arch_state = read_json(os.path.join(data_dir, ARCHIVE_SUB, fund["state"]))
    state_src = live_state or arch_state

    # Klassifikation nutzt NUR unbereinigte Positionsdaten (Sicherung, sonst Live-Stand ohne Bereinigungsmarke),
    # sonst waere ein zweiter Lauf nicht identisch. Die Ausgabe nimmt die Live-Position (aktuelle Marken).
    # Positionen und Lebenszyklen werden ueber (ID, Einstiegszeit) zugeordnet, nicht ueber die ID allein: dasselbe Paar
    # kann nach dem Entfernen seines ersten Kaufs erneut gekauft worden sein (zwei offene Lebenszyklen, zwei Positionen).
    cls_by_key, out_by_key = {}, {}
    for p in (arch_state or {}).get("positions", []):
        cls_by_key[position_key(p, idcol)] = p
        out_by_key[position_key(p, idcol)] = p
    for p in (live_state or {}).get("positions", []):
        out_by_key[position_key(p, idcol)] = p
        if not p.get("bereinigt") and position_key(p, idcol) not in cls_by_key:
            cls_by_key[position_key(p, idcol)] = p

    lcs = lifecycles(rows, idcol)
    open_lcs = [lc for lc in lcs if not lc["closed"] and lc["rows"]]
    cls_pos = {id(lc): p for lc, p in match_positions(open_lcs, list(cls_by_key.values()), idcol)}
    decisions = {}  # row index -> (decision, reason, extra, basis)
    lc_info = []
    for lc in lcs:
        if fund["kind"] == "poly" or fund["kind"] == "solo":
            dec, reason, extra = classify_poly(lc)
            basis = pricing.basis_of(lc["rows"][0][1]) if lc["rows"] else pricing.BASIS_ESTIMATE
        else:
            dec, reason, extra, basis = classify_cross(lc, cls_pos.get(id(lc)))
        n = len(lc["rows"])
        for j, (i, r) in enumerate(lc["rows"]):
            decisions[i] = (dec, reason, extra, basis)
        lc_info.append({"lc": lc, "decision": dec, "reason": reason, "extra": extra, "basis": basis})

    # bereinigte Trades
    kept_rows, log_rows = [], []
    cash = start
    # --- Kassenzwang: Das Entfernen frueher (gewinnbringender) Trades nimmt spaeteren Kaeufen ihre Finanzierung.
    # Chronologisch nachrechnen; reicht die Kasse nicht, werden die Kaeufe eines Laufs proportional verkleinert
    # (Anteile und Kassenabfluss mal f, Auszahlungen proportional). Unter MIN_TRADE_NOTIONAL -> entfernen.
    lc_of_row = {}
    for info in lc_info:
        info["s_old"] = num(info["lc"]["rows"][0][1]["anteile"]) if info["lc"]["rows"] else 0.0
        info["s_impact"] = info["extra"] if info["decision"] == "korrigieren" else info["s_old"]
        info["f"] = 1.0
        for (i, _r) in info["lc"]["rows"]:
            lc_of_row[i] = info
    sim_cash = start
    i = 0
    while i < len(rows):
        info = lc_of_row[i]
        r = rows[i]
        if info["decision"] == "entfernen":
            i += 1
            continue
        if r["aktion"] == "KAUF":
            batch = [i]
            j = i + 1
            while j < len(rows) and rows[j]["aktion"] == "KAUF" and rows[j]["timestamp"] == r["timestamp"]:
                batch.append(j)
                j += 1
            batch = [k for k in batch if lc_of_row[k]["decision"] != "entfernen"]
            while batch:
                need = sum(-num(rows[k]["betrag"]) for k in batch)
                f = 1.0 if need <= sim_cash + 1e-9 else max(0.0, sim_cash) / need
                tiny = [k for k in batch if f < 1.0 and -num(rows[k]["betrag"]) * f < MIN_TRADE_NOTIONAL]
                if not tiny:
                    break
                for k in tiny:
                    lc_of_row[k]["decision"] = "entfernen"
                    lc_of_row[k]["reason"] = ("Nach Entfernen der Vorgaenger-Trades reicht die Kasse nicht fuer diesen Kauf "
                                              "(Mindestgroesse 2 USD unterschritten); Finanzierung stammte aus nicht ausfuehrbaren Gewinnen")
                batch = [k for k in batch if k not in tiny]
            for k in batch:
                lc_of_row[k]["f"] = f
                sim_cash -= -num(rows[k]["betrag"]) * f
                if f < 1.0 - 1e-9:
                    if lc_of_row[k]["decision"] == "behalten":
                        lc_of_row[k]["decision"] = "korrigieren"
                    lc_of_row[k]["reason"] += (f"; Kasse reichte nach Bereinigung nur fuer {f*100:.0f} % des Kaufs "
                                               "(Vorgaenger-Gewinne entfernt) -> Position proportional verkleinert")
            i = max(batch + [i]) + 1 if batch else j
            continue
        # Schliessung (AUSZAHLUNG/VERKAUF/UMSCHICHTUNG)
        scale = info["f"] * (info["s_impact"] / info["s_old"] if info["s_old"] else 1.0)
        sim_cash += num(r["betrag"]) * scale
        i += 1
    for info in lc_info:
        info["s_final"] = info["s_impact"] * info["f"]
        for (i, _r) in info["lc"]["rows"]:
            decisions[i] = (info["decision"], info["reason"], info["extra"], info["basis"])

    delta_live = 0.0  # Kassenaenderung gegenueber dem Live-Stand (nur Zeilen, die live stehen)
    live_idx_keys = row_keys(rows, idcol)
    for i, r in enumerate(rows):
        dec, reason, extra, basis = decisions[i]
        info = lc_of_row[i]
        betrag_alt = num(r["betrag"])
        is_live = live_idx_keys[i] in live_keys
        if dec == "entfernen":
            log_rows.append({"fund": fund["key"], "timestamp": r["timestamp"], "aktion": r["aktion"], "id": r[idcol],
                             "frage": r["frage"], "betrag_alt": betrag_alt, "betrag_neu": "", "entscheidung": "entfernen",
                             "grund": reason})
            if is_live:
                delta_live -= betrag_alt
            continue
        new = dict(r)
        new["preisbasis"] = r.get("preisbasis") or basis
        new["bereinigung"] = r.get("bereinigung", "")
        changed = False
        s_old, s_final, f = info["s_old"], info["s_final"], info["f"]
        if s_old and abs(s_final - s_old) > 1e-9:
            changed = True
            new["anteile"] = round(s_final, 4)
            if r["aktion"] == "KAUF":
                new["betrag"] = round(betrag_alt * f, 4)  # Abflussfaktor f (Kassenzwang); Impact aendert nur die Stueckzahl
                new["preis"] = round(-num(new["betrag"]) / s_final, 4)
                new["notiz"] = (r.get("notiz", "") + f" | Bereinigung: {s_final:.3f} statt {s_old:.3f} Anteile").strip()
            else:
                new["betrag"] = round(betrag_alt * s_final / s_old, 4)
                if is_live:
                    delta_live += num(new["betrag"]) - betrag_alt
            new["bereinigung"] = "korrigiert"
        if r["aktion"] == "KAUF" and changed and is_live:
            delta_live += num(new["betrag"]) - betrag_alt
        cash += num(new["betrag"])
        new["kasse_danach"] = round(cash, 4)
        kept_rows.append(new)
        log_rows.append({"fund": fund["key"], "timestamp": r["timestamp"], "aktion": r["aktion"], "id": r[idcol],
                         "frage": r["frage"], "betrag_alt": betrag_alt, "betrag_neu": num(new["betrag"]),
                         "entscheidung": "korrigieren" if changed else "behalten", "grund": reason})

    out_fields = list(fields)
    for c in EXTRA_TRADE_COLS:
        if c not in out_fields:
            out_fields.append(c)

    # Zustand
    positions = []
    open_infos = [info for info in lc_info if not info["lc"]["closed"] and info["lc"]["rows"]]
    # Zuordnung ueber ALLE offenen Lebenszyklen (auch die entfernten), damit ein entfernter Kauf keiner behaltenen
    # Position derselben ID im Weg steht.
    out_pos = {id(lc): p for lc, p in match_positions([i["lc"] for i in open_infos], list(out_by_key.values()), idcol)}
    for info in open_infos:
        if info["decision"] == "entfernen":
            continue
        pid = info["lc"]["id"]
        p = out_pos.get(id(info["lc"]))
        if p is None:
            raise RuntimeError(f"{fund['key']}: offene Position {pid} fehlt im Zustand")
        p = json.loads(json.dumps(p))
        p["preisbasis"] = p.get("preisbasis") or info["basis"]
        if info["decision"] == "korrigieren" and not p.get("bereinigt"):
            pf = info["f"] * info["s_old"] / info["s_final"]   # Preisfaktor je Anteil (Impact bezahlt)
            p["shares"] = p["shares"] * info["s_final"] / info["s_old"]
            if "leg_a" in p:
                p["leg_a"]["einstandskurs"] *= pf
                p["leg_b"]["einstandskurs"] *= pf
                p["leg_a"]["fee_gezahlt"] = p["leg_a"].get("fee_gezahlt", 0) * info["f"]
                p["leg_b"]["fee_gezahlt"] = p["leg_b"].get("fee_gezahlt", 0) * info["f"]
            else:
                p["einstandskurs"] *= pf
        p["bereinigt"] = True
        positions.append(p)
    ledger_cash = start + sum(num(r["betrag"]) for r in kept_rows)
    base_cash = (state_src or {}).get("cash", start) if state_src else start
    # Kasse: Live-Stand + Aenderungen durch Entfernen/Korrigieren (bewahrt ungerundete Workflow-Kasse)
    if live_state is not None:
        new_cash = live_state["cash"] + delta_live
    else:
        new_cash = ledger_cash
    new_state = {"cash": new_cash, "positions": positions, "started": (state_src or {}).get("started")}
    if abs(new_cash - ledger_cash) > TOLERANCE:
        raise RuntimeError(f"{fund['key']}: Kasse {new_cash:.4f} weicht von Handelsliste {ledger_cash:.4f} ab")

    # History
    hist_rows, hist_fields = read_csv(os.path.join(data_dir, fund["history"]))
    kept_ts = [(ts(r["timestamp"]), num(r["betrag"])) for r in kept_rows]
    open_by_time = []
    for info in lc_info:
        if info["decision"] == "entfernen" or not info["lc"]["rows"]:
            continue
        rws = info["lc"]["rows"]
        kauf_new = next(k for k in kept_rows if k["timestamp"] == rws[0][1]["timestamp"] and k["aktion"] == "KAUF"
                        and k[idcol] == rws[0][1][idcol])
        t_open = ts(rws[0][1]["timestamp"])
        t_close = ts(rws[-1][1]["timestamp"]) if info["lc"]["closed"] else None
        shares = num(kauf_new["anteile"])
        cost = -num(kauf_new["betrag"])
        open_by_time.append((t_open, t_close, shares, cost, info["basis"]))
    new_hist = []
    for h in hist_rows:
        t = ts(h["timestamp"])
        cash_t = start + sum(b for tt, b in kept_ts if tt <= t)
        if abs(num(h["kasse"]) - cash_t) <= TOLERANCE:
            new_hist.append(h)
            continue
        opens = [(s, c, b) for (to, tc, s, c, b) in open_by_time if to <= t and (tc is None or tc > t)]
        pw = sum(c for s, c, b in opens)
        term = cash_t + sum(s for s, c, b in opens)
        nav = cash_t + pw
        n_est = sum(1 for s, c, b in opens if b != pricing.BASIS_EXECUTABLE)
        v_est = sum(c for s, c, b in opens if b != pricing.BASIS_EXECUTABLE)
        nh = dict(h)
        nh.update({
            "kasse": round(cash_t, 4), "positionswert": round(pw, 4), "nav": round(nav, 4),
            "rendite_pct": round((nav / start - 1) * 100, 3), "offene_positionen": len(opens),
            "terminierungswert": round(term, 4), "diskontierter_terminierungswert": round(term, 4),
            "diskontierte_rendite_pct": round((term / start - 1) * 100, 3),
            "positionen_schaetzung": n_est, "positionswert_schaetzung": round(v_est, 4), "bereinigt": 1,
        })
        new_hist.append(nh)
    out_hist_fields = list(hist_fields)
    for c in ("positionen_schaetzung", "positionswert_schaetzung", "bereinigt"):
        if c not in out_hist_fields and hist_rows:
            out_hist_fields.append(c)

    # Kennzahlen vorher/nachher
    def state_nav(st):
        if not st:
            return None
        pv = 0.0
        for p in st.get("positions", []):
            if "leg_a" in p:
                pv += p["shares"] * (p["leg_a"]["letzter_kurs"] + p["leg_b"]["letzter_kurs"])
            else:
                pv += p["shares"] * p.get("letzter_kurs", p["einstandskurs"])
        return st["cash"] + pv

    before_state = arch_state or live_state
    cnt = {"behalten": 0, "korrigieren": 0, "entfernen": 0}
    for lr in log_rows:
        cnt[lr["entscheidung"]] += 1
    lc_cnt = {"behalten": 0, "korrigieren": 0, "entfernen": 0}
    for info in lc_info:
        lc_cnt[info["decision"]] += 1
    summary = {
        "fund": fund["key"], "label": fund["label"], "start": start,
        "trades_gesamt": len(rows), "trades_behalten": cnt["behalten"], "trades_korrigiert": cnt["korrigieren"],
        "trades_entfernt": cnt["entfernen"],
        "lebenszyklen_behalten": lc_cnt["behalten"], "lebenszyklen_korrigiert": lc_cnt["korrigieren"],
        "lebenszyklen_entfernt": lc_cnt["entfernen"],
        "nav_vorher": round(state_nav(before_state), 4) if before_state else "",
        "nav_nachher": round(state_nav(new_state), 4),
        "nav_nachher_einstandswert": round(num(new_hist[-1]["nav"]), 4) if new_hist else "",
        "kasse_vorher": round(before_state["cash"], 4) if before_state else "",
        "kasse_nachher": round(new_cash, 4),
        "offene_positionen_vorher": len(before_state["positions"]) if before_state else 0,
        "offene_positionen_nachher": len(positions),
        "kasse_abweichung_handelsliste": round(new_cash - ledger_cash, 4),
    }
    lc_log = []
    for info in lc_info:
        rws = info["lc"]["rows"]
        if not rws:
            continue
        lc_log.append({
            "fund": fund["key"], "id": info["lc"]["id"], "frage": rws[0][1]["frage"],
            "eroeffnet": rws[0][1]["timestamp"], "geschlossen": rws[-1][1]["timestamp"] if info["lc"]["closed"] else "offen",
            "entscheidung": info["decision"], "preisbasis": info["basis"], "anteile_neu": round(info.get("s_final", 0), 4) if info["decision"] == "korrigieren" else "",
            "grund": info["reason"],
        })
    return {"kept_rows": kept_rows, "trade_fields": out_fields, "state": new_state, "hist": new_hist,
            "hist_fields": out_hist_fields, "log": log_rows, "lc_log": lc_log, "summary": summary,
            "has_trades": bool(rows)}


def run(data_dir="data", dry_run=False):
    if not dry_run:
        archive_originals(data_dir)
    results = []
    skipped = [f["label"] for f in FUNDS if superseded(f, data_dir)]
    for fund in FUNDS:
        if fund["label"] in skipped:
            continue
        have_any = any(os.path.isfile(os.path.join(data_dir, n)) for n in (fund["trades"], fund["state"]))
        if not have_any:
            continue
        if not dry_run:
            pass
        res = process_fund(fund, data_dir)
        res["fund"] = fund
        results.append(res)
    if dry_run:
        return results
    if skipped:
        print("Bereinigung uebersprungen (durch die Neuberechnung vom 27.10.2026 abgeloest): " + ", ".join(skipped))
    for res in results:
        fund = res["fund"]
        if res["has_trades"]:
            write_csv(os.path.join(data_dir, fund["trades"]), res["trade_fields"], res["kept_rows"])
        if res["hist"]:
            write_csv(os.path.join(data_dir, fund["history"]), res["hist_fields"], res["hist"])
        write_json(os.path.join(data_dir, fund["state"]), res["state"])
    out = os.path.join(data_dir, OUT_SUB)
    if skipped:
        return results  # Protokoll der Bereinigung vom 14.10. bleibt unveraendert (dokumentiert den damaligen Lauf)
    log_fields = ["fund", "timestamp", "aktion", "id", "frage", "betrag_alt", "betrag_neu", "entscheidung", "grund"]
    write_csv(os.path.join(out, "entscheidungen_trades.csv"), log_fields, [r for res in results for r in res["log"]])
    lc_fields = ["fund", "id", "frage", "eroeffnet", "geschlossen", "entscheidung", "preisbasis", "anteile_neu", "grund"]
    write_csv(os.path.join(out, "entscheidungen_lebenszyklen.csv"), lc_fields, [r for res in results for r in res["lc_log"]])
    sum_fields = list(results[0]["summary"].keys()) if results else []
    write_csv(os.path.join(out, "zusammenfassung.csv"), sum_fields, [res["summary"] for res in results])
    write_markdown(out, results)
    return results


def write_markdown(out, results):
    lines = ["# Bereinigung der Fonds (automatisch erzeugt von scripts/bereinigung.py)", "",
             "| Fonds | Trades gesamt | behalten | korrigiert | entfernt | Kasse vorher | Kasse nachher | NAV vorher | NAV nachher | Pos. vorher | Pos. nachher |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s = r["summary"]
        lines.append(f"| {s['label']} | {s['trades_gesamt']} | {s['trades_behalten']} | {s['trades_korrigiert']} | {s['trades_entfernt']} | "
                     f"{s['kasse_vorher']} | {s['kasse_nachher']} | {s['nav_vorher']} | {s['nav_nachher']} | "
                     f"{s['offene_positionen_vorher']} | {s['offene_positionen_nachher']} |")
    lines += ["", "Details je Trade: `entscheidungen_trades.csv`, je Lebenszyklus: `entscheidungen_lebenszyklen.csv`.",
              "Originale: `data/archiv/vor_bereinigung/` (Pruefsummen in SHA256SUMS.txt).", ""]
    with open(os.path.join(out, "zusammenfassung.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--dry-run", action="store_true", help="nur berechnen und Zusammenfassung ausgeben, nichts schreiben")
    a = ap.parse_args()
    results = run(a.data, a.dry_run)
    for r in results:
        s = r["summary"]
        print(f"{s['label']:<16} Trades {s['trades_gesamt']:>5}: behalten {s['trades_behalten']:>5}, korrigiert {s['trades_korrigiert']:>3}, "
              f"entfernt {s['trades_entfernt']:>5} | Kasse {s['kasse_vorher']} -> {s['kasse_nachher']} | NAV {s['nav_vorher']} -> {s['nav_nachher']}")


if __name__ == "__main__":
    main()
