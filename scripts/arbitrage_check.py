"""
Strategie-Pruefung vor jedem Cross-Platform-Kauf (Reparatur 27.10.2026)
==========================================================================

Echte Arbitrage heisst hier (und NUR das wird gekauft):

  1. Beide Beine gehoeren zu DEMSELBEN Ereignis (Matcher + KI-Pruefung, siehe cross_platform.py/llm_verify.py).
  2. Die Seiten sind ausdruecklich zugeordnet: was "yes" auf Plattform A bedeutet, ist bekannt und entspricht
     "yes" (Zuordnung "same") oder "no" (Zuordnung "flipped") auf Plattform B. Fehlt diese Zuordnung, gibt es
     keinen Kauf. Anlass: "Lamar vs. Southeastern Louisiana" (Polymarkt) gegen "Southeastern Louisiana vs Lamar"
     (SX Bet) - beide Beine setzten auf dasselbe Team, gebucht als 23-%-"Arbitrage" (17.10.2026).
  3. Die Aufloesungsregeln sind vergleichbar. Bekannte Unterschiede fuehren zum Ausschluss (rule_conflicts):
     Kopie-/Testmaerkte, Kandidatenlisten ("first listed", "of these"), plattformabhaengige Definitionen
     ("closest race", "most seats"), Sportwetten mit anderem Markttyp, anderer Anstosszeit oder unbekanntem
     Abbruch-Verfahren.
  4. Beide Preise sind ausfuehrbar (Orderbuch-Asks), der Kursunterschied liegt hoechstens bei
     MAX_PLAUSIBLE_SPREAD (10 %; groessere Luecken zwischen liquiden Plaetzen waren bisher immer Fehlpaarungen).
  5. In JEDEM moeglichen Ausgang (A gewinnt, B gewinnt, bei Sport zusaetzlich: Spiel abgesagt) bleibt nach
     ALLEN Gebuehren ein Gewinn von mindestens MIN_EDGE je Anteilspaar. Gebuehren:
       - Polymarkt: Taker-Gebuehr C x Satz x p x (1-p) je Kauf (feeSchedule des Markts, platforms/polymarket.py)
       - Kalshi: Taker-Gebuehr ceil(0,07 x C x p x (1-p)) je Kauf
       - PredictIt: 10 % auf den Gewinn des gewinnenden Beins
       - SX Bet: 2 % auf den Gewinn des gewinnenden Beins (vorsichtige Annahme, siehe platforms/sxbet.py)
     Abbruch eines Spiels: Polymarkt zahlt 0,5 je Anteil (50-50), SX Bet erstattet den Einsatz, Kalshi zahlt einen
     vorab unbekannten "fair price" und PredictIt ist unbekannt - beide werden mit 0 angesetzt (schlechtester Fall).
  6. Die garantierte Rendite schlaegt die Kapitalbindung: mindestens MIN_ANNUAL_RETURN pro Jahr bis zur
     spaetesten Aufloesung beider Beine. 3 % Vorsprung fuer zwei Jahre Bindung ist kein Geschaeft.

Ist eine Bedingung nicht erfuellt, gibt check_pair ok=False mit Begruendung zurueck: kein Kauf.
Im Zweifel kein Trade statt falscher Trade.
"""

import math
import re
from collections import namedtuple
from datetime import datetime, timezone

import cross_platform
import pricing
from platforms import kalshi, polymarket, sxbet

MAX_PLAUSIBLE_SPREAD = 0.10   # Kursunterschied (vor Gebuehren) hoechstens 10 %
MIN_EDGE = 0.03               # garantierter Gewinn je Anteilspaar im schlechtesten Ausgang, nach Gebuehren (USD)
MIN_ANNUAL_RETURN = 0.04      # Kapitalkosten-Huerde p.a. (etwa Zins fuer kurzlaufende Staatsanleihen)
MAX_START_GAP_HOURS = 3.0     # Sport: Anstosszeiten beider Maerkte duerfen hoechstens so weit auseinanderliegen

PROFIT_FEE_RATE = {"predictit": 0.10, "sxbet": sxbet.PROFIT_FEE_RATE}
# Auszahlung je Anteil, wenn ein Spiel abgesagt/nicht gewertet wird. "refund" = Einsatz zurueck. None = unbekannt.
CANCEL_PAYOUT = {"polymarket": 0.5, "sxbet": "refund", "kalshi": None, "predictit": None}

Check = namedtuple("Check", "ok reasons alignment side_a side_b price_a price_b summe spread edge days annual")


