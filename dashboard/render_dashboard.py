"""
Dashboard-Generator
====================

Baut aus den Daten in data/ (summary.csv, last_run_stats.json und je Fonds-
Profil fund_state*.json/fund_history*.csv/fund_trades*.csv, siehe
FUND_PROFILES unten) die HTML-Seite für das Claude-Artefakt
"Polymarket Arbitrage Monitor". Lokal ausführbar:

    python dashboard/render_dashboard.py [--data-dir data] [--out dashboard/output.html]

Das erzeugte output.html wird NICHT committed (siehe .gitignore) - es ist
reiner Build-Output, der bei jeder Aktualisierung neu erzeugt und dann per
Claude-Artifact-Tool veröffentlicht wird.
"""

import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone

STARTING_CAPITAL = 100.0
ANNUAL_DISCOUNT_RATE = 0.02  # muss zu scripts/fund_simulator.py ANNUAL_DISCOUNT_RATE passen


def discount_factor(days, rate=ANNUAL_DISCOUNT_RATE):
    if days is None or days <= 0:
        return 1.0
    return 1.0 / ((1.0 + rate) ** (days / 365.25))

# Muss zu scripts/fund_simulator.py PROFILES passen (Dateinamen + Reihenfolge).
FUND_PROFILES = [
    {
        "key": "conservative", "label": "Konservativ", "starting_capital": 100.0,
        "state_json": "fund_state.json", "history_csv": "fund_history.csv", "trades_csv": "fund_trades.csv",
        "desc": "Mindest-Liquidität 15 USD, Mindest-Spread 1%, Kaufimpact simuliert, verkauft bei Konvergenz.",
        "empty_note": "Mindest-Spread (1&thinsp;%) und Mindest-Liquidität (15&thinsp;USD) erfüllt <b>und</b> bei dem nach dem Market-Impact-Modell noch ein Edge übrig bleibt.",
        "open_by_default": True,
    },
    {
        "key": "aggressive", "label": "Aggressiv", "starting_capital": 100.0,
        "state_json": "fund_state_aggressive.json", "history_csv": "fund_history_aggressive.csv", "trades_csv": "fund_trades_aggressive.csv",
        "desc": "Keine Mindest-Liquidität (kauft auch in sehr dünnen Märkten), hält aber im Zweifel bis zur Auflösung statt früh zu verkaufen. Kaufimpact bleibt simuliert.",
        "empty_note": "Mindest-Spread (1&thinsp;%) erfüllt <b>und</b> bei dem nach dem Market-Impact-Modell noch ein Edge übrig bleibt (Liquidität allein ist hier keine Hürde).",
        "open_by_default": False,
    },
    {
        "key": "best_case", "label": "Best Case (unrealistisch)", "starting_capital": 100.0,
        "state_json": "fund_state_bestcase.json", "history_csv": "fund_history_bestcase.csv", "trades_csv": "fund_trades_bestcase.csv",
        "desc": "Ignoriert Liquidität UND Marktimpact komplett, handelt exakt zum notierten Kurs, nimmt jeden positiven Spread mit und schichtet aktiv in bessere Gelegenheiten um. Eine bewusst unrealistische Obergrenze.",
        "empty_note": "positiven Spread hat (praktisch jeder Treffer zählt hier).",
        "open_by_default": False,
    },
    {
        "key": "cross_platform", "label": "Cross-Platform (100 USD)", "starting_capital": 100.0,
        "state_json": "fund_state_crossplatform.json", "history_csv": "fund_history_crossplatform.csv", "trades_csv": "fund_trades_crossplatform.csv",
        "desc": "Kauft Yes auf einer Plattform + No auf einer anderen für dasselbe, per Text-/Beschreibungsähnlichkeit gematchte Ereignis, wenn die Kombi-Summe nach geschätzten Gebühren unter 1 liegt. Positionsgröße ist edge-basiert (kein künstlicher Fraktions-Deckel): so groß wie der simulierte Kaufimpact es noch profitabel zulässt. Kalshi-Taker-Fee und PredictIt-Gewinn-Fee werden explizit verrechnet.",
        "empty_note": "plattformübergreifend als dasselbe Ereignis erkannt wurde (Text-/Beschreibungsähnlichkeit + Enddatum-Nähe) <b>und</b> bei dem die Kombi-Summe nach geschätzten Gebühren noch Edge lässt.",
        "open_by_default": False,
        "two_legged": True,
    },
    {
        "key": "cross_platform_1k", "label": "Cross-Platform (1.000 USD)", "starting_capital": 1000.0,
        "state_json": "fund_state_crossplatform_1k.json", "history_csv": "fund_history_crossplatform_1k.csv", "trades_csv": "fund_trades_crossplatform_1k.csv",
        "desc": "Identische Strategie wie der 100-USD-Cross-Platform-Fonds, nur mit 1.000 USD Startkapital - zum Testen, wie gut sich die Strategie mit mehr Kapital skalieren lässt (Positionsgröße bleibt edge-/liquiditätsbasiert, kein künstlicher Deckel).",
        "empty_note": "plattformübergreifend als dasselbe Ereignis erkannt wurde (Text-/Beschreibungsähnlichkeit + Enddatum-Nähe) <b>und</b> bei dem die Kombi-Summe nach geschätzten Gebühren noch Edge lässt.",
        "open_by_default": False,
        "two_legged": True,
    },
    {
        "key": "cross_platform_10k", "label": "Cross-Platform (10.000 USD)", "starting_capital": 10000.0,
        "state_json": "fund_state_crossplatform_10k.json", "history_csv": "fund_history_crossplatform_10k.csv", "trades_csv": "fund_trades_crossplatform_10k.csv",
        "desc": "Identische Strategie wie der 100-USD-Cross-Platform-Fonds, nur mit 10.000 USD Startkapital - zum Testen, wie gut sich die Strategie mit deutlich mehr Kapital skalieren lässt (Positionsgröße bleibt edge-/liquiditätsbasiert; bei wenig verfügbaren tiefen Gelegenheiten bleibt dadurch viel Kasse ungenutzt).",
        "empty_note": "plattformübergreifend als dasselbe Ereignis erkannt wurde (Text-/Beschreibungsähnlichkeit + Enddatum-Nähe) <b>und</b> bei dem die Kombi-Summe nach geschätzten Gebühren noch Edge lässt.",
        "open_by_default": False,
        "two_legged": True,
    },
    {
        "key": "kalshi_only", "label": "Kalshi Solo (100 USD)", "starting_capital": 100.0,
        "state_json": "fund_state_kalshi.json", "history_csv": "fund_history_kalshi.csv", "trades_csv": "fund_trades_kalshi.csv",
        "desc": "Dieselbe Yes+No&lt;1-Strategie wie der Polymarket-\"conservative\"-Fonds, aber auf Kalshis EIGENEN Märkten (keine Cross-Platform-Matches nötig) - zeigt, ob/wie gut die Idee innerhalb dieser einen Plattform funktioniert. Edge-basierte Positionsgröße, Kalshi-Taker-Fee auf beide Seiten beim Kauf verrechnet.",
        "empty_note": "Mindest-Spread (1,5&thinsp;Cent) nach geschätzter Gebühr erfüllt <b>und</b> bei dem nach dem Market-Impact-Modell noch ein Edge übrig bleibt.",
        "open_by_default": False,
    },
    {
        "key": "predictit_only", "label": "PredictIt Solo (100 USD)", "starting_capital": 100.0,
        "state_json": "fund_state_predictit.json", "history_csv": "fund_history_predictit.csv", "trades_csv": "fund_trades_predictit.csv",
        "desc": "Dieselbe Yes+No&lt;1-Strategie wie der Polymarket-\"conservative\"-Fonds, aber auf PredictIts EIGENEN Märkten. Da PredictIts öffentliche API keine Liquiditätsangabe liefert, gilt pro Position ein fester, konservativer Not-Deckel statt eines Impact-Modells. PredictIts 10%-Gewinn-Fee wird bei Verkauf/Auflösung verrechnet.",
        "empty_note": "Mindest-Spread (1,5&thinsp;Cent) nach geschätzter Gebühr erfüllt.",
        "open_by_default": False,
    },
    {
        "key": "sxbet_only", "label": "SX Bet Solo (100 USD)", "starting_capital": 100.0,
        "state_json": "fund_state_sxbet.json", "history_csv": "fund_history_sxbet.csv", "trades_csv": "fund_trades_sxbet.csv",
        "desc": "Dieselbe Yes+No&lt;1-Strategie, aber auf SX Bets EIGENEN Märkten (dezentrale On-Chain-Sportwetten-Börse, Orderbuch-Modell). Preise werden aus dem Orderbuch abgeleitet (bester Taker-Preis je Seite), Liquidität aus der Orderbuch-Tiefe. Keine Trading-Gebühr auf Einzelwetten (siehe platforms/sxbet.py).",
        "empty_note": "Mindest-Spread (1,5&thinsp;Cent) erfüllt <b>und</b> bei dem nach dem Market-Impact-Modell noch ein Edge übrig bleibt.",
        "open_by_default": False,
    },
]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(x):
    if x >= 1000:
        return f"${x:,.0f}"
    return f"${x:,.2f}"


