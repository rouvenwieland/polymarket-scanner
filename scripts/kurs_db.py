"""Optionaler Echtzeit-Kurs aus der SQLite-Datei des WebSocket-Sammlers (Oracle-Server).

Ohne Datei, ohne Zeile oder bei einem Kurs aelter als ``MAX_ALTER_S`` liefert ``lese`` None; der
Aufrufer nimmt dann unveraendert die REST-Abfrage. Pfad per Umgebungsvariable ``KURS_DB``.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

STANDARD_PFAD = "/srv/scanner/ws/kurs.db"
MAX_ALTER_S = 60.0


def lese(quelle: str, instrument: str, max_alter_s: float = MAX_ALTER_S, pfad: Optional[str] = None,
         jetzt: Optional[datetime] = None) -> Optional[dict]:
    pfad = pfad or os.environ.get("KURS_DB", STANDARD_PFAD)
    if not os.path.isfile(pfad):
        return None
    try:
        con = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT zeit_utc, bid, ask, last, bid_menge, ask_menge FROM kurs "
                              "WHERE quelle=? AND instrument=? ORDER BY zeit_utc DESC LIMIT 1",
                              (quelle, instrument)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        zeit = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        alter = ((jetzt or datetime.now(timezone.utc)) - zeit).total_seconds()
        if alter > max_alter_s or alter < -5:
            return None
        return dict(zip(("zeit_utc", "bid", "ask", "last", "bid_menge", "ask_menge"), row))
    except (sqlite3.Error, ValueError):
        return None
