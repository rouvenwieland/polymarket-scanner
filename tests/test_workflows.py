"""GitHub-Actions-Workflows: gemeinsame Concurrency-Gruppe, Timeouts, Rechte, Geheimnisse, Takt."""
import glob
import os
import re

import pytest
import yaml

import polymarket_snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))


def load(path):
    with open(path, encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    wf["on"] = wf.pop(True, wf.get("on"))  # PyYAML liest den Schluessel "on" als Boolean True
    return wf


WORKFLOWS = {os.path.basename(p): load(p) for p in FILES}
WRITER_NAMES = {"cross_platform_watch.yml", "fund_watch.yml", "multi_platform_scan.yml", "polymarket_overnight.yml"}
TEST_WORKFLOW = "tests.yml"
DATA_WRITE_GROUP = "polymarket-data-write"


def test_vier_schreibende_workflows_und_ein_test_workflow():
    assert set(WORKFLOWS) == WRITER_NAMES | {TEST_WORKFLOW}


def test_test_workflow_laeuft_pytest_nur_lesend_und_nicht_bei_daten_commits():
    """Reparatur 27.10.2026: Tests laufen in CI. Sie schreiben nichts, blockieren die Daten-Workflows nicht und
    starten nicht bei jedem Bot-Commit (data/**)."""
    wf = WORKFLOWS[TEST_WORKFLOW]
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["group"] != DATA_WRITE_GROUP
    assert "data/**" in wf["on"]["push"]["paths-ignore"]
    runs = " ".join(s.get("run", "") for s in wf["jobs"]["test"]["steps"])
    assert "pytest" in runs and "hypothesis" in runs


@pytest.mark.parametrize("name", ["cross_platform_watch.yml", "fund_watch.yml"])
def test_alarm_fuer_haengende_positionen_nach_dem_commit(name):
    """Haengt eine Position nach ihrem Enddatum, wird der Lauf rot - aber erst nach dem Commit (Daten gesichert)."""
    steps = [s.get("name") for s in WORKFLOWS[name]["jobs"]["watch"]["steps"]]
    alarm = next(i for i, s in enumerate(WORKFLOWS[name]["jobs"]["watch"]["steps"])
                 if "haengende_positionen.py" in s.get("run", ""))
    assert alarm > steps.index("Ergebnisse committen")


def test_alle_schreibenden_workflows_teilen_eine_concurrency_gruppe_ohne_abbruch():
    groups = {name: wf["concurrency"] for name, wf in WORKFLOWS.items() if name in WRITER_NAMES}
    assert len({g["group"] for g in groups.values()}) == 1
    assert all(g["cancel-in-progress"] is False for g in groups.values())


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_jeder_job_hat_ein_timeout(name):
    """Ohne timeout-minutes gilt das GitHub-Standardlimit von 360 min. Ein haengender Lauf blockiert wegen
    der gemeinsamen Concurrency-Gruppe alle anderen drei Workflows bis zu 6 Stunden."""
    for job_name, job in WORKFLOWS[name]["jobs"].items():
        assert "timeout-minutes" in job, f"{name}:{job_name}"
        assert job["timeout-minutes"] <= 120


@pytest.mark.parametrize("name", sorted(WRITER_NAMES))
def test_minimale_rechte(name):
    assert WORKFLOWS[name]["permissions"] == {"contents": "write"}


def test_geheimnis_nur_im_scan_und_nur_als_env_des_scan_schritts():
    for name, wf in WORKFLOWS.items():
        text = open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8").read()
        uses = re.findall(r"secrets\.([A-Z_]+)", text)
        if name == "multi_platform_scan.yml":
            assert set(uses) == {"OPENROUTER_API_KEY"}
        else:
            assert uses == [], name


def test_cron_passt_zu_intervall_konstante():
    cron = WORKFLOWS["polymarket_overnight.yml"]["on"]["schedule"][0]["cron"]
    hours = polymarket_snapshot.INTERVAL_MINUTES // 60
    assert re.fullmatch(rf"\d+ \*/{hours} \* \* \*", cron)


def test_zeitplaene_sind_realistisch_und_versetzt():
    """Gemessener Ist-Takt: Median 4-5 h. Kein Schedule darf dichter als 2 h deklariert sein (sonst nur
    Verdraengung in der gemeinsamen Gruppe), und die Minuten muessen verschieden und != 0 sein."""
    minutes, intervals = [], []
    for name, wf in WORKFLOWS.items():
        if name not in WRITER_NAMES:
            continue
        for item in wf["on"]["schedule"]:
            m, h = item["cron"].split()[:2]
            assert m.isdigit() and int(m) != 0, name
            assert h.startswith("*/"), name
            minutes.append(int(m))
            intervals.append(int(h[2:]))
    assert len(set(minutes)) == len(minutes)
    assert min(intervals) >= 2


@pytest.mark.parametrize("name", sorted(WRITER_NAMES))
def test_push_schleife_endet_mit_fehlercode(name):
    for job in WORKFLOWS[name]["jobs"].values():
        run = next(s["run"] for s in job["steps"] if s.get("name") == "Ergebnisse committen")
        assert "git pull --rebase origin main" in run
        assert re.search(r"exit 1\s*$", run.strip())


DATA_WRITERS = sorted(n for n, wf in WORKFLOWS.items() if wf.get("concurrency", {}).get("group") == DATA_WRITE_GROUP)


def test_gruppe_der_schreiber_ist_nicht_leer():
    assert DATA_WRITERS == sorted(WRITER_NAMES)


@pytest.mark.parametrize("name", DATA_WRITERS)
def test_checkout_holt_aktuellen_main_statt_ausloese_sha(name):
    """Regression 11.-18.10.2026: Ohne ref checkt actions/checkout den SHA vom AUSLOESEzeitpunkt aus. Ein
    Cross-Watch, der in der gemeinsamen Gruppe hinter einem Multi-Platform Scan (30-50 min) wartet, rechnete
    danach auf altem Fonds-Zustand und scheiterte beim Rebase an Inhaltskonflikten in
    data/fund_history_crossplatform*.csv. Mit ref: main holt jeder Lauf die Branch-Spitze bei seinem Start,
    also inklusive des Pushs seines Vorgaengers in der Gruppe."""
    for job_name, job in WORKFLOWS[name]["jobs"].items():
        checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert len(checkouts) == 1, f"{name}:{job_name}"
        assert (checkouts[0].get("with") or {}).get("ref") == "main", f"{name}:{job_name}"


@pytest.mark.parametrize("name", DATA_WRITERS)
def test_rebase_konflikt_wird_sauber_abgebrochen(name):
    """Scheitert git pull --rebase (Inhaltskonflikt), muss der Rebase abgebrochen und der Lauf mit klarer
    Meldung beendet werden, statt mit halbem Rebase-Zustand per bash -e zu sterben."""
    for job in WORKFLOWS[name]["jobs"].values():
        run = next(s["run"] for s in job["steps"] if s.get("name") == "Ergebnisse committen")
        assert re.search(r"if ! git pull --rebase origin main; then.*?git rebase --abort.*?exit 1\s*fi", run, re.S), name
