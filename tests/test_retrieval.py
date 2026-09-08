"""RAG evaluation: expected past case on top, and zero cross-customer leakage."""
from conftest import ROOT

from pqm_agent.retrieval import BM25Embedder, SimilarCaseService


def test_expected_similar_case_ranked_first(settings, ingested):
    svc = SimilarCaseService(settings, ROOT / "data" / "history", embedder=BM25Embedder())
    hits = svc.search(ingested.case)
    assert hits and hits[0].case_id == "8D-2024-031"
    assert "failure_mode" in hits[0].matched_factors
    assert hits[0].source_path  # citation back to source


def test_zero_cross_customer_leakage(settings, ingested):
    svc = SimilarCaseService(settings, ROOT / "data" / "history", embedder=BM25Embedder())
    hits = svc.search(ingested.case)
    # 8D-2025-101 (OEM-B) is textually the closest case but must never be returned for a BMW case
    assert all(h.case_id != "8D-2025-101" for h in hits)
    assert all(next(c for c in svc.history if c.case_id == h.case_id).customer == "BMW" for h in hits)


def test_explicit_cross_customer_approval_is_visible_and_segregated(settings, ingested):
    svc = SimilarCaseService(settings, ROOT / "data" / "history", embedder=BM25Embedder())
    hits = svc.search(ingested.case, explicit_cross_customer_approval=True)
    assert any(h.case_id == "8D-2025-101" for h in hits)