def spread_pct(x):
    return f"{x*100:.1f}%"


def parse_end_min(s):
    if not s or s == "-":
        return None
    neg = s.startswith("-")
    s2 = s.lstrip("-")
    d = re.search(r"(\d+)d", s2)
    h = re.search(r"(\d+)h", s2)
    m = re.search(r"(\d+)min", s2)
    total = 0
    if d:
        total += int(d.group(1)) * 1440
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return -total if neg else total


def days_to_end(end_date_str, now):
    if not end_date_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end_dt - now).total_seconds() / 86400.0


def ret_class(x):
    if x > 0.005:
        return "pos"
    if x < -0.005:
        return "neg"
    return "flat"


def load_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_navchart(history, width=1000, height=180, starting_capital=STARTING_CAPITAL):
    if len(history) < 2:
        return None
    values = [float(r["nav"]) for r in history]
    vmin, vmax = min(values), max(values)
    if vmax - vmin < 0.5:
        vmin -= 1
        vmax += 1
    pad = (vmax - vmin) * 0.12
    vmin -= pad
    vmax += pad
    n = len(values)
    pad_x = 4

    def X(i):
        return pad_x + i / (n - 1) * (width - 2 * pad_x)

    def Y(v):
        return height - 24 - (v - vmin) / (vmax - vmin) * (height - 40)

    line_pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(values))
    area_pts = f"{X(0):.1f},{height-16} " + line_pts + f" {X(n-1):.1f},{height-16}"

    baseline = ""
    if vmin <= starting_capital <= vmax:
        by = Y(starting_capital)
        baseline = (f'<line x1="{pad_x}" y1="{by:.1f}" x2="{width-pad_x}" y2="{by:.1f}" '
                    f'stroke="var(--muted-2)" stroke-width="1" stroke-dasharray="3,4"/>'
                    f'<text x="{pad_x}" y="{by-6:.1f}" font-size="11" fill="var(--muted-2)" '
                    f'font-family="IBM Plex Mono, monospace">Start {starting_capital:,.2f}</text>')

    end_color = "var(--accent)" if values[-1] >= starting_capital else "var(--danger)"
    endx, endy = X(n - 1), Y(values[-1])

    return f'''<svg class="navchart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="NAV-Verlauf">
      <defs>
        <linearGradient id="navfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{end_color}" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="{end_color}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      {baseline}
      <polygon points="{area_pts}" fill="url(#navfill)"/>
      <polyline points="{line_pts}" fill="none" stroke="{end_color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="{endx:.1f}" cy="{endy:.1f}" r="3.5" fill="{end_color}"/>
      <text x="{max(endx-70,pad_x):.1f}" y="{max(endy-10,14):.1f}" font-size="12" font-weight="700" fill="{end_color}" font-family="IBM Plex Mono, monospace">{values[-1]:.2f}</text>
    </svg>'''


