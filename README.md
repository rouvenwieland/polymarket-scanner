# polymarket-scanner

Papierhandel-Simulation: Der Scanner sucht Preisunterschiede zwischen Prognosemärkten (Polymarket, Kalshi,
PredictIt, SX Bet) und lässt mehrere Fonds mit Spielgeld darauf handeln. **Kein echtes Geld, keine
Anlageberatung.** Alle Zahlen sind Simulationen unter den unten genannten Annahmen.

## Strategie: nur echte Arbitrage (seit 27.10.2026)

Ein Kauf findet nur statt, wenn **alle** Bedingungen erfüllt sind. Die Prüfung steht in
`scripts/arbitrage_check.py` (`check_pair`), sie läuft vor jedem Kauf; schlägt eine Bedingung fehl, wird nicht gekauft.

1. **Dasselbe Ereignis.** Textabgleich mit harten Gegenbeweisen (`scripts/cross_platform.py`) und eine KI-Prüfung
   (`scripts/llm_verify.py`, bei Ausfall kein Kauf).
2. **Seiten eindeutig zugeordnet.** Jeder Markt sagt, was seine Ja-Seite bedeutet (`yes_label`/`no_label`). Bei Sport
   sind das Teamnamen in der Reihenfolge der jeweiligen Plattform. Die Zuordnung ist „same“ (Ja = Ja), „flipped“
   (Ja auf A = Nein auf B) oder unklar, und bei unklar wird nicht gekauft. Anlass war das Paar *Lamar vs.
   Southeastern Louisiana*, bei dem beide Beine auf dasselbe Team setzten.
3. **Gleiche Auflösungsregeln.** Ausgeschlossen sind:
   - Kopie- und Testmärkte;
   - Fragen, die an einer Kandidatenliste hängen („first listed“, „of these“);
   - plattformabhängige Definitionen („closest race“, „most seats“);
   - Sportpaare mit anderem Markttyp oder anderer Anstoßzeit;
   - Kalshi-Sport, weil Kalshi einen abgesagten Spieltag zu einem vorher unbekannten „fair price“ abrechnet.
4. **Ausführbare Preise.** Nur Orderbuch-Asks, und der Kursunterschied darf höchstens **10 %** betragen. Größere
   Lücken waren bisher immer Fehlpaarungen.
5. **Gewinn in jedem Ausgang, nach allen Gebühren.** Der garantierte Gewinn muss mindestens 3 Cent je Anteilspaar
   betragen. Gebühren:
   - **Polymarket:** Taker-Gebühr C × Satz × p × (1 − p), Satz aus dem Markt (`feeSchedule`); ist er positiv,
     mindestens 0,05.
   - **Kalshi:** Taker-Gebühr.
   - **PredictIt:** 10 % auf den Gewinn.
   - **SX Bet:** 2 % auf den Gewinn (vorsichtige Annahme).

   Bei Sport zählen auch Absage und gemischte Fälle, also eine Seite abgesagt und die andere entschieden. Damit
   besteht derzeit **kein** Sportpaar: Die Plattformen behandeln Absagen verschieden, es ist also keine Arbitrage.
6. **Kapitalkosten.** Die garantierte Rendite muss mindestens 4 % pro Jahr bis zur späteren Auflösung beider Beine
   erreichen. Fehlt ein Enddatum, werden 30 Tage aufgeschlagen.

**Größe:** Die Größe richtet sich nach der echten Orderbuch-Tiefe beider Beine. Jede weitere Tranche muss die
Bedingungen 5 und 6 noch erfüllen. Weitere Grenzen:
- höchstens 10 % des Fondswerts je Paar;
- ohne abrufbare Tiefe höchstens 10 USD je Bein (PredictIt).

Eigenschaftstests (Hypothesis) prüfen zwei Dinge:
- Jeder Kauf gewinnt in jedem modellierten Ausgang.
- Bei jeder Teamreihenfolge setzt genau ein Bein auf jeden möglichen Sieger.

Die Tests stehen in `tests/test_cross_platform_fund.py`.

## Fonds

