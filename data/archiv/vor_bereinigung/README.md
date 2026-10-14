# Sicherung vor der Bereinigung

Unveraenderte Originale der Fonds-Dateien (trades, history, state) vom Stand vor `scripts/bereinigung.py`. Vorhandene Zeilen werden nie veraendert; neue Rohzeilen spaeterer Workflow-Laeufe werden nur ANGEHAENGT (append-only, siehe extend_archive). Pruefsummen: SHA256SUMS.txt. Die Bereinigung berechnet ihren Stand immer aus dieser Sicherung.
