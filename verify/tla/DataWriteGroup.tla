---------------------------- MODULE DataWriteGroup ----------------------------
(***************************************************************************)
(* Modell der echten Nebenlaeufigkeit des Repos polymarket-scanner:         *)
(*   - vier GitHub-Actions-Workflows (snap = polymarket_overnight,           *)
(*     watch = fund_watch, cross = cross_platform_watch,                     *)
(*     scan = multi_platform_scan), die alle data/ nach main committen       *)
(*   - Concurrency-Gruppe "polymarket-data-write" (cancel-in-progress:false):*)
(*     hoechstens EIN laufender Lauf; hoechstens EIN wartender Lauf - ein    *)
(*     neu eintreffender Lauf VERDRAENGT den wartenden (der wird "cancelled")*)
(*   - Commit-Schritt: git push; bei non-fast-forward "git pull --rebase"   *)
(*     und erneut (max. MaxTries Versuche), Konflikt => Lauf scheitert       *)
(*   - Seiteneffekt VOR dem Commit: der Scan verbraucht KI-Anfragen          *)
(*     (OpenRouter), der Tageszaehler steht aber nur in der Commit-Datei     *)
(* Schreibmengen wurden aus den Skripten abgeleitet (siehe WriteSet).       *)
(*   UseGroup=FALSE   : Gegenprobe ohne Concurrency-Gruppe                   *)
(*   AllowExternal    : manuelle Pushes auf main + serverseitig abgelehnte   *)
(*                      Pushes ("pre-receive hook declined", Lauf 38341153184)*)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS UseGroup, AllowExternal, MaxRuns, MaxTries

Wf    == {"snap", "watch", "cross", "scan"}
Files == {"poly", "snapcsv", "crossstate", "hits", "llm"}

\* polymarket_snapshot.py  -> snapshots.csv/summary.csv + Polymarket-Fonds (fund_state*.json, trades, history)
\* fund_watch.py           -> nur Polymarket-Fonds
\* cross_platform_watch.py -> Cross- und Single-Platform-Fonds
\* multi_platform_scan.py  -> hits-CSV, Cross-/Single-Fonds, KI-Cache+Budget
WriteSet == [snap  |-> {"poly", "snapcsv"},
             watch |-> {"poly"},
             cross |-> {"crossstate"},
             scan  |-> {"crossstate", "hits", "llm"}]

ExtId    == <<"ext", 0>>                 \* Kennung manueller Pushes (kein Workflow-Lauf)
Runs     == Wf \X (1..MaxRuns)
W(r)     == WriteSet[r[1]]
Calls(r) == IF r[1] = "scan" THEN 1 ELSE 0     \* KI-Anfragen pro Lauf (vereinfacht)

VARIABLES st, base, ckout, tries, main, pend, ext, durable, nExt
vars == <<st, base, ckout, tries, main, pend, ext, durable, nExt>>

Running == {r \in Runs : st[r] \in {"running", "pushing"}}

TypeOK ==
  /\ st \in [Runs -> {"new", "queued", "running", "pushing", "done", "failed", "cancelled"}]
  /\ base \in [Runs -> Nat] /\ ckout \in [Runs -> Nat] /\ tries \in [Runs -> Nat]
  /\ pend \subseteq Runs /\ Cardinality(pend) <= 1
  /\ ext \in Nat /\ durable \in Nat /\ nExt \in Nat

Init ==
  /\ st = [r \in Runs |-> "new"]
  /\ base = [r \in Runs |-> 0] /\ ckout = [r \in Runs |-> 0] /\ tries = [r \in Runs |-> 0]
  /\ main = <<>> /\ pend = {} /\ ext = 0 /\ durable = 0 /\ nExt = 0

StartRun(r) ==
  /\ st' = [st EXCEPT ![r] = "running"]
  /\ base' = [base EXCEPT ![r] = Len(main)]
  /\ ckout' = [ckout EXCEPT ![r] = Len(main)]

