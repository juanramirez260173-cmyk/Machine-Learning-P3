"""Golden journey: the demo must reproduce the pilot acceptance behaviour."""
from pathlib import Path

from conftest import ROOT


def test_demo_journey(tmp_path):
    from scripts.demo import run_demo

    store, pkg, paths = run_demo(tmp_path / "demo.db", tmp_path / "out", llm="rules", verbose=False)
    assert pkg.read_across.d7_complete
    from pqm_agent.models import Case

    assert store.require(Case, pkg.case.case_id).state.value == "customer_closure"
    assert pkg.draft_customer.unsupported_claim_count == 0  # customer variant carries no unsupported claim
    assert pkg.draft_internal.citation_coverage >= 0.95
    assert store.verify_audit_chain()
    for p in paths.values():
        assert Path(p).exists()
    assert any(h.case_id == "8D-2024-031" for h in pkg.similar_cases)
