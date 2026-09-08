"""Hallucination / unsupported-claim layer: seeded unsupported statements must be flagged."""
from datetime import date

from pqm_agent.guardrails import ConclusiveLanguageGuard, RetrievalScopeGuard, UnsupportedClaimDetector, WriteBackGuard
from pqm_agent.models import (Approval, ApprovalDecision, ApprovalRole, CauseCategory, CauseType, Citation, Claim,
                              Evidence, EvidenceType, EvidenceValidationState, Hypothesis, HypothesisStatus)


def _ev(eid, state=EvidenceValidationState.VERIFIED):
    return Evidence(evidence_id=eid, case_id="C1", evidence_type=EvidenceType.TEST_REPORT, title="t", source_path="p",
                    owner="o", created_date=date(2026, 1, 1), validation_state=state)


def test_seeded_unsupported_claims_are_flagged(settings):
    det = UnsupportedClaimDetector(settings)
    evidence = [_ev("EV-1"), _ev("EV-Q", EvidenceValidationState.QUARANTINED)]
    claims = [
        Claim(text="supported", kind="fact", citations=[Citation(evidence_id="EV-1")]),
        Claim(text="no citation", kind="fact"),
        Claim(text="unknown evidence", kind="fact", citations=[Citation(evidence_id="EV-404")]),
        Claim(text="quarantined evidence", kind="fact", citations=[Citation(evidence_id="EV-Q")]),
        Claim(text="hypothesis needs no citation", kind="hypothesis"),
    ]
    result = det.check(claims, evidence)
    assert not result.passed
    assert set(result.flagged_claim_ids) == {claims[1].claim_id, claims[2].claim_id, claims[3].claim_id}
    assert claims[0].supported is True and claims[4].supported is True
    assert det.citation_coverage(claims) == 0.25


def test_conclusive_language_blocked_without_validated_approved_hypothesis(settings):
    guard = ConclusiveLanguageGuard(settings)
    hyp = Hypothesis(case_id="C1", cause_type=CauseType.OCCURRENCE, category=CauseCategory.MACHINE, description="x",
                     status=HypothesisStatus.EVIDENCE_SUPPORTED, supporting_evidence_ids=["EV-1"])
    claims = [Claim(text="The root cause is confirmed to be glass contamination", kind="fact")]
    assert not guard.check(claims, [hyp], []).passed
    assert "validation pending" in guard.sanitize(claims[0].text)
    # after a named approval + VALIDATED status the same wording is allowed
    hyp.status = HypothesisStatus.VALIDATED
    approval = Approval(case_id="C1", decision_type="validated_root_cause", linked_object_id=hyp.hypothesis_id,
                        linked_object_version=1, role=ApprovalRole.PROCESS_ENGINEER, approver="A. Kumar",
                        decision=ApprovalDecision.APPROVED)
    claims = [Claim(text="The root cause is confirmed to be glass contamination", kind="fact")]
    assert guard.check(claims, [hyp], [approval]).passed


def test_retrieval_scope_guard_blocks_other_customers(settings):
    guard = RetrievalScopeGuard(settings)
    docs = [{"case_id": "a", "customer": "BMW"}, {"case_id": "b", "customer": "OEM-B"}]
    assert [d["case_id"] for d in guard.filter(docs, "BMW")] == ["a"]
    assert len(guard.filter(docs, "BMW", explicit_cross_customer_approval=True)) == 2


def test_write_back_requires_named_approval(settings):
    guard = WriteBackGuard(settings)
    try:
        guard.require_approval("closure", "CASE-1", [])
        assert False, "should have raised"
    except PermissionError:
        pass
    wrong_role = Approval(case_id="CASE-1", decision_type="closure", linked_object_id="CASE-1", linked_object_version=1,
                          role=ApprovalRole.PRODUCTION_REP, approver="J. Novak", decision=ApprovalDecision.APPROVED)
    try:
        guard.require_approval("closure", "CASE-1", [wrong_role])
        assert False, "production rep cannot close"
    except PermissionError:
        pass
