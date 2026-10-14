#!/bin/bash
# Spielt die Push-Schleife aus den Workflow-YAMLs mit lokalem Bare-Remote nach (kein Netz).
# Zeigt: gleiche Datei in zwei Klonen -> Konflikt, Exit 1; disjunkte Dateien -> Rebase, Erfolg.
set -u; export GIT_CONFIG_GLOBAL=/dev/null; W=$(mktemp -d); cd "$W"
git init -q --bare remote.git; git clone -q remote.git seed 2>/dev/null
(cd seed && git config user.name t && git config user.email t@t && git checkout -q -b main && mkdir data \
  && echo '{"cash":100}' > data/fund_state.json && echo a > data/other.json && git add -A && git commit -qm init && git push -q origin main)
for n in A B; do git clone -q remote.git $n 2>/dev/null; (cd $n && git config user.name t && git config user.email t@t); done
loop(){ for i in 1 2 3 4 5; do git push -q origin main 2>/dev/null && { echo "push OK (Versuch $i)"; return 0; }; git pull --rebase origin main >/dev/null 2>&1; done; echo "Push nach 5 Versuchen fehlgeschlagen"; return 1; }
echo "## gleiche Datei"; (cd A && echo '{"cash":90}' > data/fund_state.json && git commit -qam A && loop); (cd B && echo '{"cash":120}' > data/fund_state.json && git commit -qam B && loop)
for n in A B; do (cd $n && git rebase --abort 2>/dev/null; git fetch -q origin && git reset -q --hard origin/main); done
echo "## disjunkte Dateien"; (cd A && echo a2 > data/other.json && git commit -qam A2 && loop); (cd B && echo '{"cash":1}' > data/fund_state.json && git commit -qam B2 && loop)
