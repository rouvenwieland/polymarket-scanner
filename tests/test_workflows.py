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


def test_vier_workflows_vorhanden():
    assert set(WORKFLOWS) == {"cross_platform_watch.yml", "fund_watch.yml", "multi_platform_scan.yml", "polymarket_overnight.yml"}


def test_alle_schreibenden_workflows_teilen_eine_concurrency_gruppe_ohne_abbruch():
    groups = {name: wf["concurrency"] for name, wf in WORKFLOWS.items()}
    assert len({g["group"] for g in groups.values()}) == 1
    assert all(g["cancel-in-progress"] is False for g in groups.values())


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_jeder_job_hat_ein_timeout(name):
    """Ohne timeout-minutes gilt das GitHub-Standardlimit von 360 min. Ein haengender Lauf blockiert wegen
    der gemeinsamen Concurrency-Gruppe alle anderen drei Workflows bis zu 6 Stunden."""
    for job_name, job in WORKFLOWS[name]["jobs"].items():
        assert "timeout-minutes" in job, f"{name}:{job_name}"
        assert job["timeout-minutes"] <= 120


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
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
        for item in wf["on"]["schedule"]:
            m, h = item["cron"].split()[:2]
            assert m.isdigit() and int(m) != 0, name
            assert h.startswith("*/"), name
            minutes.append(int(m))
            intervals.append(int(h[2:]))
    assert len(set(minutes)) == len(minutes)
    assert min(intervals) >= 2


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_push_schleife_endet_mit_fehlercode(name):
    text = open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8").read()
    assert "git pull --rebase origin main" in text
    assert re.search(r"exit 1\s*$", text.strip())
