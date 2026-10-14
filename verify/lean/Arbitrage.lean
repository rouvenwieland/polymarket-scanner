import Mathlib

/-!
# Arithmetik des Polymarket- und Cross-Platform-Scanners (polymarket-scanner)

Alle Aussagen sind über `ℚ`/`ℝ` bzw. `ℕ` formuliert (keine Gleitkomma-Effekte). Sie bilden die
Formeln aus fund_simulator.py, cross_platform_fund.py und
platforms/kalshi.py ab:

1. `pair_same_market`     : Yes+No DESSELBEN Marktes zahlt in jedem Ausgang genau 1
                            (=> eine zu frühe "Auszahlung" ist ein Timing-, kein Wertfehler).
2. `cross_pair_*`         : Yes(A)+No(B) zahlt genau dann 1, wenn beide Plattformen gleich auflösen;
                            sonst 0 oder 2 (=> falsches Matching verliert den GANZEN Einsatz).
3. `fee_ceil_*`           : Aufrunden auf Cent in Ganzzahlarithmetik (Kalshi-Taker-Fee).
4. `edge_cap_*`           : `_max_shares_for_edge` ist korrekt UND scharf (Cap <=> Kosten <= Schwelle).
5. `profit_le_max`        : Der Gewinn bei linearem Kaufimpact hat ein Maximum; am Edge-Cap
                            (Durchschnittspreis = 0,999) liegt die Positionsgröße darüber, der Gewinn
                            ist dort fast null (`profit_at_cap_*`).
6. `nav_*`                : NAV-Erhaltung beim Kauf; Auszahlung erhöht das NAV um shares*(1-Kurs) >= 0.
-/

namespace Scanner

/-! ## 1/2  Auszahlungen -/

/-- Auszahlung eines Yes-Anteils bei Ausgang `o`. -/
def payYes (o : Bool) : ℚ := if o then 1 else 0
/-- Auszahlung eines No-Anteils. -/
def payNo (o : Bool) : ℚ := if o then 0 else 1

/-- Yes+No im SELBEN Markt: immer genau 1, egal wie der Markt ausgeht. -/
theorem pair_same_market (o : Bool) : payYes o + payNo o = 1 := by
  cases o <;> simp [payYes, payNo]

/-- Cross-Platform: Yes auf A, No auf B zahlt genau dann 1, wenn beide gleich auflösen. -/
theorem cross_pair_eq_one_iff (oA oB : Bool) : payYes oA + payNo oB = 1 ↔ oA = oB := by
  cases oA <;> cases oB <;> simp [payYes, payNo]

/-- Lösen die Plattformen unterschiedlich auf, zahlt das Paar 0 oder 2 (nie 1). -/
theorem cross_pair_mismatch (oA oB : Bool) (h : oA ≠ oB) :
    payYes oA + payNo oB = 0 ∨ payYes oA + payNo oB = 2 := by
  cases oA <;> cases oB <;> simp_all [payYes, payNo] <;> norm_num

/-- Bei Fehl-Match (z. B. Auszahlung 0) beträgt der Verlust den kompletten Einsatz `c`. -/
theorem mismatch_can_lose_everything (c : ℚ) (hc : 0 < c) :
    ∃ oA oB : Bool, (payYes oA + payNo oB) - c = -c := by
  exact ⟨false, true, by simp [payYes, payNo]⟩

/-! ## 3  Kalshi-Taker-Fee in Ganzzahlen
`x` = 7·C·p·(100−p) (Zähler der Fee in Zehntausendstel-Cent); Fee in Cent = ceil(x / 10000). -/

def feeCents (x : ℕ) : ℕ := (x + 9999) / 10000

theorem fee_ceil_ge (x : ℕ) : x ≤ feeCents x * 10000 := by
  unfold feeCents; omega

theorem fee_ceil_lt (x : ℕ) : feeCents x * 10000 < x + 10000 := by
  unfold feeCents; omega

/-- Ganzzahlige Fee ist genau dann `n`, wenn x in ((n-1)·10000, n·10000] liegt. -/
theorem fee_ceil_eq_iff (x n : ℕ) (hn : 0 < n) :
    feeCents x = n ↔ (n - 1) * 10000 < x ∧ x ≤ n * 10000 := by
  unfold feeCents
  constructor
  · intro h; omega
  · rintro ⟨h1, h2⟩; omega

/-- Konkreter Float-Fehlerfall aus dem Test: 4 Kontrakte @ 0,50 USD -> exakt 7 Cent (nicht 8). -/
example : feeCents (7 * 4 * 50 * 50) = 7 := by decide

/-! ## 4  Edge-Cap (`_max_shares_for_edge`)
Kosten pro Anteilspaar bei `s` Anteilen: a + b + f + q·s  (q = Σ k·p²/L > 0, linear!). -/

/-- Kosten je Paar inkl. linearem Impact und Gebühren. -/
def cost (a b f q s : ℚ) : ℚ := a + b + f + q * s