def build_value_chart(history, width=1000, height=200, starting_capital=STARTING_CAPITAL):
    """Terminierungswert (was man bei sofortiger regulärer Auszahlung aller
    offenen Positionen bekäme) und derselbe Wert abgezinst für die
    Kapitalbindung bis zur Fälligkeit. Nutzt nur Zeilen, in denen beide
    Felder vorhanden sind (ältere history.csv-Zeilen vor Einführung dieser
    Kennzahl bleiben für diesen Chart leer, tauchen aber weiter im
    NAV-Chart auf)."""
    rows = [r for r in history if r.get("terminierungswert") and r.get("diskontierter_terminierungswert")]
    if len(rows) < 2:
        return None

    term_vals = [float(r["terminierungswert"]) for r in rows]
    disc_vals = [float(r["diskontierter_terminierungswert"]) for r in rows]
    all_vals = term_vals + disc_vals + [starting_capital]
    vmin, vmax = min(all_vals), max(all_vals)
    if vmax - vmin < 0.5:
        vmin -= 1
        vmax += 1
    pad = (vmax - vmin) * 0.12
    vmin -= pad
    vmax += pad
    n = len(rows)
    pad_x = 4

    def X(i):
        return pad_x + i / (n - 1) * (width - 2 * pad_x)

    def Y(v):
        return height - 28 - (v - vmin) / (vmax - vmin) * (height - 44)

    def polyline(values, color, dash=""):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(values))
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"{dash_attr} stroke-linejoin="round" stroke-linecap="round"/>'

    baseline = ""
    if vmin <= starting_capital <= vmax:
        by = Y(starting_capital)
        baseline = (f'<line x1="{pad_x}" y1="{by:.1f}" x2="{width-pad_x}" y2="{by:.1f}" '
                    f'stroke="var(--muted-2)" stroke-width="1" stroke-dasharray="3,4"/>')

    term_line = polyline(term_vals, "var(--accent)")
    disc_line = polyline(disc_vals, "var(--warn)", dash="6,4")
    endx = X(n - 1)

    return f'''<svg class="navchart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="Terminierungswert-Verlauf">
      {baseline}
      {term_line}
      {disc_line}
      <circle cx="{endx:.1f}" cy="{Y(term_vals[-1]):.1f}" r="3.5" fill="var(--accent)"/>
      <circle cx="{endx:.1f}" cy="{Y(disc_vals[-1]):.1f}" r="3.5" fill="var(--warn)"/>
      <text x="{max(endx-90,pad_x):.1f}" y="{max(Y(term_vals[-1])-10,14):.1f}" font-size="12" font-weight="700" fill="var(--accent)" font-family="IBM Plex Mono, monospace">{term_vals[-1]:.2f}</text>
      <text x="{max(endx-90,pad_x):.1f}" y="{min(Y(disc_vals[-1])+20,height-6):.1f}" font-size="12" font-weight="700" fill="var(--warn)" font-family="IBM Plex Mono, monospace">{disc_vals[-1]:.2f}</text>
    </svg>'''


