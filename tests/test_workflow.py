"""Workflow / approvals: reject / modify / approve, versioning and audit-log behaviour; no bypass."""
import pytest
from conftest import TODAY, doc_specs

from pqm_agent.models import ApprovalDecision, ApprovalRole, CaseState, HypothesisStatus


def test_cannot_close_without_approvals(supervisor, ingested):
    pkg = supervisor.run_case(ingested.case.case_id, today=TODAY, document_specs=doc_specs("documents_before.json"))
    check = supervisor.workflow.transition_check(pkg.case, CaseState.CUSTOMER_CLOSURE)
    assert not check.allowed and check.blockers  # illegal transition + blockers
    assert supervisor.workflow.closure_blockers(pkg.case, TODAY)


def test_hypothesis_validation_requires_named_engineer(supervisor, ingested):
    pkg = supervisor.run_case(ingested.case.case_id, today=TODAY)
    hyp = next(h for h in pkg.hypotheses if h.status == HypothesisStatus.EVIDENCE_SUPPORTED)
    svc = supervisor.workflow.approvals
    with pytest.raises(PermissionError):
        svc.validate_hypothesis(hyp, ApprovalRole.PRODUCTION_REP, "J. Novak")
    with pytest.raises(ValueError):
        svc.validate_hypothesis(hyp, ApprovalRole.PROCESS_ENGINEER, "   ")
    unsupported = next(h for h in pkg.hypotheses if h.status == HypothesisStatus.HYPOTHESIS)
    with pytest.raises(ValueError):
        svc.validate_hypothesis(unsupported, ApprovalRole.PROCESS_ENGINEER, "A. Kumar")
    svc.validate_hypothesis(hyp, ApprovalRole.PROCESS_ENGINEER, "A. Kumar", "test confirmed")
    assert hyp.status == HypothesisStatus.VALIDATED and hyp.validated_by_approval_id


def test_state_machine_enforces_gates_and_versions(supervisor, ingested, store):
    from pqm_agent.models import Case

    pkg = supervisor.run_case(ingested.case.case_id, today=TODAY)
    case = pkg.case
    wf = supervisor.workflow
    # containment requires approval
    with pytest.raises(PermissionError):
        wf.transition(case, CaseState.CONTAINMENT_ACTIVE, actor="S. Weber")
    wf.approvals.record(case.case_id, "containment", pkg.population.population_id, 1, ApprovalRole.QUALITY_LEAD,
                        "S. Weber", ApprovalDecision.APPROVED)
    v_before = store.require(Case, case.case_id).version
    wf.transition(case, CaseState.CONTAINMENT_ACTIVE, actor="S. Weber")
    assert store.require(Case, case.case_id).version == v_before + 1
    assert store.get_version(Case, case.case_id, v_before).state != CaseState.CONTAINMENT_ACTIVE
    # illegal jump
    with pytest.raises(PermissionError):
        wf.transition(case, CaseState.DOCUMENTS_UPDATED, actor="S. Weber")
    # rejected / modify decisions are recorded but do not count as approval
    wf.approvals.record(case.case_id, "problem_statement", pkg.d2.d2_id, 1, ApprovalRole.EIGHT_D_OWNER, "M. Ortega",
                        ApprovalDecision.MODIFY, "add boundary")
    assert wf.approvals.has_approval(case.case_id, "problem_statement") is None
    trail = store.audit_trail(case.case_id)
    assert any(e["action"] == "approval:problem_statement:modify" for e in trail)
    assert store.verify_audit_chain()


def test_wrong_role_cannot_approve(supervisor, ingested):
    with pytest.raises(PermissionError):
        supervisor.workflow.approvals.record(ingested.case.case_id, "pfmea_change", "PFMEA", 1,
                                             ApprovalRole.PRODUCTION_REP, "J. Novak", ApprovalDecision.APPROVED)
