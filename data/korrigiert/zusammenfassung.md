# Neuberechnung der Fonds (automatisch erzeugt von scripts/neuberechnung.py)

Nur Papierhandel, keine Anlageberatung. Strategie: echte Arbitrage nach `scripts/arbitrage_check.py`.

- Scans ausgewertet: 103, Trefferzeilen: 589558
- verworfen: Kursunterschied ausserhalb 3%-10%: 500757, Schaetzkurs (vor 14.10.): 56828, Matcher-Gegenbeweis: 195, Regeln (Text): 3077, Kalshi-Sport: 17014, KI nicht bestaetigt: 11109
- Kandidaten-Paare (KI bestaetigt, Textregeln bestanden): 40, davon automatisch bestanden: 7, nach Pruefung von Hand echt: 5
- Polymarkt-Einzelmarkt (Konservativ/Aggressiv/Best Case): 0 ausfuehrbare Treffer von 5787 Snapshot-Zeilen

| Fonds | Start | NAV (Anschaffungswert) | garantierter Endwert | Kaeufe | Auszahlungen | offen | gebunden USD |
|---|---|---|---|---|---|---|---|
| Cross-Platform (100 USD) | 100 | 100.00 | 102.30 | 5 | 0 | 5 | 50.00 |
| Cross-Platform (1.000 USD) | 1000 | 1000.00 | 1002.80 | 5 | 0 | 5 | 60.33 |
| Cross-Platform (10.000 USD) | 10000 | 10000.00 | 10002.80 | 5 | 0 | 5 | 60.33 |
| Konservativ | 100 | 100.00 | 100.00 | 0 | 0 | 0 | 0.00 |
| Aggressiv | 100 | 100.00 | 100.00 | 0 | 0 | 0 | 0.00 |
| Best Case (unrealistisch) | 100 | 100.00 | 100.00 | 0 | 0 | 0 | 0.00 |

Empfindlichkeit (gleiche Reihe, mehr Tiefe je Bein angenommen):

| Fonds@Bein-Deckel USD | NAV | garantierter Endwert | Kaeufe |
|---|---|---|---|
| cross_platform@100 | 100.00 | 102.30 | 5 |
| cross_platform@1000 | 100.00 | 102.30 | 5 |
| cross_platform_1k@100 | 1000.00 | 1023.02 | 5 |
| cross_platform_1k@1000 | 1000.00 | 1023.02 | 5 |
| cross_platform_10k@100 | 10000.00 | 10027.99 | 5 |
| cross_platform_10k@1000 | 10000.00 | 10230.22 | 5 |

## Kandidaten-Paare

