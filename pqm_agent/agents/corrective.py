"""Corrective Action Agent (D5/D6): maps causes to occurrence, detection and systemic actions."""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional, Sequence

from pydantic import BaseModel, Field

from ..models import Action, ActionType, Discipline, Hypothesis, HypothesisStatus
from .base import AgentContext, BaseAgent


class _ActionOut(BaseModel):
    action_type: str  # occurrence | detection | systemic
    description: str
    owner_role: str = "process_engineer"
    due_in_days: int = 30
    evidence_required: str
    effectiveness_metric: str
    effectiveness_baseline: Optional[str] = None
    effectiveness_target: Optional[str] = None
    change_risk: Optional[str] = None
    linked_hypothesis_id: Optional[str] = None
    is_critical: bool = False


class CorrectiveActionPlan(BaseModel):
    actions: List[_ActionOut] = Field(default_factory=list)


class CorrectiveActionAgent(BaseAgent):
    name = "corrective_action_agent"
    prompt_name = "corrective_actions"

    def _rule_extract(self, ctx: AgentContext, hypotheses: Sequence[Hypothesis]) -> CorrectiveActionPlan:
        plan = CorrectiveActionPlan()
        candidates = [h for h in hypotheses if h.status in (HypothesisStatus.VALIDATED, HypothesisStatus.EVIDENCE_SUPPORTED)]
        for h in candidates:
            planned = None
            for e in ctx.evidence:
                for cand in e.structured.get("candidate_causes", []):
                    if isinstance(cand, dict) and cand.get("description", "").lower() == h.description.lower():
                        planned = cand.get("proposed_action")
            if h.cause_type.value == "occurrence":
                plan.actions.append(_ActionOut(action_type="occurrence", linked_hypothesis_id=h.hypothesis_id,
                                               description=planned or f"Eliminate cause: {h.description}",
                                               evidence_required="Process change record, parameter sheet, first-off validation",
                                               effectiveness_metric="Internal defect rate for failure mode (PPM)",
                                               effectiveness_baseline="Not available", effectiveness_target="0 in 3 consecutive lots",
                                               change_risk="Requires PFMEA update and customer PPAP/change notification check", is_critical=True))
            elif h.cause_type.value == "escape":
                plan.actions.append(_ActionOut(action_type="detection", linked_hypothesis_id=h.hypothesis_id,
                                               description=planned or f"Add / upgrade detection for: {h.description}",
                                               evidence_required="Gauge R&R / detection capability study, Control Plan update",
                                               effectiveness_metric="Detection rate on seeded defects", effectiveness_target="100 % on seeded samples",
                                               change_risk="Cycle-time impact", is_critical=True))
            else:
                plan.actions.append(_ActionOut(action_type="systemic", linked_hypothesis_id=h.hypothesis_id,
                                               description=planned or f"Systemic fix: {h.description}",
                                               evidence_required="Procedure revision and training record",
                                               effectiveness_metric="Audit finding closure", due_in_days=45))
        if candidates and not any(a.action_type == "systemic" for a in plan.actions):
            plan.actions.append(_ActionOut(action_type="systemic", owner_role="quality_lead", due_in_days=45,
                                           description="Read-across lesson learned to similar products / lines and update PFMEA family",
                                           evidence_required="Lessons-learned record, PFMEA family review minutes",
                                           effectiveness_metric="Similar products reviewed (%)", effectiveness_target="100 %"))
        return plan

    def run(self, ctx: AgentContext, hypotheses: Sequence[Hypothesis]) -> List[Action]:
        hyp_text = "\n".join(h.model_dump_json(include={"hypothesis_id", "cause_type", "description", "status"}) for h in hypotheses)
        plan = self.provider.structured(self.name, self.system_prompt(),
                                        self.user_prompt(ctx, {"Hypotheses with status": hyp_text}), CorrectiveActionPlan,
                                        case_id=ctx.case.case_id, rule_fallback=lambda: self._rule_extract(ctx, hypotheses))
        case = ctx.case
        known = {h.hypothesis_id for h in hypotheses}
        actions: List[Action] = []
        for a in plan.actions:
            act = Action(case_id=case.case_id, action_type=ActionType(a.action_type),
                         discipline=Discipline.D5, description=a.description,
                         owner=case.team.get(a.owner_role, case.owner),
                         due_date=case.opened_date + timedelta(days=a.due_in_days),
                         linked_hypothesis_id=a.linked_hypothesis_id if a.linked_hypothesis_id in known else None,
                         evidence_required=a.evidence_required, effectiveness_metric=a.effectiveness_metric,
                         effectiveness_baseline=a.effectiveness_baseline, effectiveness_target=a.effectiveness_target,
                         change_risk=a.change_risk, is_critical=a.is_critical)
            self.store.put(act, actor=self.name, action="corrective_action:proposed")
            actions.append(act)
        self.record_run(ctx, [a.action_id for a in actions], tools=["cause_to_action_mapping"])
        return actions
