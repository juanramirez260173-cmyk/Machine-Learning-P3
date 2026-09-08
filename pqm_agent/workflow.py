"""Case state machine, approval service and closure blockers.

The agent may *recommend* a transition; this service verifies required artefacts
and authorised approvals before the official state changes (Solution Ref 4.1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional

from .config import Settings
from .models import (Action, ActionStatus, ActionType, Approval, ApprovalDecision, ApprovalRole, Case,
                     CaseState, Discipline, DraftLabel, Hypothesis, HypothesisStatus, ProblemDefinition,
                     ReadAcrossReport)
from .store import Store


@dataclass
class TransitionCheck:
    allowed: bool
    blockers: List[str] = field(default_factory=list)


# Legal transitions (ordered lifecycle plus a few controlled loops back)
TRANSITIONS: Dict[CaseState, List[CaseState]] = {
    CaseState.NEW: [CaseState.EVIDENCE_INCOMPLETE, CaseState.CONTAINMENT_ACTIVE],
    CaseState.EVIDENCE_INCOMPLETE: [CaseState.CONTAINMENT_ACTIVE],
    CaseState.CONTAINMENT_ACTIVE: [CaseState.ROOT_CAUSE_UNDER_VALIDATION, CaseState.EVIDENCE_INCOMPLETE],
    CaseState.ROOT_CAUSE_UNDER_VALIDATION: [CaseState.CORRECTIVE_ACTION_APPROVED, CaseState.EVIDENCE_INCOMPLETE],
    CaseState.CORRECTIVE_ACTION_APPROVED: [CaseState.EFFECTIVENESS_MONITORING],
    CaseState.EFFECTIVENESS_MONITORING: [CaseState.DOCUMENTS_UPDATED, CaseState.ROOT_CAUSE_UNDER_VALIDATION],
    CaseState.DOCUMENTS_UPDATED: [CaseState.CUSTOMER_CLOSURE],
    CaseState.CUSTOMER_CLOSURE: [CaseState.ARCHIVED],
    CaseState.ARCHIVED: [],
}

STATE_DISCIPLINE = {
    CaseState.NEW: Discipline.D1,
    CaseState.EVIDENCE_INCOMPLETE: Discipline.D2,
    CaseState.CONTAINMENT_ACTIVE: Discipline.D3,
    CaseState.ROOT_CAUSE_UNDER_VALIDATION: Discipline.D4,
    CaseState.CORRECTIVE_ACTION_APPROVED: Discipline.D5,
    CaseState.EFFECTIVENESS_MONITORING: Discipline.D6,
    CaseState.DOCUMENTS_UPDATED: Discipline.D7,
    CaseState.CUSTOMER_CLOSURE: Discipline.D8,
    CaseState.ARCHIVED: Discipline.D8,
}


class ApprovalService:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def allowed_roles(self, decision_type: str) -> List[str]:
        return list(self.settings.get(f"approvals.{decision_type}", []))

    def record(self, case_id: str, decision_type: str, linked_object_id: str, linked_object_version: int,
               role: ApprovalRole, approver: str, decision: ApprovalDecision, comment: str = "") -> Approval:
        allowed = self.allowed_roles(decision_type)
        if role.value not in allowed:
            raise PermissionError(f"Role '{role.value}' may not approve '{decision_type}'. Allowed: {allowed}")
        if not approver or not approver.strip():
            raise ValueError("Approver must be a named person (FR-09)")
        approval = Approval(case_id=case_id, decision_type=decision_type, linked_object_id=linked_object_id,
                            linked_object_version=linked_object_version, role=role, approver=approver,
                            decision=decision, comment=comment)
        self.store.put(approval, actor=approver, action=f"approval:{decision_type}:{decision.value}",
                       detail={"object": linked_object_id, "version": linked_object_version, "comment": comment})
        return approval

    def has_approval(self, case_id: str, decision_type: str, linked_object_id: Optional[str] = None) -> Optional[Approval]:
        allowed = set(self.allowed_roles(decision_type))
        for a in self.store.list(Approval, case_id=case_id):
            if a.decision_type != decision_type or a.decision != ApprovalDecision.APPROVED:
                continue
            if a.role.value not in allowed:
                continue
            if linked_object_id and a.linked_object_id != linked_object_id:
                continue
            return a
        return None

    # Convenience official transitions that need an approval to be legal
    def validate_hypothesis(self, hypothesis: Hypothesis, role: ApprovalRole, approver: str, comment: str = "") -> Hypothesis:
        """Only a named engineering/quality approval moves a hypothesis to VALIDATED (FR-06)."""
        if hypothesis.status not in (HypothesisStatus.EVIDENCE_SUPPORTED,):
            raise ValueError("Only an EVIDENCE_SUPPORTED hypothesis can be validated; gather supporting evidence first")
        if not hypothesis.supporting_evidence_ids:
            raise ValueError("A hypothesis without supporting evidence cannot be validated")
        approval = self.record(hypothesis.case_id, "validated_root_cause", hypothesis.hypothesis_id,
                               hypothesis.version, role, approver, ApprovalDecision.APPROVED, comment)
        hypothesis.status = HypothesisStatus.VALIDATED
        hypothesis.validated_by_approval_id = approval.approval_id
        self.store.put(hypothesis, actor=approver, action="hypothesis:validated")
        return hypothesis


class WorkflowService:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.approvals = ApprovalService(store, settings)

    # ---------------------------------------------------------------- blockers
    def closure_blockers(self, case: Case, today: Optional[date] = None) -> List[str]:
        """Deterministic closure-blocker list (D8 check)."""
        today = today or date.today()
        blockers: List[str] = []
        cid = case.case_id
        d2 = self.store.list(ProblemDefinition, case_id=cid)
        if not d2 or d2[-1].completeness() < 1.0:
            blockers.append("D2 problem definition incomplete or missing")
        if not self.approvals.has_approval(cid, "problem_statement"):
            blockers.append("D2 problem statement not approved by 8D owner / quality lead")
        hyps = self.store.list(Hypothesis, case_id=cid)
        validated = [h for h in hyps if h.status == HypothesisStatus.VALIDATED]
        if not validated:
            blockers.append("No validated (human-approved) root cause")
        else:
            types = {h.cause_type for h in validated}
            if "occurrence" not in {t.value for t in types}:
                blockers.append("Occurrence root cause not validated")
            if "escape" not in {t.value for t in types}:
                blockers.append("Escape (non-detection) root cause not validated")
        actions = self.store.list(Action, case_id=cid)
        if not self.approvals.has_approval(cid, "containment"):
            blockers.append("Containment scope/disposition not approved")
        corrective = [a for a in actions if a.action_type in (ActionType.OCCURRENCE, ActionType.DETECTION, ActionType.SYSTEMIC)]
        if not corrective:
            blockers.append("No corrective (occurrence/detection/systemic) actions defined")
        for a in corrective:
            if a.status not in (ActionStatus.DONE, ActionStatus.VERIFIED):
                blockers.append(f"Action {a.action_id} not implemented ({a.status.value})")
            elif not a.effectiveness_accepted:
                blockers.append(f"Action {a.action_id} effectiveness not accepted")
        if not self.approvals.has_approval(cid, "effectiveness"):
            blockers.append("Effectiveness not accepted by 8D owner / customer quality")
        reports = self.store.list(ReadAcrossReport, case_id=cid)
        if not reports:
            blockers.append("PFMEA / Control Plan / WI read-across not performed")
        elif not reports[-1].d7_complete:
            gaps = [f.check_id for f in reports[-1].findings if f.blocks_closure]
            blockers.append(f"D7 read-across incomplete: {', '.join(gaps) or 'gaps present'}")
        if not self.approvals.has_approval(cid, "closure"):
            blockers.append("Closure approval by quality lead / customer process missing")
        return blockers

    def transition_check(self, case: Case, target: CaseState) -> TransitionCheck:
        if target not in TRANSITIONS.get(case.state, []):
            return TransitionCheck(False, [f"Illegal transition {case.state.value} -> {target.value}"])
        cid = case.case_id
        blockers: List[str] = []
        if target == CaseState.CONTAINMENT_ACTIVE:
            if not self.approvals.has_approval(cid, "containment"):
                blockers.append("Containment requires approval by quality/operations owner")
        if target == CaseState.ROOT_CAUSE_UNDER_VALIDATION:
            if not self.approvals.has_approval(cid, "problem_statement"):
                blockers.append("D2 problem statement must be approved before D4")
        if target == CaseState.CORRECTIVE_ACTION_APPROVED:
            if not any(h.status == HypothesisStatus.VALIDATED for h in self.store.list(Hypothesis, case_id=cid)):
                blockers.append("A validated root cause is required before corrective action approval")
            if not self.approvals.has_approval(cid, "corrective_action"):
                blockers.append("Corrective action not approved by responsible function")
        if target == CaseState.DOCUMENTS_UPDATED:
            reports = self.store.list(ReadAcrossReport, case_id=cid)
            if not reports or not reports[-1].d7_complete:
                blockers.append("Read-across must be complete (no Missing / Revision Mismatch)")
        if target == CaseState.CUSTOMER_CLOSURE:
            blockers.extend(self.closure_blockers(case))
        if target == CaseState.ARCHIVED:
            if not self.approvals.has_approval(cid, "closure"):
                blockers.append("Archive requires closure approval")
        return TransitionCheck(not blockers, blockers)

    def transition(self, case: Case, target: CaseState, actor: str) -> Case:
        check = self.transition_check(case, target)
        if not check.allowed:
            raise PermissionError("Transition blocked: " + "; ".join(check.blockers))
        previous = case.state
        case.state = target
        case.current_discipline = STATE_DISCIPLINE[target]
        if target == CaseState.CUSTOMER_CLOSURE:
            case.closure_date = date.today()
        self.store.put(case, actor=actor, action="case:transition",
                       detail={"from": previous.value, "to": target.value})
        return case