/-- Das Cap des Codes. -/
def cap (T a b f q : ℚ) : ℚ := (T - f - a - b) / q

/-- Korrektheit UND Schärfe: s <= cap  <=>  Kosten <= Schwelle T. -/
theorem edge_cap_iff (T a b f q s : ℚ) (hq : 0 < q) :
    s ≤ cap T a b f q ↔ cost a b f q s ≤ T := by
  unfold cap cost
  rw [le_div_iff₀ hq]
  constructor <;> intro h <;> nlinarith

/-- Unterhalb des Caps ist der Trade garantiert profitabel (Auszahlung 1 > Kosten), sofern T < 1. -/
theorem below_cap_profitable (T a b f q s : ℚ) (hq : 0 < q) (hT : T < 1)
    (hs : s ≤ cap T a b f q) : cost a b f q s < 1 := by
  have := (edge_cap_iff T a b f q s hq).1 hs
  linarith

/-- Ist headroom <= 0, ist das Cap nicht positiv: kein Trade (Code: `return 0.0`). -/
theorem cap_nonpos_of_no_headroom (T a b f q : ℚ) (hq : 0 < q) (h : T - f - a - b ≤ 0) :
    cap T a b f q ≤ 0 := by
  unfold cap
  exact div_nonpos_of_nonpos_of_nonneg h hq.le

/-! ## 5  Gewinnmaximum bei linearem Kaufimpact (Single-Markt, `fund_simulator`)
Preis je Paar bei Notional `n`: e(n) = s·(1 + a·n)  (s = Yes+No-Summe, a = Impact/Liquidität).
Anteile = n / e(n); Auszahlung 1 je Anteil; Gewinn P(n) = n / e(n) − n. -/

noncomputable def profit (s a n : ℝ) : ℝ := n / (s * (1 + a * n)) - n

/-- Kernidentität: Maximum minus Gewinn = (r·u−1)² / (a·r²·u). -/
theorem key_id (r u a : ℝ) (hr : 0 < r) (hu : 0 < u) (ha : 0 < a) :
    (1 - r) ^ 2 / (a * r ^ 2) - ((u - 1) / a / (r ^ 2 * u) - (u - 1) / a)
      = (r * u - 1) ^ 2 / (a * r ^ 2 * u) := by
  have h1 : r ≠ 0 := hr.ne'
  have h2 : u ≠ 0 := hu.ne'
  have h3 : a ≠ 0 := ha.ne'
  field_simp
  ring

