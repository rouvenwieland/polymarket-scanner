# Lokales Modell als Match-Pruefer (Messung 18.11.2026)

Modell: qwen3-4b-instruct-2507 (Q8_0, llama.cpp auf dem Oracle-Server, 4 ARM-Kerne, JSON-Schema-Antwort, 5 Paare je Anfrage).
Referenz: Urteile in `data/llm_verification_cache.json` (6411 Faelle, davon 101 "gleich"), erzeugt von freien OpenRouter-Modellen.
Stichprobe: 200 Faelle mit festem Seed, je 100 Referenz-"gleich" und Referenz-"ungleich" (Skript `scripts/lokal_messung.py`).
Die Referenz ist keine Wahrheit; die Abweichungen wurden von Hand beurteilt.

| Referenz \ lokal | gleich | ungleich | ungueltig (= nein) |
|---|---|---|---|
| gleich (100) | 63 | 33 | 4 |
| ungleich (100) | 2 | 98 | 0 |

Handbeurteilung der 35 Abweichungen:
- Referenz "gleich", lokal "ungleich" (33): mindestens 8 davon sind Fehlurteile der Referenz, die lokal richtig abgelehnt wurden
  (7x "Spiel endet Remis" gegen "wer gewinnt?", 1x "Netanyahu out" gegen "Netanyahu wird Premier", umgekehrte Polaritaet). Weitere
  ca. 8 sind echte Gleichheiten, die lokal verpasst wurden (z. B. "Republican controls House" / "Which party wins House - Republican",
  "Neomi Rao next Supreme Court nominee"): das kostet nur verpasste Chancen.
- Referenz "ungleich", lokal "gleich" (2): "Dak Prescott 2+ Touchdowns" ist wirklich dieselbe Frage (Referenz irrte); "New Mexico State vs. Sam Houston"
  gegen "Sam Houston ... vs New Mexico State - wer gewinnt?" ist polaritaetsmaessig unklar (moeglicher echter Fehlalarm).

| Kennzahl | Referenz (OpenRouter-Modelle) | lokal |
|---|---|---|
| Falsch-positiv (Schein-"gleich" unter allen "gleich"-Urteilen) | mindestens 8 von 100 (>= 8 %) | hoechstens 1 von 65 (<= 1,5 %) |
| Falsch-negativ (echte Paare verpasst) | nicht messbar | ca. 8 von 100 + 4 ungueltig |
| Dauer | je Anfrage Sekunden | ca. 13 s je Paar auf dem ausgelasteten Server (2636 s fuer 200) |

Urteil: Das lokale Modell ist strenger als die Referenz (weniger Schein-Arbitragen, mehr verpasste echte Paare). Das Kriterium
"falsch-positiv <= Referenz" ist erfuellt, daher Standard `LLM_BACKEND=auto` (lokal zuerst, Rueckfall OpenRouter `:free` nur bei
unerreichbarem Endpunkt und vorhandenem Schluessel). Ungueltige Antworten zaehlen als "nein" und werden nicht gecacht.
Einschraenkung: 1 moeglicher Fehlalarm in 65 ist statistisch duenn; Polaritaet bei "A vs B - wer gewinnt?" bleibt die Schwachstelle.
Die Stichprobe ist zu je 100 geschichtet, nicht nach der echten Verteilung (1,6 % "gleich").
