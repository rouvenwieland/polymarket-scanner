"""
Alarm: Positionen, die nach ihrem Enddatum offen bleiben
==========================================================

Anlass (17.-27.10.2026): Die Cross-Position "Lamar vs. Southeastern Louisiana" konnte zehn Tage lang nicht
abgerechnet werden (Polymarkt-Ausgaenge hiessen nicht "Yes"/"No"), niemand hat es bemerkt, und ihr eingefrorener
Kurs hat den 10k-Fonds um 5,8 % "abstuerzen" lassen.

Dieses Skript laeuft als letzter Schritt der Watch-Workflows (NACH dem Commit, damit keine Daten verloren gehen)
und beendet sich mit Fehlercode 1, wenn eine Position mehr als ERROR_HOURS (14 Tage) nach ihrem spaetesten Enddatum
noch offen ist. Zwischen GRACE_HOURS (72 h) und ERROR_HOURS gibt es nur eine Warnung (::warning, Exit 0).
Bei Fehler wird der Lauf rot und GitHub verschickt die Fehler-Mail.

Quittieren (z.B. Wahl wird erst Wochen spaeter beglaubigt): in der Position im State-JSON
  "alarm_quittiert_bis": "2026-12-01T00:00:00+00:00"
setzen. Bis dahin zaehlt die Position als bekannt und loest keinen Alarm aus.

Aufruf: python scripts/haengende_positionen.py [--data data] [--grace-hours 72]
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

GRACE_HOURS = 72.0
# Nach der Karenz ist eine offene Position zunaechst nur eine WARNUNG (Aufloesung ausstehend: Auszaehlung,
# Beglaubigung, spaete PredictIt-Abrechnung). Erst nach so vielen Stunden wird daraus ein Fehler (rot, Mail).
ERROR_HOURS = 14 * 24.0


def _parse(value):
    if not value or str(value).strip().upper() in ("NA", "N/A", "NONE"):
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def position_end(pos):
    """Spaetestes bekanntes Enddatum einer Position (Position selbst und beide Beine bei Cross-Paaren)."""
    dates = [_parse(pos.get("end_date"))]
    for leg in ("leg_a", "leg_b"):
        if isinstance(pos.get(leg), dict):
            dates.append(_parse(pos[leg].get("end_date")))
    dates = [d for d in dates if d is not None]
    return max(dates) if dates else None


def find_stale(data_dir, now, grace_hours=GRACE_HOURS):
    """Liste (datei, beschreibung, enddatum, stunden_ueberfaellig) aller nach der Karenz noch offenen Positionen
    (Warnstufe und Fehlerstufe; die Trennung macht main() anhand von ERROR_HOURS)."""
    stale = []
    for path in sorted(glob.glob(os.path.join(data_dir, "fund_state*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as e:
            stale.append((os.path.basename(path), f"State-Datei unlesbar: {e}", None, None))
            continue
        for pos in state.get("positions", []):
            end = position_end(pos)
            if end is None:
                continue
            ack = _parse(pos.get("alarm_quittiert_bis"))
            if ack is not None and ack > now:
                continue
            overdue = (now - end).total_seconds() / 3600.0
            if overdue > grace_hours:
                name = pos.get("frage") or pos.get("pair_id") or pos.get("market_id") or "?"
                stale.append((os.path.basename(path), name, end, overdue))
    return stale


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--data", default="data")
    ap.add_argument("--grace-hours", type=float, default=GRACE_HOURS)
    ap.add_argument("--error-hours", type=float, default=ERROR_HOURS)
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    ap_error = args.error_hours
    stale = find_stale(args.data, now, args.grace_hours)
    if not stale:
        print(f"Keine haengenden Positionen (Karenz {args.grace_hours:.0f} h nach Enddatum).")
        return 0
    errors = 0
    for fname, name, end, overdue in stale:
        when = f"Ende {end.isoformat()}, {overdue:.0f} h ueberfaellig" if end else "Datei"
        # Unlesbare Datei (end None) oder laenger als ERROR_HOURS offen: Fehler. Dazwischen: Warnung.
        if end is None or overdue > ap_error:
            errors += 1
            # GitHub-Annotation: erscheint in der Zusammenfassung des Laufs
            print(f"::error title=Position haengt::{fname}: {name[:160]} ({when})")
        else:
            print(f"::warning title=Aufloesung ausstehend::{fname}: {name[:160]} ({when}; Fehler ab {ap_error:.0f} h)")
    if errors:
        print(f"{errors} Position(en) nach Enddatum + {ap_error / 24:.0f} Tagen noch offen. Pruefen: Abrechnung defekt, "
              f"Fehlpaarung oder Aufloesung verzoegert (dann 'alarm_quittiert_bis' setzen).", file=sys.stderr)
        return 1
    print(f"{len(stale)} Position(en) beendet, Aufloesung ausstehend (Warnung, noch kein Fehler).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