# ---------------------------------------------------------------- Gebuehren und Ausgaenge

def entry_fee(market, shares, price):
    """Kaufgebuehr in USD fuer `shares` Anteile eines Beins zum Preis `price`."""
    platform = market["platform"]
    if platform == "polymarket":
        return polymarket.taker_fee(shares, price, market.get("fee_rate", polymarket.UNKNOWN_FEE_RATE))
    if platform == "kalshi":
        return kalshi.taker_fee(shares, price)
    return 0.0


def entry_fee_per_share(market, price):
    """Gebuehr je Anteil ohne Cent-Rundung (fuer Grenzpreis-Rechnungen; die echte Rechnung nutzt entry_fee)."""
    platform = market["platform"]
    if platform == "polymarket":
        return market.get("fee_rate", polymarket.UNKNOWN_FEE_RATE) * price * (1.0 - price)
    if platform == "kalshi":
        return 0.07 * price * (1.0 - price)
    return 0.0


def settle_value(platform, payout, cost_per_share):
    """Netto-Auszahlung je Anteil nach Gewinngebuehr der Plattform."""
    rate = PROFIT_FEE_RATE.get(platform, 0.0)
    return payout - rate * max(0.0, payout - cost_per_share)


def _cancel_payout(platform, cost_per_share):
    rule = CANCEL_PAYOUT.get(platform)
    if rule == "refund":
        return cost_per_share
    return 0.0 if rule is None else float(rule)


def world_values(platform_a, platform_b, cost_a, cost_b, with_cancel):
    """Netto-Auszahlung je Anteilspaar in jedem Ausgang: {"A gewinnt": x, "B gewinnt": y[, "Absage": z]}.
    Voraussetzung: die Beine sind komplementaer (genau eines zahlt 1,00) - das stellt die Seitenzuordnung sicher."""
    out = {
        "A gewinnt": settle_value(platform_a, 1.0, cost_a) + settle_value(platform_b, 0.0, cost_b),
        "B gewinnt": settle_value(platform_a, 0.0, cost_a) + settle_value(platform_b, 1.0, cost_b),
    }
    if with_cancel:
        out["Absage"] = (settle_value(platform_a, _cancel_payout(platform_a, cost_a), cost_a)
                         + settle_value(platform_b, _cancel_payout(platform_b, cost_b), cost_b))
    return out


def worst_net_per_share(ma, mb, price_a, price_b, with_cancel):
    """Garantierter Gewinn je Anteilspaar (schlechtester Ausgang minus Kosten inkl. Kaufgebuehren)."""
    cost = price_a + price_b + entry_fee_per_share(ma, price_a) + entry_fee_per_share(mb, price_b)
    worlds = world_values(ma["platform"], mb["platform"], price_a, price_b, with_cancel)
    return min(worlds.values()) - cost


# ---------------------------------------------------------------- Regelvergleich

_COPY_RE = re.compile(r"\(copy\)|\[test\]|\btest market\b", re.IGNORECASE)
# Formulierungen, deren Aufloesung von einer plattformeigenen Liste/Definition abhaengt. Auf beiden Seiten
# gleich lautend heisst noch nicht gleich definiert (Kandidatenliste, Messgroesse "knappstes Rennen").
_DEFINITION_RE = {
    "kandidatenliste": re.compile(r"\bfirst listed\b|\bof these\b|\blisted\b", re.IGNORECASE),
    "knappstes_rennen": re.compile(r"\bclosest\b|\bmargin\b", re.IGNORECASE),
    "meiste_sitze": re.compile(r"\bmost seats\b|\bseat(?:s)? share\b|\bmajority of seats\b", re.IGNORECASE),
    "erster": re.compile(r"\bfirst to\b|\bannounce .* first\b|\bfirst\b.*\bannounce\b", re.IGNORECASE),
}
_SPORT_TYPES_OK = {"moneyline"}  # nur echte Zwei-Wege-Siegerwetten (kein Remis moeglich) mit Teamnamen


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rule_conflicts(ma, mb):
    """Liste von Gruenden, warum die Aufloesungsregeln beider Maerkte nicht als gleich gelten. Leer = vergleichbar."""
    reasons = []
    qa, qb = ma.get("question") or "", mb.get("question") or ""
    for m, q in ((ma, qa), (mb, qb)):
        if _COPY_RE.search(q) or str(m.get("slug", "")).endswith("-copy"):
            reasons.append(f"Kopie-/Testmarkt ({m['platform']})")
    for name, rx in _DEFINITION_RE.items():
        if rx.search(qa) or rx.search(qb):
            reasons.append(f"Aufloesung haengt an plattformeigener Definition ({name})")

    sport_a, sport_b = bool(ma.get("is_sport")), bool(mb.get("is_sport"))
    if sport_a or sport_b:
        if not (sport_a and sport_b):
            reasons.append("nur eine Seite ist ein Sportmarkt")
        for m in (ma, mb):
            if m.get("sport_market_type") not in _SPORT_TYPES_OK:
                reasons.append(f"Sport-Markttyp {m.get('sport_market_type') or 'unbekannt'} ({m['platform']}) ist "
                               f"keine Zwei-Wege-Siegerwette")
            if CANCEL_PAYOUT.get(m["platform"]) is None:
                reasons.append(f"Abbruch-Regel von {m['platform']} unbekannt (fair price / ungeregelt)")
        ta, tb = _parse_time(ma.get("start_time")), _parse_time(mb.get("start_time"))
        if ta is None or tb is None:
            reasons.append("Anstosszeit auf einer Seite unbekannt")
        elif abs((ta - tb).total_seconds()) > MAX_START_GAP_HOURS * 3600:
            reasons.append(f"Anstosszeiten {abs((ta - tb).total_seconds()) / 3600:.1f} h auseinander (anderes Spiel)")
    return reasons