/-- Der Gewinn ist nach oben durch (1−√s)²/(a·s) beschränkt (Maximum bei n* = (1/√s − 1)/a). -/
theorem profit_le_max (s a n : ℝ) (hs : 0 < s) (ha : 0 < a) (hn : 0 ≤ n) :
    profit s a n ≤ (1 - Real.sqrt s) ^ 2 / (a * s) := by
  have hr0 : 0 < Real.sqrt s := Real.sqrt_pos.2 hs
  have hrs : s = Real.sqrt s ^ 2 := (Real.sq_sqrt hs.le).symm
  obtain ⟨r, hr⟩ : ∃ r, r = Real.sqrt s := ⟨_, rfl⟩
  rw [← hr] at hr0 hrs ⊢
  obtain ⟨u, hu⟩ : ∃ u, u = 1 + a * n := ⟨_, rfl⟩
  have hu0 : 0 < u := by have := mul_nonneg ha.le hn; linarith
  have hn' : n = (u - 1) / a := by rw [hu]; field_simp; ring
  have hp : profit s a n = (u - 1) / a / (r ^ 2 * u) - (u - 1) / a := by
    unfold profit
    rw [← hu, ← hn', ← hrs]
  have key := key_id r u a hr0 hu0 ha
  rw [hrs] at *
  have : 0 ≤ (r * u - 1) ^ 2 / (a * r ^ 2 * u) := by positivity
  rw [hp]
  rw [← hrs] at key ⊢
  linarith

/-- Gewinn am Maximum: n* = (1/√s − 1)/a erreicht die Schranke exakt. -/
theorem profit_attains_max (s a : ℝ) (hs : 0 < s) (ha : 0 < a) :
    profit s a ((1 / Real.sqrt s - 1) / a) = (1 - Real.sqrt s) ^ 2 / (a * s) := by
  set r := Real.sqrt s with hr
  have hr0 : 0 < r := Real.sqrt_pos.2 hs
  have hrs : s = r ^ 2 := by rw [hr, Real.sq_sqrt hs.le]
  unfold profit
  rw [hrs]
  field_simp
  ring

/-- Am Edge-Cap (Durchschnittspreis e = T < 1) beträgt der Gewinn je Notional-Dollar nur 1/T − 1:
bei T = 0,999 also < 0,11 %. Das Cap des Codes sizt damit an die Grenze der NULL-Marge. -/
theorem profit_at_cap_per_dollar (T : ℝ) (hT : 0 < T) (n : ℝ) (hn : 0 < n) (s a : ℝ)
    (he : s * (1 + a * n) = T) : profit s a n / n = 1 / T - 1 := by
  unfold profit
  rw [he]
  field_simp

example : (1 : ℝ) / 0.999 - 1 < 0.0011 := by norm_num

/-- Liegt der Cap-Punkt NICHT im Optimum (r·u ≠ 1), ist der Gewinn dort strikt kleiner als das Maximum. -/
theorem profit_strictly_below_max_off_optimum (s a n : ℝ) (hs : 0 < s) (ha : 0 < a) (hn : 0 ≤ n)
    (hne : Real.sqrt s * (1 + a * n) ≠ 1) :
    profit s a n < (1 - Real.sqrt s) ^ 2 / (a * s) := by
  have hr0 : 0 < Real.sqrt s := Real.sqrt_pos.2 hs
  have hrs : s = Real.sqrt s ^ 2 := (Real.sq_sqrt hs.le).symm
  obtain ⟨r, hr⟩ : ∃ r, r = Real.sqrt s := ⟨_, rfl⟩
  rw [← hr] at hr0 hrs hne ⊢
  obtain ⟨u, hu⟩ : ∃ u, u = 1 + a * n := ⟨_, rfl⟩
  rw [← hu] at hne
  have hu0 : 0 < u := by have := mul_nonneg ha.le hn; linarith
  have hn' : n = (u - 1) / a := by rw [hu]; field_simp; ring
  have hp : profit s a n = (u - 1) / a / (r ^ 2 * u) - (u - 1) / a := by
    unfold profit
    rw [← hu, ← hn', ← hrs]
  have key := key_id r u a hr0 hu0 ha
  have hpos : 0 < (r * u - 1) ^ 2 / (a * r ^ 2 * u) := by
    have : r * u - 1 ≠ 0 := fun h => hne (by linarith)
    positivity
  rw [hp]
  rw [← hrs] at key hpos ⊢
  linarith

/-! ## 6  NAV-Erhaltung (Buchhaltung)  -/

/-- NAV = Kasse + Anteile · Kurs. -/
def nav (cash shares mark : ℚ) : ℚ := cash + shares * mark

/-- Kauf von `n` USD zum Einstandskurs `e` (Anteile = n/e), Markierung = Einstandskurs:
das NAV ändert sich nicht (keine Gebühr) – Kasse und Position tauschen nur. -/
theorem nav_invariant_on_buy (cash n e : ℚ) (he : e ≠ 0) :
    nav (cash - n) (n / e) e = nav cash 0 0 := by
  unfold nav; field_simp; ring

/-- Mit Gebühr `fee` sinkt das NAV beim Kauf um genau diese Gebühr. -/
theorem nav_buy_with_fee (cash n e fee : ℚ) (he : e ≠ 0) :
    nav (cash - n - fee) (n / e) e = nav cash 0 0 - fee := by
  unfold nav; field_simp; ring

/-- Auszahlung bei Auflösung (1 je Anteil): NAV steigt um shares·(1−Kurs) ≥ 0, solange Kurs ≤ 1. -/
theorem nav_gain_on_settlement (cash shares mark : ℚ) (hsh : 0 ≤ shares) (hm : mark ≤ 1) :
    nav (cash + shares * 1) 0 0 - nav cash shares mark = shares * (1 - mark) ∧
    0 ≤ shares * (1 - mark) := by
  refine ⟨by unfold nav; ring, mul_nonneg hsh (by linarith)⟩

/-- Ein Verkauf zum Preis `p` unter dem Einstandskurs `e` realisiert Verlust shares·(e−p) > 0, während
Halten bis zur Auflösung shares·(1−e) > 0 bringt: Halten dominiert (Zeitwert hier nicht betrachtet). -/
theorem selling_below_cost_is_dominated (shares e p : ℚ) (hsh : 0 < shares) (he : e < 1) (hp : p < e) :
    shares * p < shares * e ∧ shares * e < shares * 1 := by
  exact ⟨mul_lt_mul_of_pos_left hp hsh, mul_lt_mul_of_pos_left he hsh⟩

/-! ## 7  Orderbuch statt Mittelkurs (pricing.py)
Ein binärer Polymarket-Markt hat ein gespiegeltes Buch: Yes kaufen = ask, No kaufen = 1 − bid. -/

/-- Bei gültigem Buch (bid ≤ ask) ist die ausführbare Kauf-Summe Yes+No immer ≥ 1:
Yes+No < 1 kann im EINZELNEN Markt nie ausführbar sein, nur auf Mittel- und Letztkursen. -/
theorem book_buy_sum_ge_one (bid ask : ℚ) (h : bid ≤ ask) : 1 ≤ ask + (1 - bid) := by linarith

/-- Dual: die Verkaufs-Summe ist ≤ 1 (Bewertung zu Bids ist nie optimistischer als 1). -/
theorem book_sell_sum_le_one (bid ask : ℚ) (h : bid ≤ ask) : bid + (1 - ask) ≤ 1 := by linarith

end Scanner
