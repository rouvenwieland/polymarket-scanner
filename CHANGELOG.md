# Änderungen

## 30.10.2026 – Feinschliff (automatischer Lauf, Eigentümer nicht erreichbar)

- **Datenwachstum begrenzt:** `data/cross_platform_hits_004.csv` wuchs um ~12 MB/Tag, weil zwei Läufe am 29.10. je
  ~25.000 abgelehnte Zeilen (fast nur Fußball-Genauergebnis-Paare) schrieben. Neu: Zeilen mit Ergebnis `ok` werden
  immer geschrieben, abgelehnte höchstens `HITS_MAX_REJECTED_PER_RUN` (1500) je Lauf, bevorzugt mit hohem Score
  (`scripts/multi_platform_scan.py`, Test in `tests/test_data_hygiene.py`). Kaufregeln, Gebühren und Abrechnung
  sind unverändert; abgelehnte Zeilen wurden nie gehandelt.
- Bestand wurde nicht angetastet (keine Historie umgeschrieben, keine Fondswerte geändert).
- Offen: Die Treffer-Dateien (~230 MB) bleiben im Repo; ein Auslagern/Komprimieren älterer Teildateien braucht
  eine Entscheidung des Eigentümers, weil `neuberechnung.py` sie liest.