| Fonds | Start | Was er macht |
|---|---|---|
| Konservativ, Aggressiv, Best Case | je 100 USD | Ja + Nein im selben Polymarket-Markt unter 1 USD. Mit ausführbaren Preisen ist das unmöglich (Ask Ja + Ask Nein = 1 + Geld-Brief-Spanne), deshalb handeln sie nicht. |
| Cross-Platform 100 / 1.000 / 10.000 | 100 / 1.000 / 10.000 USD | Gleiche Strategie (oben), nur das Kapital unterscheidet sich. So wird sichtbar, ob mehr Kapital mehr Ertrag bringt. |
| Kalshi / PredictIt / SX Bet Solo | je 100 USD | Ja + Nein im selben Markt der jeweiligen Plattform; haben nie gehandelt. |

**Bewertung (Spalten der `fund_history_*.csv`):**
- **`nav`:** Kasse plus Anschaffungswert der offenen Paare. Gewinne zählen erst bei der Auflösung.
- **`liquidationswert`:** Wert zu Geldkursen, also sofort verkaufbar.
- **`terminierungswert`:** garantierter Endwert im schlechtesten Ausgang.

## Datenreihen

- `data/fund_*`: Live-Fonds, die die Workflows fortschreiben. Seit 27.10.2026 setzen sie die **korrigierte**
  Neuberechnung fort.
- `data/korrigiert/`: eingefrorene Neuberechnung von Anfang an. Erzeugt wird sie von `scripts/neuberechnung.py`,
  reproduzierbar aus `data/korrigiert/quellen/`. Die Prüfung von Hand je Paar steht in `paarpruefung.csv`, die
  Ergebnisse in `zusammenfassung.md`.
- `data/archiv/vor_reparatur_2026-10-27/`: die alten, fehlerhaften Reihen, unverändert (mit Prüfsummen).
- `data/archiv/vor_bereinigung/`: Stand vor der Bereinigung vom 14.10.2026.
- `data/cross_platform_hits*.csv`: jeder gefundene Treffer. Seit 27.10. stehen dort auch Markt-IDs, Beinpreise,
  Preisbasis, Seitenzuordnung und das Prüfergebnis (neue Teildatei).

## Überwachung

- **`.github/workflows/tests.yml`:** führt alle Tests aus. Er läuft bei Code-Änderungen, bei Pull Requests und
  täglich.
- **`scripts/haengende_positionen.py`:** färbt Fonds-Watch und Cross-Watch rot, wenn eine Position mehr als 72 h nach
  ihrem Enddatum noch offen ist. Wer eine Position bewusst offen lässt, setzt im State `"alarm_quittiert_bis"`.

## Neuberechnung ausführen

```
python scripts/neuberechnung.py               # offline aus den gespeicherten Quellen
python scripts/neuberechnung.py --abrufen     # Quellen neu holen (clob.polymarket.com ist aus Deutschland nicht erreichbar,
                                              # deshalb lief das in GitHub Actions)
```

## Grenzen und Risiken (auch bei „echter“ Arbitrage)

- **Auflösungsstreit:** UMA-Einspruch bei Polymarket; PredictIt entscheidet nach eigenen Regeln. Der PredictIt-
  Regeltext ist über die API nicht abrufbar.
- **Plattformrisiko:**
  - PredictIt: Rechtsstatus, Limits je Contract.
  - Polymarket: aus Deutschland nicht erreichbar.
  - SX Bet: On-Chain-Risiko.
- **Auszahlung:** Sie dauert und kann Gebühren kosten. Die frühere 5-%-Auszahlungsgebühr von PredictIt gilt laut
  Produktmeldung seit 23.09.2026 nicht mehr. Das ist nicht unabhängig geprüft; käme sie zurück, wäre jede
  PredictIt-Arbitrage unprofitabel.
- **Teilausführung:** Die Simulation nimmt an, dass beide Beine gleichzeitig voll gefüllt werden. Real kann ein Bein
  offen bleiben.
- **Tiefe und Limits:** Ohne echte Buchtiefe rechnet der Fonds mit 10 USD je Bein. Mehr Kapital bringt deshalb kaum
  mehr Ertrag.