# ---------------------------------------------------------------- Laufzeit

def days_to_resolution(ma, mb, now):
    """Tage bis zur SPAETEREN Aufloesung beider Beine (das Kapital ist bis dahin gebunden), oder None."""
    days = []
    for m in (ma, mb):
        dt = _parse_time(m.get("end_date"))
        if dt is not None:
            days.append((dt - now).total_seconds() / 86400.0)
    return max(days) if days else None


# ---------------------------------------------------------------- Hauptpruefung

def check_pair(ma, mb, now, alignment=None):
    """Prueft ein gematchtes Paar gegen die Strategie (Moduldoku). Gibt Check zurueck; ok=False heisst kein Kauf."""
    reasons = []
    if alignment is None:
        alignment = cross_platform.outcome_alignment(ma, mb)
    if alignment not in ("same", "flipped"):
        return Check(False, ["Seitenzuordnung unklar (welche Seite welchem Ausgang entspricht)"], None,
                     None, None, None, None, None, None, None, None, None)
    side_a, side_b, summe = cross_platform.best_arbitrage_combo(ma, mb, alignment)
    price_a = ma["yes_price"] if side_a == "yes" else ma["no_price"]
    price_b = mb["yes_price"] if side_b == "yes" else mb["no_price"]

    reasons.extend(rule_conflicts(ma, mb))
    if pricing.combine_basis(ma.get("price_basis", pricing.BASIS_ESTIMATE),
                             mb.get("price_basis", pricing.BASIS_ESTIMATE)) != pricing.BASIS_EXECUTABLE:
        reasons.append("mindestens ein Preis ist nicht ausfuehrbar (Schaetzkurs)")
    valid = all(isinstance(p, (int, float)) and math.isfinite(p) and 0.0 < p < 1.0 for p in (price_a, price_b))
    spread = edge = days = annual = None
    if not valid:
        reasons.append("Preis 0/1 oder ungueltig (kein Angebot)")
    else:
        spread = 1.0 - summe
        if spread > MAX_PLAUSIBLE_SPREAD:
            reasons.append(f"Kursunterschied {spread:.1%} ueber {MAX_PLAUSIBLE_SPREAD:.0%}: fast sicher Fehlpaarung")
        with_cancel = bool(ma.get("is_sport") or mb.get("is_sport"))
        edge = worst_net_per_share(ma, mb, price_a, price_b, with_cancel)
        if edge < MIN_EDGE:
            reasons.append(f"garantierter Gewinn {edge:+.3f} USD je Paar nach Gebuehren unter {MIN_EDGE:.2f}")
        days = days_to_resolution(ma, mb, now)
        if days is None:
            reasons.append("Laufzeit unbekannt")
        elif days <= 0:
            reasons.append("Enddatum bereits erreicht")
        else:
            cost = price_a + price_b + entry_fee_per_share(ma, price_a) + entry_fee_per_share(mb, price_b)
            annual = (edge / cost) * 365.0 / max(days, 1.0)
            if annual < MIN_ANNUAL_RETURN:
                reasons.append(f"Rendite {annual:.1%} p.a. bis zur Aufloesung unter {MIN_ANNUAL_RETURN:.0%}")
    return Check(not reasons, reasons, alignment, side_a, side_b, price_a, price_b, summe, spread, edge, days, annual)


