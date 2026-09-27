"""
Dashboard-Generator
====================

Baut aus den Daten in data/ (summary.csv, fund_state.json, fund_history.csv,
fund_trades.csv, last_run_stats.json) die HTML-Seite für das Claude-Artefakt
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


def build_navchart(history, width=1000, height=180):
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
    if vmin <= STARTING_CAPITAL <= vmax:
        by = Y(STARTING_CAPITAL)
        baseline = (f'<line x1="{pad_x}" y1="{by:.1f}" x2="{width-pad_x}" y2="{by:.1f}" '
                    f'stroke="var(--muted-2)" stroke-width="1" stroke-dasharray="3,4"/>'
                    f'<text x="{pad_x}" y="{by-6:.1f}" font-size="11" fill="var(--muted-2)" '
                    f'font-family="IBM Plex Mono, monospace">Start 100.00</text>')

    end_color = "var(--accent)" if values[-1] >= STARTING_CAPITAL else "var(--danger)"
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

.card.fund .row{ display:flex; justify-content:space-between; font-size:12px; color:var(--muted); font-family:"IBM Plex Mono",monospace; }
.card.fund .pnl.pos{ color:var(--accent); }
.card.fund .pnl.neg{ color:var(--danger); }

.pill.buy{ background:var(--surface-2); color:var(--muted); border:1px solid var(--border); }
.pill.sell{ background:var(--accent-dim); color:var(--accent); }
.pill.payout{ background:#1d3a6b; color:#7ab3ff; }

.freqnote{ font-size:12px; color:var(--muted-2); margin-top:10px; }
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
    fund_state = load_json(os.path.join(data_dir, "fund_state.json"), {"cash": STARTING_CAPITAL, "positions": [], "started": None})
    fund_history = load_csv(os.path.join(data_dir, "fund_history.csv"))
    fund_trades = load_csv(os.path.join(data_dir, "fund_trades.csv"))
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

    fund_cash = fund_state["cash"]
    fund_positions = fund_state["positions"]
    fund_positions_value = sum(p["shares"] * p.get("letzter_kurs", p["einstandskurs"]) for p in fund_positions)
    fund_nav = fund_cash + fund_positions_value
    fund_return_pct = (fund_nav / STARTING_CAPITAL - 1) * 100

    navchart_html = build_navchart(fund_history)
    if navchart_html is None:
        navchart_html = '<div class="empty">Noch zu wenig Verlauf für einen Chart &mdash; der Fonds sammelt gerade seine erste Kursreihe.</div>'

    now_utc = datetime.now(timezone.utc)
    position_cards_html = ""
    for p in sorted(fund_positions, key=lambda p: -(p["shares"] * (p.get("letzter_kurs", p["einstandskurs"]) - p["einstandskurs"]))):
        letzter = p.get("letzter_kurs", p["einstandskurs"])
        pnl_abs = p["shares"] * (letzter - p["einstandskurs"])
        pnl_pct = (letzter / p["einstandskurs"] - 1) * 100
        pnl_cls = "pos" if pnl_abs >= 0 else "neg"
        dte = days_to_end(p.get("end_date"), now_utc)
        laufzeit = f"{dte:.1f} Tage" if dte is not None and dte >= 0 else "steht kurz bevor / überfällig"
        position_cards_html += f"""
        <div class="card fund">
          <div class="q">{esc(p['frage'])}</div>
          <div class="row"><span>Anteile</span><span>{p['shares']:.2f}</span></div>
          <div class="row"><span>Einstand</span><span>{p['einstandskurs']:.3f}</span></div>
          <div class="row"><span>Letzter Kurs</span><span>{letzter:.3f}</span></div>
          <div class="row pnl {pnl_cls}"><span>Unrealisiert</span><span>{pnl_abs:+.2f} USD ({pnl_pct:+.1f}%)</span></div>
          <div class="row"><span>Liquidität</span><span>{money(p['liquiditaet'])}</span></div>
          <div class="row"><span>Restlaufzeit</span><span>{laufzeit}</span></div>
        </div>"""

    if not fund_positions:
        position_cards_html = ('<div class="empty">Der Fonds hält aktuell keine Position &mdash; er wartet auf einen Treffer, der '
                               'Mindest-Spread (1&thinsp;%) und Mindest-Liquidität (15&thinsp;USD) erfüllt <b>und</b> bei dem nach dem '
                               'Market-Impact-Modell noch ein Edge übrig bleibt.</div>')

    action_pill = {"KAUF": "buy", "VERKAUF": "sell", "AUSZAHLUNG": "payout"}
    trade_rows_html = ""
    for t in list(reversed(fund_trades))[:20]:
        cls = action_pill.get(t["aktion"], "buy")
        trade_rows_html += f"""<tr>
          <td class="mono">{t['timestamp'][:16].replace('T',' ')}</td>
          <td><span class="pill {cls}">{t['aktion']}</span></td>
          <td>{esc(t['frage'])}</td>
          <td class="num">{float(t['betrag']):+.2f}</td>
          <td class="num">{float(t['kasse_danach']):.2f}</td>
        </tr>"""
    if not fund_trades:
        trade_rows_html = '<tr><td colspan="5" style="color:var(--muted); padding:14px;">Noch keine Trades protokolliert.</td></tr>'

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
      <h2>Arbitrage-Fonds (Simulation)</h2>
      <p>Papier-Trading auf Basis der Scan-Treffer &middot; Start 100.00 USD</p>
    </div>
    <div class="fundhero">
      <div class="navblock">
        <span class="navlabel">NAV</span>
        <span class="navvalue">{fund_nav:.2f}&nbsp;USD</span>
      </div>
      <span class="retpill {ret_class(fund_return_pct)}">{fund_return_pct:+.2f}%</span>
      <div class="fundstats">
        <div>Kasse<b>{fund_cash:.2f} USD</b></div>
        <div>Offene Positionen<b>{len(fund_positions)}</b></div>
        <div>In Positionen gebunden<b>{fund_positions_value:.2f} USD</b></div>
      </div>
      {navchart_html}
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
    <p class="freqnote">Neue Positionen wählt der große Scan (alle ~15&thinsp;Min., voller Marktüberblick). Offene Positionen werden zusätzlich alle ~5&thinsp;Min. einzeln nachverfolgt (fund_watch.py), damit Konvergenz/Auflösung schneller auffällt als beim nächsten Vollscan.</p>
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
    Fonds-Logik: reines Papier-Trading, es wird nichts echt gehandelt. Gekauft wird nur bei Mindest-Spread und Mindest-Liquidität und nur wenn nach einem einfachen Marktimpact-Modell noch ein Edge übrig bleibt. Auszahlung bei Marktauflösung wird als sauberes 1.00&nbsp;USD/Paar angenommen.<br>
    Rohdaten &amp; Verlauf: <a href="https://github.com/rouvenwieland/polymarket-scanner" target="_blank" rel="noopener">github.com/rouvenwieland/polymarket-scanner</a> &middot; <code>data/summary.csv</code>, <code>data/fund_state.json</code>, <code>data/fund_trades.csv</code>, <code>data/fund_history.csv</code><br>
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