\* Ein Cron-Trigger kommt an.
Arrive(r) ==
  /\ st[r] = "new"
  /\ \/ /\ (~UseGroup \/ (Running = {} /\ pend = {}))        \* sofort starten
        /\ StartRun(r)
        /\ UNCHANGED <<tries, main, pend, ext, durable, nExt>>
     \/ /\ UseGroup /\ (Running # {} \/ pend # {})            \* in die Gruppe einreihen
        /\ IF pend = {}
             THEN /\ st' = [st EXCEPT ![r] = "queued"]
                  /\ pend' = {r}
             ELSE LET old == CHOOSE x \in pend : TRUE IN      \* Verdraengung des Wartenden
                  /\ st' = [st EXCEPT ![old] = "cancelled", ![r] = "queued"]
                  /\ pend' = {r}
        /\ UNCHANGED <<base, ckout, tries, main, ext, durable, nExt>>

Promote(r) ==
  /\ UseGroup /\ Running = {} /\ pend = {r}
  /\ StartRun(r) /\ pend' = {}
  /\ UNCHANGED <<tries, main, ext, durable, nExt>>

\* Skript laeuft; KI-Anfragen sind externe Seiteneffekte (nicht rueckholbar).
Compute(r) ==
  /\ st[r] = "running"
  /\ st' = [st EXCEPT ![r] = "pushing"]
  /\ ext' = ext + Calls(r)
  /\ UNCHANGED <<base, ckout, tries, main, pend, durable, nExt>>

Conflict(r) == \E i \in (base[r] + 1)..Len(main) : main[i].files \cap W(r) # {}

\* Erfolgreicher Push (fast-forward)
PushOK(r) ==
  /\ st[r] = "pushing" /\ base[r] = Len(main)
  /\ main' = Append(main, [run |-> r, files |-> W(r), cb |-> ckout[r]])
  /\ durable' = durable + Calls(r)
  /\ st' = [st EXCEPT ![r] = "done"]
  /\ UNCHANGED <<base, ckout, tries, pend, ext, nExt>>

\* non-fast-forward: pull --rebase; Konflikt => Lauf scheitert, sonst neuer Versuch
Rebase(r) ==
  /\ st[r] = "pushing" /\ base[r] < Len(main) /\ tries[r] < MaxTries
  /\ IF Conflict(r)
       THEN /\ st' = [st EXCEPT ![r] = "failed"]
            /\ base' = base
       ELSE /\ st' = st
            /\ base' = [base EXCEPT ![r] = Len(main)]
  /\ tries' = [tries EXCEPT ![r] = @ + 1]
  /\ UNCHANGED <<ckout, main, pend, ext, durable, nExt>>

\* Server lehnt den Push ab (pre-receive hook, ...)
Rejected(r) ==
  /\ AllowExternal /\ st[r] = "pushing"
  /\ tries' = [tries EXCEPT ![r] = @ + 1]
  /\ st' = IF tries[r] + 1 >= MaxTries THEN [st EXCEPT ![r] = "failed"] ELSE st
  /\ UNCHANGED <<base, ckout, main, pend, ext, durable, nExt>>

\* Rouven pusht von Hand auf main (beliebige Datei)
ExternalPush ==
  /\ AllowExternal /\ nExt < 1
  /\ \E f \in Files :
       main' = Append(main, [run |-> ExtId, files |-> {f}, cb |-> 0])
  /\ nExt' = nExt + 1
  /\ UNCHANGED <<st, base, ckout, tries, pend, ext, durable>>

Terminal == \A r \in Runs : st[r] \in {"done", "failed", "cancelled"}

Next ==
  \/ \E r \in Runs : Arrive(r) \/ Promote(r) \/ Compute(r) \/ PushOK(r) \/ Rebase(r) \/ Rejected(r)
  \/ ExternalPush
  \/ (Terminal /\ UNCHANGED vars)

Spec == Init /\ [][Next]_vars

(***************************** Invarianten *********************************)
AtMostOneWriter == UseGroup => Cardinality(Running) <= 1
SingleWriter    == Cardinality(Running) <= 1     \* ohne Gruppe NICHT garantiert

\* Kein verlorener Schreibvorgang: jeder Commit eines Laufs enthaelt den Stand ALLER frueheren
\* Commits, die dieselbe Datei beruehren (der Lauf hat sie beim Auschecken gesehen, cb >= i).
\* Eine Verletzung waere ein "stale overwrite": Berechnung auf altem Stand, per Rebase ohne
\* Textkonflikt durchgerutscht.
NoLostUpdate ==
  \A i, j \in 1..Len(main) :
     (i < j /\ main[j].run # ExtId /\ main[i].files \cap main[j].files # {}) => main[j].cb >= i

\* Kein doppeltes Buchen: kein Lauf (= keine Kauf-/Auszahlungsbuchung) steht zweimal in main.
NoDoubleCommit ==
  \A i, j \in 1..Len(main) : (i # j /\ main[i].run # ExtId) => main[i].run # main[j].run

\* Zustand konsistent nach Abbruch: gescheiterte/verdraengte Laeufe hinterlassen nichts in main.
FailedLeavesNoTrace ==
  \A r \in Runs : st[r] \in {"failed", "cancelled"} => \A i \in 1..Len(main) : main[i].run # r

\* Verfuegbarkeits-Eigenschaften (duerfen verletzt werden; TLC zeigt dann das Gegenbeispiel):
NoFailedRun   == \A r \in Runs : st[r] # "failed"
NoDroppedRun  == \A r \in Runs : st[r] # "cancelled"
BudgetDurable == ext <= durable + Cardinality({r \in Runs : st[r] = "pushing" /\ Calls(r) = 1})
==============================================================================
