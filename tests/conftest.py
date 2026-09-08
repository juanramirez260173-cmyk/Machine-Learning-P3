import os
from datetime import date
from pathlib import Path

import pytest

os.environ["PQM_AGENT_LLM"] = "rules"  # tests never call a paid model

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "samples" / "case_bmw_egr_leak"
TODAY = date(2026, 9, 12)


@pytest.fixture
def settings():
    from pqm_agent.config import load_settings

    return load_settings()


@pytest.fixture
def store():
    from pqm_agent.store import Store

    return Store(":memory:")


@pytest.fixture
def supervisor(store, settings):
    from pqm_agent.orchestrator import QualityEscalationSupervisor

    return QualityEscalationSupervisor(store, settings, history_dir=ROOT / "data" / "history")


@pytest.fixture
def ingested(supervisor):
    return supervisor.ingest(SAMPLE / "case.json", SAMPLE / "evidence")


def doc_specs(name: str):
    import json

    specs = json.loads((SAMPLE / name).read_text(encoding="utf-8"))
    for s in specs:
        s["path"] = str(SAMPLE / s["path"])
    return specs