| Paar | min. Summe | Zeilen | bestanden | von Hand | Grund (Beispiel) |
|---|---|---|---|---|---|
| Crypto Market Structure legislation becomes law in 2026? / Will crypto regulation (Clarity Act) be enacted in 2026? | 0.908 | 11 | 8 | abgelehnt | kein Polymarkt-Kurs zum Zeitpunkt (Kursverlauf) |
| Will the Republican Party candidate win the 2026 New York gu / Which party will win the 2026 election for governor of New Y | 0.959 | 10 | 4 | abgelehnt | garantierter Gewinn +0.027 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Michigan governor race in 2026? / Which party will win the 2026 election for governor of Michi | 0.954 | 3 | 2 | echt | garantierter Gewinn +0.028 USD je Paar nach Gebuehren unter 0.03 |
| Will Luiz Inácio Lula da Silva win the 2026 Brazilian presid / Who will win the 2026 Brazilian presidential election? - Lui | 0.910 | 14 | 1 | echt | Rendite -30.8% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.032 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Nevada governor race in 2026? / Which party will win the 2026 election for governor of Nevad | 0.920 | 12 | 1 | echt | Rendite -2.3% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.003 USD je Paar nach Gebuehren unter 0.03 |
| Will the Democrats win the Florida Senate race in 2026? / Which party will win the 2026 US Senate election in Florida? | 0.950 | 24 | 1 | echt | garantierter Gewinn +0.029 USD je Paar nach Gebuehren unter 0.03 |
| Will the Democrats win the Kentucky Senate race in 2026? / Which party will win the 2026 US Senate election in Kentucky | 0.950 | 1 | 1 | echt |  |
| Will James Ho be the next Supreme Court nominee? / Who will be the next Supreme Court nominee? - James Ho | 0.900 | 47 | 0 | - | Rendite -2.1% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.048 USD je Paar nach Gebuehren unter 0.03 |
| Will Jon Ossoff announce a Presidential run before 2028? / Will Jon Ossoff announce a run for President before 2028? -  | 0.900 | 27 | 0 | - | Rendite 1.7% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.019 USD je Paar nach Gebuehren unter 0.03 |
| Will Marco Rubio be the 2028 Republican Vice-Presidential no / Who will win the 2028 Republican vice presidential nominatio | 0.910 | 51 | 0 | - | Rendite -1.4% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.028 USD je Paar nach Gebuehren unter 0.03 |
| Tennessee-Martin vs. Western Illinois / Western Illinois vs UT Martin - wer gewinnt? | 0.914 | 2 | 0 | - | vertauschte Seiten: gespeichert ist die falsche Kombination, nicht nachrechenbar |
| Will Amul Thapar be the next Supreme Court nominee? / Who will be the next Supreme Court nominee? - Amul Thapar | 0.920 | 51 | 0 | - | Rendite -1.0% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.022 USD je Paar nach Gebuehren unter 0.03 |
| Atlanta Dream vs. Golden State Valkyries / Atlanta Dream W vs Golden State Valkyries W - wer gewinnt? | 0.924 | 4 | 0 | - | Seitenzuordnung unklar |
| Will Naftali Bennett be the next Prime Minister of Israel? / Who will be the Prime Minister of Israel after the 2026 elec | 0.930 | 17 | 0 | - | Rendite -29.2% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.033 USD je Paar nach Gebuehren unter 0.03 |
| West Florida vs. Eastern Kentucky / Eastern Kentucky vs West Florida - wer gewinnt? | 0.934 | 1 | 0 | - | vertauschte Seiten: gespeichert ist die falsche Kombination, nicht nachrechenbar |
| Will Aileen Cannon be the next Supreme Court nominee? / Who will be the next Supreme Court nominee? - Aileen Cannon | 0.935 | 8 | 0 | - | Rendite -1.1% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.025 USD je Paar nach Gebuehren unter 0.03 |
| Will Flávio Bolsonaro win the 2026 Brazilian presidential el / Who will win the 2026 Brazilian presidential election? - Flá | 0.937 | 11 | 0 | - | Rendite -26.8% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.028 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the 2028 US Presidential Election? / Which party will win the 2028 US presidential election? - Re | 0.940 | 51 | 0 | - | Rendite 0.8% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.017 USD je Paar nach Gebuehren unter 0.03 |
| Will Jon Ossoff win the 2028 Democratic presidential nominat / Who will win the 2028 Democratic presidential nomination? -  | 0.942 | 51 | 0 | - | Rendite 1.6% p.a. bis zur Aufloesung unter 4% |
| Will Ron DeSantis be the 2028 Republican Vice-Presidential n / Who will win the 2028 Republican vice presidential nominatio | 0.950 | 1 | 0 | - | Rendite 2.2% p.a. bis zur Aufloesung unter 4% |
| Will Bernadette Wilson win the 2026 Alaska governor election / Who will win the 2026 election for governor of Alaska? - Ber | 0.950 | 2 | 0 | - | garantierter Gewinn +0.030 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Ohio governor race in 2026? / Which party will win the 2026 election for governor of Ohio? | 0.955 | 2 | 0 | - | garantierter Gewinn +0.014 USD je Paar nach Gebuehren unter 0.03 |
| Will Andrew Oldham be the next Supreme Court nominee? / Who will be the next Supreme Court nominee? - Andrew Oldham | 0.955 | 37 | 0 | - | Rendite 1.3% p.a. bis zur Aufloesung unter 4% |
| Will Neomi Rao be the next Supreme Court nominee? / Who will be the next Supreme Court nominee? - Neomi Rao | 0.955 | 2 | 0 | - | Rendite -2.2% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.050 USD je Paar nach Gebuehren unter 0.03 |
| 2026 Balance of Power: R Senate, D House / What will be the balance of power in Congress after the 2026 | 0.960 | 12 | 0 | - | Rendite 3.7% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.005 USD je Paar nach Gebuehren unter 0.03 |
| Will the Democratic Party candidate win the 2026 South Carol / Which party will win the 2026 US Senate election in South Ca | 0.960 | 7 | 0 | - | garantierter Gewinn +0.017 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Wisconsin governor race in 2026 / Which party will win the 2026 election for governor of Wisco | 0.960 | 2 | 0 | - | garantierter Gewinn +0.010 USD je Paar nach Gebuehren unter 0.03 |
| Will the Democrats win the Texas Senate race in 2026? / Which party will win the 2026 US Senate election in Texas? - | 0.960 | 8 | 0 | - | Rendite -6.7% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.008 USD je Paar nach Gebuehren unter 0.03 |
| Will Marco Rubio win the 2028 Republican presidential nomina / Who will win the 2028 Republican presidential nomination? -  | 0.962 | 21 | 0 | - | Rendite 0.8% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.017 USD je Paar nach Gebuehren unter 0.03 |
| Will Andy Beshear win the 2028 Democratic presidential nomin / Who will win the 2028 Democratic presidential nomination? -  | 0.963 | 51 | 0 | - | Rendite 1.6% p.a. bis zur Aufloesung unter 4% |
| Will the Republican Party candidate win the 2026 Minnesota g / Which party will win the 2026 election for governor of Minne | 0.963 | 1 | 0 | - | garantierter Gewinn +0.030 USD je Paar nach Gebuehren unter 0.03 |
| Will Gretchen Whitmer be the 2028 Democratic Vice-Presidenti / Who will win the 2028 Democratic vice presidential nominatio | 0.966 | 12 | 0 | - | Rendite 1.3% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.027 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the South Dakota Senate race in 202 / Which party will win the 2026 US Senate election in South Da | 0.966 | 5 | 0 | - | Rendite -48.9% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.062 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Minnesota governor race in 2026 / Which party will win the 2026 election for governor of Minne | 0.967 | 3 | 0 | - | garantierter Gewinn +0.027 USD je Paar nach Gebuehren unter 0.03 |
| Will Glenn Youngkin be the 2028 Republican Vice-Presidential / Who will win the 2028 Republican vice presidential nominatio | 0.967 | 4 | 0 | - | Rendite 1.2% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.026 USD je Paar nach Gebuehren unter 0.03 |
| Will Jon Ossoff win the 2028 US Presidential Election? / Who will win the 2028 US presidential election? - Jon Ossoff | 0.968 | 3 | 0 | - | Rendite 1.0% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn +0.021 USD je Paar nach Gebuehren unter 0.03 |
| Will Hilton win the 2026 California gubernatorial election? / Who will win the 2026 election for governor of California? - | 0.968 | 1 | 0 | - | garantierter Gewinn +0.028 USD je Paar nach Gebuehren unter 0.03 |
| Will JD Vance win the 2028 US Presidential Election? / Who will win the 2028 US presidential election? - JD Vance | 0.969 | 3 | 0 | - | Rendite -2.3% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.048 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Florida Senate race in 2026? / Which party will win the 2026 US Senate election in Florida? | 0.970 | 4 | 0 | - | garantierter Gewinn +0.020 USD je Paar nach Gebuehren unter 0.03 |
| Will the Republicans win the Michigan Senate race in 2026? / Which party will win the 2026 US Senate election in Michigan | 0.970 | 1 | 0 | - | Rendite -1.1% p.a. bis zur Aufloesung unter 4%; garantierter Gewinn -0.001 USD je Paar nach Gebuehren unter 0.03 |

Grenzen der Rekonstruktion: siehe Moduldoku von scripts/neuberechnung.py.