HEAD = """<title>Polymarket Arbitrage Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0e13;
  --surface:#111823;
  --surface-2:#161f2c;
  --border:#232f3f;
  --text:#e7edf3;
  --muted:#8592a3;
  --muted-2:#5b6878;
  --accent:#35d0a0;
  --accent-dim:#1d6b57;
  --warn:#f0b350;
  --danger:#f2646b;
  --danger-dim:#5c2a2e;
  color-scheme: light;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){ color-scheme: dark; }
}
:root[data-theme="dark"]{ color-scheme: dark; }

*{box-sizing:border-box;}
body{
  margin:0;
  background:var(--bg);
  color:var(--text);
  font-family:"IBM Plex Sans", system-ui, sans-serif;
  padding-inline:16px;
}
.mono{ font-family:"IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; }

.wrap{ max-width:1080px; margin:0 auto; padding-block:28px 64px; }

.topbar{
  position:sticky; top:env(safe-area-inset-top,0px);
  z-index:5;
  display:flex; align-items:center; justify-content:space-between; gap:16px;
  flex-wrap:wrap;
  padding-block:14px;
  margin-bottom:28px;
  border-bottom:1px solid var(--border);
  background:var(--bg);
}
.brand{ display:flex; align-items:center; gap:10px; }
.brand-dot{ width:10px; height:10px; border-radius:50%; background:var(--accent); box-shadow:0 0 0 4px var(--accent-dim); flex:none; }
.brand h1{ font-size:17px; margin:0; font-weight:700; letter-spacing:-0.01em; }
.brand span{ display:block; font-size:12px; color:var(--muted); font-weight:500; }

.status{
  display:flex; align-items:center; gap:8px;
  font-size:12px; color:var(--muted);
  padding:6px 10px; border:1px solid var(--border); border-radius:999px;
  background:var(--surface);
}
.status b{ color:var(--text); font-weight:600; }

.kpis{
  display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:32px;
}
@media (max-width:720px){ .kpis{ grid-template-columns:repeat(2,1fr); } }
.kpi{
  border:1px solid var(--border); background:var(--surface); border-radius:12px;
  padding:16px;
}
.kpi .num{ font-size:26px; font-weight:700; font-family:"IBM Plex Mono",monospace; letter-spacing:-0.02em; }
.kpi .label{ font-size:12px; color:var(--muted); margin-top:4px; }
.kpi.accent{ border-color:var(--accent-dim); }
.kpi.accent .num{ color:var(--accent); }

section{ margin-bottom:36px; }
.section-head{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
.section-head h2{ font-size:15px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0; font-weight:700; }
.section-head p{ margin:0; font-size:13px; color:var(--muted-2); }

.empty{
  border:1px dashed var(--border); border-radius:12px; padding:22px;
  color:var(--muted); font-size:14px; background:var(--surface); line-height:1.6;
}

.cards{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
.card{
  border:1px solid var(--border); background:var(--surface); border-radius:12px;
  padding:16px; border-left:3px solid var(--muted-2);
  display:flex; flex-direction:column; gap:10px;
}
.card.open{ border-left-color:var(--accent); }
.card.closed{ border-left-color:var(--danger); opacity:.72; }
.card .q{ font-size:14px; font-weight:600; line-height:1.35; text-wrap:balance; }
.pillrow{ display:flex; gap:6px; flex-wrap:wrap; }
.pill{ font-size:11px; padding:3px 8px; border-radius:999px; font-weight:600; font-family:"IBM Plex Mono",monospace; }
.pill.spread{ background:var(--accent-dim); color:var(--accent); }
.pill.vol{ background:var(--surface-2); color:var(--muted); border:1px solid var(--border); }
.pill.state-open{ background:var(--accent-dim); color:var(--accent); }
.pill.state-closed{ background:var(--danger-dim); color:var(--danger); }
.card .meta{ font-size:12px; color:var(--muted); display:flex; justify-content:space-between; font-family:"IBM Plex Mono",monospace; }

.toggle{
  cursor:pointer; user-select:none; display:flex; align-items:center; gap:8px;
  font-size:13px; color:var(--muted); background:none; border:none; padding:0;
  font-family:inherit;
}
.toggle:hover{ color:var(--text); }
.chev{ transition:transform .15s ease; }
.chev.open{ transform:rotate(90deg); }

table{ width:100%; border-collapse:collapse; font-size:13px; }
th,td{ padding:9px 10px; text-align:left; border-bottom:1px solid var(--border); }
th{ color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; cursor:pointer; white-space:nowrap; }
th:hover{ color:var(--text); }
td.num, th.num{ text-align:right; font-family:"IBM Plex Mono",monospace; }
tbody tr:hover{ background:var(--surface-2); }
.tablewrap{ overflow-x:auto; border:1px solid var(--border); border-radius:12px; background:var(--surface); }
.tablewrap table{ min-width:640px; }

footer{ border-top:1px solid var(--border); padding-top:20px; font-size:12px; color:var(--muted-2); line-height:1.7; }
footer a{ color:var(--muted); }
footer code{ font-family:"IBM Plex Mono",monospace; background:var(--surface-2); padding:1px 5px; border-radius:4px; }

.fundhero{
  display:flex; gap:24px; flex-wrap:wrap; align-items:flex-end;
  border:1px solid var(--border); background:var(--surface); border-radius:14px;
  padding:20px; margin-bottom:16px;
}
.fundhero .navblock{ display:flex; flex-direction:column; gap:4px; }
.fundhero .navlabel{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
.fundhero .navvalue{ font-size:34px; font-weight:800; font-family:"IBM Plex Mono",monospace; letter-spacing:-0.02em; }
.retpill{ font-size:13px; font-weight:700; padding:4px 10px; border-radius:999px; font-family:"IBM Plex Mono",monospace; }
.retpill.pos{ background:var(--accent-dim); color:var(--accent); }
.retpill.neg{ background:var(--danger-dim); color:var(--danger); }
.retpill.flat{ background:var(--surface-2); color:var(--muted); }
.fundstats{ display:flex; gap:22px; flex-wrap:wrap; margin-left:auto; }
.fundstats div{ font-size:12px; color:var(--muted); }
.fundstats b{ display:block; font-size:15px; color:var(--text); font-family:"IBM Plex Mono",monospace; margin-top:2px; }
.navchart{ display:block; width:100%; height:auto; margin-top:14px; }
.charttitle{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; font-weight:700; margin-top:4px; }
.chartlegend{ display:flex; gap:16px; flex-wrap:wrap; font-size:12px; color:var(--muted); margin-top:4px; }
.chartlegend span{ display:flex; align-items:center; gap:6px; }
.chartlegend i{ width:16px; height:0; border-top-width:2px; border-top-style:solid; display:inline-block; }

.card.fund .row{ display:flex; justify-content:space-between; font-size:12px; color:var(--muted); font-family:"IBM Plex Mono",monospace; }
.card.fund .pnl.pos{ color:var(--accent); }
.card.fund .pnl.neg{ color:var(--danger); }

.pill.buy{ background:var(--surface-2); color:var(--muted); border:1px solid var(--border); }
.pill.sell{ background:var(--accent-dim); color:var(--accent); }
.pill.payout{ background:#1d3a6b; color:#7ab3ff; }

.freqnote{ font-size:12px; color:var(--muted-2); margin-top:10px; }

.compare{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
@media (max-width:900px){ .compare{ grid-template-columns:repeat(2,1fr); } }
@media (max-width:480px){ .compare{ grid-template-columns:1fr; } }
.compare-card{ border:1px solid var(--border); background:var(--surface); border-radius:12px; padding:16px; }
.compare-card .label{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; font-weight:700; }
.compare-card .nav{ font-size:22px; font-weight:800; font-family:"IBM Plex Mono",monospace; margin-top:6px; }
.compare-card .sub{ font-size:12px; color:var(--muted); margin-top:6px; display:flex; justify-content:space-between; }

details.funddetail{ border:1px solid var(--border); background:var(--surface); border-radius:12px; margin-bottom:12px; overflow:hidden; }
details.funddetail summary{
  cursor:pointer; list-style:none; padding:14px 16px; display:flex; align-items:center; gap:10px;
  font-weight:700; font-size:14px;
}
details.funddetail summary::-webkit-details-marker{ display:none; }
details.funddetail summary .sumchev{ transition:transform .15s ease; color:var(--muted); flex:none; }
details.funddetail[open] summary .sumchev{ transform:rotate(90deg); }
details.funddetail summary .sumdesc{ font-weight:500; font-size:12px; color:var(--muted-2); }
details.funddetail .funddetail-body{ padding:0 16px 16px; border-top:1px solid var(--border); padding-top:16px; }
</style>
"""