# ---------------------------------------------------------------- Groesse nach Orderbuch-Tiefe

UNKNOWN_DEPTH_LEG_USD = 10.0   # Bein ohne abrufbare Tiefe (PredictIt; Buch nicht erreichbar): hoechstens 10 USD
MAX_PAIR_FRACTION = 0.10       # hoechstens 10 % des Fondswerts in einem Paar (Klumpenrisiko, Lamar: 57 %)

Fill = namedtuple("Fill", "shares cost_a cost_b fee_a fee_b total avg_a avg_b worst_value")


def _unknown_depth(price):
    return [(price, UNKNOWN_DEPTH_LEG_USD / price)]


def size_pair(ma, mb, chk, levels_a, levels_b, budget):
    """Groesste Stueckzahl, die beide Orderbuecher hergeben und bei der JEDE weitere Tranche noch den
    Mindestgewinn im schlechtesten Ausgang bringt; hoechstens `budget` USD Gesamtkosten.
    levels_*: [(preis, anteile)] aufsteigend ab dem besten Ask, None = Tiefe unbekannt (dann UNKNOWN_DEPTH_LEG_USD).
    Gibt Fill oder None zurueck. Teilfuellungen (ein Bein voll, das andere nicht) bildet die Simulation nicht ab;
    die Stueckzahl ist deshalb auf das duennere Buch begrenzt."""
    with_cancel = bool(ma.get("is_sport") or mb.get("is_sport"))
    def clean(levels, price):
        lv = sorted((p, s) for p, s in (levels or []) if 0.0 < p < 1.0 and s > 0 and math.isfinite(p * s))
        return lv or _unknown_depth(price)

    la, lb = clean(levels_a, chk.price_a), clean(levels_b, chk.price_b)
    if budget <= 0:
        return None
    i = j = 0
    ra, rb = la[0][1], lb[0][1]
    fills_a, fills_b, spent, n = [], [], 0.0, 0.0
    while i < len(la) and j < len(lb):
        pa, pb = la[i][0], lb[j][0]
        if worst_net_per_share(ma, mb, pa, pb, with_cancel) < MIN_EDGE:
            break
        unit = pa + pb + entry_fee_per_share(ma, pa) + entry_fee_per_share(mb, pb)
        q = min(ra, rb, (budget - spent) / unit)
        if q <= 1e-9:
            break
        fills_a.append((pa, q))
        fills_b.append((pb, q))
        spent += q * unit
        n += q
        ra -= q
        rb -= q
        if ra <= 1e-9:
            i += 1
            ra = la[i][1] if i < len(la) else 0.0
        if rb <= 1e-9:
            j += 1
            rb = lb[j][1] if j < len(lb) else 0.0
        if budget - spent <= 1e-6:
            break
    if n <= 0:
        return None
    return _fill_from(ma, mb, fills_a, fills_b, with_cancel)


def _fill_from(ma, mb, fills_a, fills_b, with_cancel):
    n = sum(q for _, q in fills_a)
    cost_a = sum(p * q for p, q in fills_a)
    cost_b = sum(p * q for p, q in fills_b)
    fee_a = sum(entry_fee(ma, q, p) for p, q in fills_a)
    fee_b = sum(entry_fee(mb, q, p) for p, q in fills_b)
    avg_a, avg_b = cost_a / n, cost_b / n
    worlds = world_values(ma["platform"], mb["platform"], avg_a, avg_b, with_cancel)
    total = cost_a + cost_b + fee_a + fee_b
    return Fill(n, cost_a, cost_b, fee_a, fee_b, total, avg_a, avg_b, min(worlds.values()) * n)


def scale_fill(ma, mb, fill, factor):
    """Dieselbe Mischung, alle Tranchen mit `factor` skaliert (fuer Kassen-Engpaesse)."""
    with_cancel = bool(ma.get("is_sport") or mb.get("is_sport"))
    n = fill.shares * factor
    return _fill_from(ma, mb, [(fill.avg_a, n)], [(fill.avg_b, n)], with_cancel)


def fill_is_safe(fill):
    """Endkontrolle mit den echten (gerundeten) Gebuehren: garantierter Wert >= Kosten + MIN_EDGE je Paar."""
    return fill is not None and fill.shares > 0 and fill.worst_value - fill.total >= MIN_EDGE * fill.shares - 1e-9
