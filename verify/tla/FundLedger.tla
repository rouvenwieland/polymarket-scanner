------------------------------ MODULE FundLedger ------------------------------
(***************************************************************************)
(* Buchhaltung eines Polymarket-Fonds (fund_simulator.py) gegen die        *)
(* unsichere Beobachtung des Marktes. Einheiten: jede Position kostet 1,   *)
(* zahlt bei Aufloesung 1 + Gewinn. Es geht NUR um die Frage, WANN         *)
(* ausgezahlt wird:                                                        *)
(*   Fix = FALSE : urspruenglicher Code - ein Netzwerkfehler, HTTP 5xx/429 *)
(*                 oder eine unvollstaendige Marktliste (Markt "fehlt")    *)
(*                 gilt als "nicht auffindbar = aufgeloest" -> Auszahlung  *)
(*   Fix = TRUE  : nur eindeutig geschlossen (closed) oder eindeutiges 404 *)
(*                 zahlt aus; Fehler/Teil-Abruf -> halten                  *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Markets, Fix, StartCash

VARIABLES cash, held, paid, res, early

vars == <<cash, held, paid, res, early>>

Obs == {"ok_open", "ok_closed", "not_found", "error"}

Init ==
  /\ cash = StartCash /\ held = {} /\ paid = {} /\ early = {}
  /\ res = [m \in Markets |-> "open"]

Buy(m) ==
  /\ m \notin held /\ m \notin paid /\ res[m] = "open" /\ cash >= 1
  /\ cash' = cash - 1 /\ held' = held \cup {m}
  /\ UNCHANGED <<paid, res, early>>

Resolve(m) ==
  /\ res[m] = "open" /\ res' = [res EXCEPT ![m] = "resolved"]
  /\ UNCHANGED <<cash, held, paid, early>>

\* Beobachtung, die der Code tatsaechlich machen kann (Wahrheit schraenkt ein):
Possible(m, o) ==
  \/ (res[m] = "open"     /\ o \in {"ok_open", "error"})              \* offener Markt: normal oder Fehler/fehlt in Liste
  \/ (res[m] = "resolved" /\ o \in {"ok_closed", "not_found", "error"})

Settles(o) ==
  IF Fix THEN o \in {"ok_closed", "not_found"}
         ELSE o \in {"ok_closed", "not_found", "error"}

Observe(m, o) ==
  /\ m \in held /\ Possible(m, o)
  /\ IF Settles(o)
       THEN /\ cash' = cash + 1 /\ held' = held \ {m} /\ paid' = paid \cup {m}
            /\ early' = IF res[m] = "open" THEN early \cup {m} ELSE early
       ELSE UNCHANGED <<cash, held, paid, early>>
  /\ UNCHANGED res

Next == \/ \E m \in Markets : Buy(m) \/ Resolve(m) \/ \E o \in Obs : Observe(m, o)
        \/ UNCHANGED vars   \* Endzustand erlaubt (kein Deadlock-Alarm)

Spec == Init /\ [][Next]_vars

TypeOK == /\ cash \in 0..(StartCash) /\ held \subseteq Markets /\ paid \subseteq Markets
          /\ held \cap paid = {}
\* Geld wird weder erzeugt noch vernichtet (jede Position: 1 Einsatz <-> 1 Rueckfluss)
Conservation == cash + Cardinality(held) = StartCash
\* Auszahlung nur nach echter Aufloesung (sonst: Kapital zu frueh frei, Rendite durch Umschichtung aufgeblaeht)
NoEarlyPayout == early = {}
==============================================================================