SCRIPT = """
<script>
(function(){
  var btn = document.getElementById('noiseToggle');
  var panel = document.getElementById('noiseTable');
  var chev = btn.querySelector('.chev');
  btn.addEventListener('click', function(){
    var show = panel.style.display === 'none';
    panel.style.display = show ? 'block' : 'none';
    chev.classList.toggle('open', show);
  });

  var table = document.getElementById('dataTable');
  var tbody = table.querySelector('tbody');
  var ths = table.querySelectorAll('th');
  var sortState = { key: null, dir: 1 };
  ths.forEach(function(th){
    th.addEventListener('click', function(){
      var key = th.dataset.key;
      var idx = Array.from(ths).indexOf(th);
      var isNum = th.classList.contains('num');
      var dir = (sortState.key === key) ? -sortState.dir : 1;
      sortState = { key: key, dir: dir };
      var rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort(function(a, b){
        var av = a.children[idx].textContent.trim();
        var bv = b.children[idx].textContent.trim();
        if (isNum) {
          av = parseFloat(av.replace(/[^0-9.\\-]/g,'')) || 0;
          bv = parseFloat(bv.replace(/[^0-9.\\-]/g,'')) || 0;
          return (av - bv) * dir;
        }
        return av.localeCompare(bv) * dir;
      });
      rows.forEach(function(r){ tbody.appendChild(r); });
    });
  });
})();
</script>
"""


