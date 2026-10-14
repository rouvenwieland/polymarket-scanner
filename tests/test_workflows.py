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
    assert cron == f"*/{polymarket_snapshot.INTERVAL_MINUTES} * * * *"


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_push_schleife_endet_mit_fehlercode(name):
    text = open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8").read()
    assert "git pull --rebase origin main" in text
    assert re.search(r"exit 1\s*$", text.strip())