def render(data_dir):
    summary_rows = load_csv(os.path.join(data_dir, "summary.csv"))
    last_run = load_json(os.path.join(data_dir, "last_run_stats.json"), {})

    data = []
    for r in summary_rows:
        vol = float(r["Max. Volumen (USD)"])
        liq = float(r["Max. Liquiditaet (USD)"])
        summe = float(r["Min. Summe beobachtet"])
        spread = round(1 - summe, 4)
        end_min = parse_end_min(r["Zeit bis Terminierung"])
        still_open = end_min is not None and end_min > 0
        data.append({
            "frage": r["Frage"], "slug": r["Slug"], "summe": summe, "spread": spread,
            "zeit_bis_ende": r["Zeit bis Terminierung"], "still_open": still_open,
            "volumen": vol, "liquiditaet": liq,
        })

    real = sorted([o for o in data if o["spread"] >= 0.01], key=lambda o: -o["spread"])
    noise = sorted([o for o in data if o["spread"] < 0.01], key=lambda o: -o["volumen"])

    cards_html = ""
    if real:
        for o in real:
            state = "open" if o["still_open"] else "closed"
            state_label = "offen" if o["still_open"] else "beendet"
            cards_html += f"""
            <div class="card {state}">
              <div class="q">{esc(o['frage'])}</div>
              <div class="pillrow">
                <span class="pill spread">Spread {spread_pct(o['spread'])}</span>
                <span class="pill state-{state}">{state_label}</span>
                <span class="pill vol">Vol {money(o['volumen'])}</span>
              </div>
              <div class="meta">
                <span>Summe {o['summe']:.3f}</span>
                <span>{esc(o['zeit_bis_ende'])} bis Ende</span>
              </div>
            </div>"""
    else:
        cards_html = '<div class="empty">Aktuell kein Markt mit Spread &ge; 1&thinsp;%. Die Schwelle Yes+No&nbsp;&lt;&nbsp;1 wird zwar laufend getroffen, aber meist nur um Bruchteile eines Cents &mdash; siehe Tabelle unten.</div>'

    rows_html = ""
    for o in noise:
        rows_html += f"""<tr>
          <td>{esc(o['frage'])}</td>
          <td class="num">{o['summe']:.3f}</td>
          <td class="num">{spread_pct(o['spread'])}</td>
          <td class="num">{money(o['volumen'])}</td>
          <td class="num">{money(o['liquiditaet'])}</td>
          <td>{esc(o['zeit_bis_ende'])}</td>
        </tr>"""

    now_utc = datetime.now(timezone.utc)

    platform_overview = load_json(os.path.join(data_dir, "platform_overview.json"), {})
    cross_hits_rows = load_csv(os.path.join(data_dir, "cross_platform_hits.csv"))
    latest_cross_hits = []
    if cross_hits_rows:
        latest_ts = max(r["timestamp"] for r in cross_hits_rows)
        latest_cross_hits = sorted(
            (r for r in cross_hits_rows if r["timestamp"] == latest_ts),
            key=lambda r: float(r["summe"]),
        )

    fund_results = []
    for fp in FUND_PROFILES:
        starting_capital = fp.get("starting_capital", STARTING_CAPITAL)
        state = load_json(os.path.join(data_dir, fp["state_json"]), {"cash": starting_capital, "positions": [], "started": None})
        history = load_csv(os.path.join(data_dir, fp["history_csv"]))
        trades = load_csv(os.path.join(data_dir, fp["trades_csv"]))
        positions = state["positions"]
        two_legged = fp.get("two_legged", False)
        if two_legged:
            positions_value = sum(p["shares"] * (p["leg_a"]["letzter_kurs"] + p["leg_b"]["letzter_kurs"]) for p in positions)
        else:
            positions_value = sum(p["shares"] * p.get("letzter_kurs", p["einstandskurs"]) for p in positions)
        nav = state["cash"] + positions_value
        termination_value = state["cash"] + sum(p["shares"] for p in positions)
        discounted_value = state["cash"] + sum(
            p["shares"] * discount_factor(days_to_end(p.get("end_date"), now_utc)) for p in positions
        )
        fund_results.append({
            "profile": fp, "state": state, "history": history, "trades": trades,
            "positions": positions, "positions_value": positions_value, "nav": nav,
            "return_pct": (nav / starting_capital - 1) * 100,
            "termination_value": termination_value, "discounted_value": discounted_value,
        })

    compare_html = ""
    for fr in fund_results:
        compare_html += f"""
        <div class="compare-card">
          <div class="label">{esc(fr['profile']['label'])}</div>
          <div class="nav">{fr['nav']:.2f}&nbsp;USD</div>
          <span class="retpill {ret_class(fr['return_pct'])}">{fr['return_pct']:+.2f}%</span>
          <div class="sub"><span>Positionen</span><span>{len(fr['positions'])}</span></div>
          <div class="sub"><span>Kasse</span><span>{fr['state']['cash']:.2f} USD</span></div>
          <div class="sub"><span>Terminierungswert</span><span>{fr['termination_value']:.2f} USD</span></div>
          <div class="sub"><span>&nbsp;&nbsp;davon diskontiert</span><span>{fr['discounted_value']:.2f} USD</span></div>
        </div>"""

    def render_fund_detail(fr):
        fp = fr["profile"]
        starting_capital = fp.get("starting_capital", STARTING_CAPITAL)
        navchart_html = build_navchart(fr["history"], starting_capital=starting_capital)
        if navchart_html is None:
            navchart_html = '<div class="empty">Noch zu wenig Verlauf für einen Chart &mdash; der Fonds sammelt gerade seine erste Kursreihe.</div>'

        valuechart_html = build_value_chart(fr["history"], starting_capital=starting_capital)
        if valuechart_html is None:
            valuechart_html = '<div class="empty">Noch zu wenig Verlauf für diesen Chart &mdash; die Kennzahl wurde gerade erst eingeführt.</div>'

        two_legged = fp.get("two_legged", False)
        position_cards_html = ""
        if two_legged:
            for p in sorted(fr["positions"], key=lambda p: -(p["shares"] * (p["leg_a"]["letzter_kurs"] + p["leg_b"]["letzter_kurs"] - p["leg_a"]["einstandskurs"] - p["leg_b"]["einstandskurs"]))):
                la, lb = p["leg_a"], p["leg_b"]
                einstand = la["einstandskurs"] + lb["einstandskurs"]
                letzter = la["letzter_kurs"] + lb["letzter_kurs"]
                pnl_abs = p["shares"] * (letzter - einstand)
                pnl_pct = (letzter / einstand - 1) * 100 if einstand else 0.0
                pnl_cls = "pos" if pnl_abs >= 0 else "neg"
                dte = days_to_end(p.get("end_date"), now_utc)
                laufzeit = f"{dte:.1f} Tage" if dte is not None and dte >= 0 else "steht kurz bevor / überfällig"
                term_value = p["shares"] * 1.0
                dfac = discount_factor(dte)
                disc_value = term_value * dfac
                abschlag_pct = (1 - dfac) * 100
                position_cards_html += f"""
                <div class="card fund">
                  <div class="q">{esc(p['frage'])}</div>
                  <div class="row"><span>Bein A</span><span>{esc(la['platform'])} &middot; {esc(la['side'])} &middot; {la['letzter_kurs']:.3f}</span></div>
                  <div class="row"><span>Bein B</span><span>{esc(lb['platform'])} &middot; {esc(lb['side'])} &middot; {lb['letzter_kurs']:.3f}</span></div>
                  <div class="row"><span>Anteile</span><span>{p['shares']:.2f}</span></div>
                  <div class="row"><span>Einstand (Summe)</span><span>{einstand:.3f}</span></div>
                  <div class="row pnl {pnl_cls}"><span>Unrealisiert</span><span>{pnl_abs:+.2f} USD ({pnl_pct:+.1f}%)</span></div>
                  <div class="row"><span>Restlaufzeit</span><span>{laufzeit}</span></div>
                  <div class="row" style="border-top:1px dashed var(--border); padding-top:6px; margin-top:2px;"><span>Terminierungswert</span><span>{term_value:.2f} USD</span></div>
                  <div class="row"><span>Diskontiert ({ANNUAL_DISCOUNT_RATE*100:.0f}%&thinsp;p.a.)</span><span>{disc_value:.2f} USD (&minus;{abschlag_pct:.1f}%)</span></div>
                </div>"""
        else:
            for p in sorted(fr["positions"], key=lambda p: -(p["shares"] * (p.get("letzter_kurs", p["einstandskurs"]) - p["einstandskurs"]))):
                letzter = p.get("letzter_kurs", p["einstandskurs"])
                pnl_abs = p["shares"] * (letzter - p["einstandskurs"])
                pnl_pct = (letzter / p["einstandskurs"] - 1) * 100
                pnl_cls = "pos" if pnl_abs >= 0 else "neg"
                dte = days_to_end(p.get("end_date"), now_utc)
                laufzeit = f"{dte:.1f} Tage" if dte is not None and dte >= 0 else "steht kurz bevor / überfällig"
                term_value = p["shares"] * 1.0
                dfac = discount_factor(dte)
                disc_value = term_value * dfac
                abschlag_pct = (1 - dfac) * 100
                position_cards_html += f"""
                <div class="card fund">
                  <div class="q">{esc(p['frage'])}</div>
                  <div class="row"><span>Anteile</span><span>{p['shares']:.2f}</span></div>
                  <div class="row"><span>Einstand</span><span>{p['einstandskurs']:.3f}</span></div>
                  <div class="row"><span>Letzter Kurs</span><span>{letzter:.3f}</span></div>
                  <div class="row pnl {pnl_cls}"><span>Unrealisiert</span><span>{pnl_abs:+.2f} USD ({pnl_pct:+.1f}%)</span></div>
                  <div class="row"><span>Liquidität</span><span>{money(p['liquiditaet'])}</span></div>
                  <div class="row"><span>Restlaufzeit</span><span>{laufzeit}</span></div>
                  <div class="row" style="border-top:1px dashed var(--border); padding-top:6px; margin-top:2px;"><span>Terminierungswert</span><span>{term_value:.2f} USD</span></div>
                  <div class="row"><span>Diskontiert ({ANNUAL_DISCOUNT_RATE*100:.0f}%&thinsp;p.a.)</span><span>{disc_value:.2f} USD (&minus;{abschlag_pct:.1f}%)</span></div>
                </div>"""
        if not fr["positions"]:
            position_cards_html = (f'<div class="empty">Der Fonds hält aktuell keine Position &mdash; er wartet auf einen Treffer, der '
                                   f'{fp["empty_note"]}</div>')

        action_pill = {"KAUF": "buy", "VERKAUF": "sell", "AUSZAHLUNG": "payout", "UMSCHICHTUNG": "sell"}
        trade_rows_html = ""
        for t in list(reversed(fr["trades"]))[:20]:
            cls = action_pill.get(t["aktion"], "buy")
            trade_rows_html += f"""<tr>
              <td class="mono">{t['timestamp'][:16].replace('T',' ')}</td>
              <td><span class="pill {cls}">{t['aktion']}</span></td>
              <td>{esc(t['frage'])}</td>
              <td class="num">{float(t['betrag']):+.2f}</td>
              <td class="num">{float(t['kasse_danach']):.2f}</td>
            </tr>"""
        if not fr["trades"]:
            trade_rows_html = '<tr><td colspan="5" style="color:var(--muted); padding:14px;">Noch keine Trades protokolliert.</td></tr>'

        return f"""
      <details class="funddetail"{' open' if fp['open_by_default'] else ''}>
        <summary>
          <svg class="sumchev" width="10" height="10" viewBox="0 0 10 10"><path d="M2 1l5 4-5 4" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>
          <span>{esc(fp['label'])}</span>
          <span class="retpill {ret_class(fr['return_pct'])}">{fr['return_pct']:+.2f}%</span>
          <span class="sumdesc">{esc(fp['desc'])}</span>
        </summary>
        <div class="funddetail-body">
          <div class="fundhero">
            <div class="navblock">
              <span class="navlabel">NAV</span>
              <span class="navvalue">{fr['nav']:.2f}&nbsp;USD</span>
            </div>
            <span class="retpill {ret_class(fr['return_pct'])}">{fr['return_pct']:+.2f}%</span>
            <div class="fundstats">
              <div>Kasse<b>{fr['state']['cash']:.2f} USD</b></div>
              <div>Offene Positionen<b>{len(fr['positions'])}</b></div>
              <div>In Positionen gebunden<b>{fr['positions_value']:.2f} USD</b></div>
            </div>
            <div class="charttitle">NAV (Marktpreis-bewertet)</div>
            {navchart_html}
          </div>
          <div class="fundhero">
            <div class="navblock">
              <span class="navlabel">Terminierungswert</span>
              <span class="navvalue">{fr['termination_value']:.2f}&nbsp;USD</span>
            </div>
            <div class="fundstats">
              <div>Diskontiert ({ANNUAL_DISCOUNT_RATE*100:.0f}%&thinsp;p.a.)<b>{fr['discounted_value']:.2f} USD</b></div>
              <div>Abschlag durch Kapitalbindung<b>{(1 - fr['discounted_value']/fr['termination_value'])*100 if fr['termination_value'] else 0:.2f}%</b></div>
            </div>
            <div class="chartlegend">
              <span><i style="border-top-color:var(--accent);"></i>Terminierungswert (Auszahlung 1.00&nbsp;USD/Paar, sofort)</span>
              <span><i style="border-top-color:var(--warn); border-top-style:dashed;"></i>Diskontiert (Kapitalbindung bis Fälligkeit)</span>
            </div>
            {valuechart_html}
          </div>
          <div class="cards">{position_cards_html}
          </div>
          <div class="tablewrap" style="margin-top:12px;">
            <table>
              <thead><tr><th>Zeit</th><th>Aktion</th><th>Markt</th><th class="num">Betrag (USD)</th><th class="num">Kasse danach</th></tr></thead>
              <tbody>{trade_rows_html}
              </tbody>
            </table>
          </div>
        </div>
      </details>"""

    fund_details_html = "".join(render_fund_detail(fr) for fr in fund_results)

    platform_labels = {"polymarket": "Polymarket", "kalshi": "Kalshi", "predictit": "PredictIt", "sxbet": "SX Bet"}
    platforms_info = platform_overview.get("platforms", {})
    total_all_platforms = platform_overview.get("total_open_markets")
    total_fmt = f"{total_all_platforms:,}".replace(",", ".") if isinstance(total_all_platforms, int) else "-"
    platform_cards_html = f"""
        <div class="kpi accent">
          <div class="num">{total_fmt}</div>
          <div class="label">Alle Plattformen zusammen &middot; offene Märkte</div>
        </div>"""
    for key, label in platform_labels.items():
        count = platforms_info.get(key, {}).get("open_markets")
        count_fmt = f"{count:,}".replace(",", ".") if isinstance(count, int) else "-"
        platform_cards_html += f"""
        <div class="kpi">
          <div class="num">{count_fmt}</div>
          <div class="label">{label} &middot; offene Märkte</div>
        </div>"""

    cross_hit_rows_html = ""
    for r in latest_cross_hits[:30]:
        cross_hit_rows_html += f"""<tr>
          <td>{esc(r['plattform_a'])}</td>
          <td>{esc(r['frage_a'])}</td>
          <td>{esc(r['plattform_b'])}</td>
          <td>{esc(r['frage_b'])}</td>
          <td class="mono">{esc(r['seite_a'])}/{esc(r['seite_b'])}</td>
          <td class="num">{float(r['summe']):.3f}</td>
          <td class="num">{float(r['score'])*100:.0f}%</td>
        </tr>"""
    if not latest_cross_hits:
        cross_hit_rows_html = ('<tr><td colspan="7" style="color:var(--muted); padding:14px;">'
                                'Aktuell kein plattformübergreifendes Match mit Kombi-Summe unter 1.00 USD gefunden.</td></tr>')

    stand_dt = last_run.get("timestamp")
    if stand_dt:
        try:
            stand_label = datetime.fromisoformat(stand_dt).strftime("%d.%m.%Y, %H:%M")
        except ValueError:
            stand_label = stand_dt
    else:
        stand_label = "unbekannt"

    markets_checked = last_run.get("markets_checked", "-")
    hits_this_run = last_run.get("hits_this_run", "-")
    markets_checked_fmt = f"{markets_checked:,}".replace(",", ".") if isinstance(markets_checked, int) else markets_checked

    body = f"""
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <div class="brand-dot"></div>
      <div>
        <h1>Polymarket Arbitrage Monitor</h1>
        <span>rouvenwieland/polymarket-scanner &middot; Yes&nbsp;+&nbsp;No&nbsp;&lt;&nbsp;1.00&nbsp;USD</span>
      </div>
    </div>
    <div class="status"><span>Stand</span><b class="mono">{stand_label}&nbsp;CEST</b></div>
  </div>

  <div class="kpis">
    <div class="kpi accent">
      <div class="num">{markets_checked_fmt}</div>
      <div class="label">Märkte im letzten Lauf geprüft</div>
    </div>
    <div class="kpi">
      <div class="num">{hits_this_run}</div>
      <div class="label">Treffer im letzten Lauf</div>
    </div>
    <div class="kpi">
      <div class="num">{len(data)}</div>
      <div class="label">Märkte insgesamt je unter Schwelle</div>
    </div>
    <div class="kpi">
      <div class="num">{len(real)}</div>
      <div class="label">davon mit Spread &ge; 1&thinsp;%</div>
    </div>
  </div>

  <section>
    <div class="section-head">
      <h2>Plattform-Übersicht</h2>
      <p>Offene Märkte je Plattform &middot; Stand {stand_label}</p>
    </div>
    <div class="kpis">{platform_cards_html}
    </div>
    <div class="tablewrap" style="margin-top:12px;">
      <table>
        <thead><tr><th>Plattform A</th><th>Frage A</th><th>Plattform B</th><th>Frage B</th><th>Seiten</th><th class="num">Summe</th><th class="num">Match-Score</th></tr></thead>
        <tbody>{cross_hit_rows_html}
        </tbody>
      </table>
    </div>
    <p class="freqnote">Matches werden per Textähnlichkeit (gemeinsame signifikante Wörter) + Enddatum-Nähe gefunden &mdash; eine Heuristik, kein Beweis: ein falsches Match (ähnlicher Wortlaut, andere Auflösungskriterien) ist das Hauptrisiko dieser Strategie. Aktualisiert durch den Multi-Platform-Scan.</p>
  </section>

  <section>
    <div class="section-head">
      <h2>Arbitrage-Fonds (Simulation)</h2>
      <p>Acht parallele Papier-Trading-Strategien auf Basis der Scan-Treffer &middot; drei Polymarket-only (je 100 USD), drei Cross-Platform bei 100/1.000/10.000 USD (Skalierungstest), zwei Single-Platform solo (Kalshi/PredictIt, je 100 USD)</p>
    </div>
    <div class="compare">{compare_html}
    </div>
    {fund_details_html}
    <p class="freqnote">Neue Positionen wählt der große Scan (alle ~15&thinsp;Min., voller Marktüberblick). Offene Positionen werden zusätzlich alle ~5&thinsp;Min. einzeln nachverfolgt (fund_watch.py), damit Konvergenz/Auflösung schneller auffällt als beim nächsten Vollscan. "Best Case" ist bewusst unrealistisch (kein Marktimpact, keine Liquiditätsschranke) und dient als theoretische Obergrenze zum Vergleich.</p>
  </section>

  <section>
    <div class="section-head">
      <h2>Nennenswerte Spreads</h2>
      <p>Yes-Preis + No-Preis mindestens 1&thinsp;Cent unter 1&thinsp;USD beobachtet</p>
    </div>
    <div class="cards">{cards_html}
    </div>
  </section>

  <section>
    <button class="toggle" id="noiseToggle">
      <svg class="chev" width="10" height="10" viewBox="0 0 10 10"><path d="M2 1l5 4-5 4" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>
      <span id="noiseToggleLabel">{len(noise)} weitere Treffer mit Spread &lt; 1&thinsp;% anzeigen (Rundungsrauschen, meist ohne Volumen)</span>
    </button>
    <div id="noiseTable" class="tablewrap" style="display:none; margin-top:12px;">
      <table id="dataTable">
        <thead>
          <tr>
            <th data-key="frage">Frage</th>
            <th class="num" data-key="summe">Summe</th>
            <th class="num" data-key="spread">Spread</th>
            <th class="num" data-key="volumen">Volumen</th>
            <th class="num" data-key="liquiditaet">Liquidität</th>
            <th data-key="zeit_bis_ende">Bis Ende</th>
          </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
      </table>
    </div>
  </section>

  <footer>
    Scan-Logik: alle offenen Yes/No-Märkte der Polymarket-Gamma-API werden per Keyset-Pagination geladen und auf <code>Yes-Preis + No-Preis &lt; 1.00</code> geprüft. Ein Treffer ist <b>keine garantierte risikofreie Arbitrage</b> &mdash; Spread, Slippage und Settlement-Timing sind nicht eingerechnet, und die meisten Treffer oben haben kein Handelsvolumen.<br>
    Fonds-Logik: reines Papier-Trading, es wird nichts echt gehandelt. Drei Profile mit unterschiedlichem Realismus-Grad (siehe Beschreibung je Fonds oben) - von "hält sich an Mindest-Liquidität und Marktimpact" bis "Best Case ohne jede Realismus-Bremse". Auszahlung bei Marktauflösung wird bei allen drei als sauberes 1.00&nbsp;USD/Paar angenommen.<br>
    Terminierungswert &amp; Diskontierung: der Terminierungswert ist, was man bekäme, würden alle offenen Positionen sofort regulär ausgezahlt (1.00&nbsp;USD je gehaltenem Anteilspaar) - unabhängig vom aktuell notierten Marktpreis. Der diskontierte Wert rechnet zusätzlich ein, dass das Geld bis zur echten Fälligkeit gebunden ist: er wird mit angenommenen {ANNUAL_DISCOUNT_RATE*100:.0f}% p.a. (Kapitalbindungskosten/Inflations-Näherung, kein realer Marktzins) über die Restlaufzeit der jeweiligen Position abgezinst.<br>
    Rohdaten &amp; Verlauf: <a href="https://github.com/rouvenwieland/polymarket-scanner" target="_blank" rel="noopener">github.com/rouvenwieland/polymarket-scanner</a> &middot; <code>data/summary.csv</code>, <code>data/fund_state*.json</code>, <code>data/fund_trades*.csv</code>, <code>data/fund_history*.csv</code><br>
    Aktualisierung: automatisch jeden Morgen um 7&thinsp;Uhr, dazu jederzeit auf Zuruf ("aktualisiere").
  </footer>
</div>
"""
    return HEAD + body + SCRIPT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out", default=os.path.join("dashboard", "output.html"))
    args = parser.parse_args()

    html = render(args.data_dir)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard geschrieben nach {args.out} ({len(html)} Zeichen).")


if __name__ == "__main__":
    main()
