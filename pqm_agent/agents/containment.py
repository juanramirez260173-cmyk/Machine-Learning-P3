"""Containment Agent (D3): suspect population, sorting logic, owners, exit criteria."""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from ..models import (Action, ActionType, Discipline, EvidenceType, EvidenceValidationState, SuspectPopulation)
from .base import AgentContext, BaseAgent


class ContainmentProposal(BaseModel):
    population_description: str
    part_lot_serial_range: str
    locations: List[str] = Field(default_factory=list)
    quantity_estimated: Optional[int] = None
    genealogy_evidence_ids: List[str] = Field(default_factory=list)
    unbounded_segments: List[str] = Field(default_factory=list)
    inspection_method: str
    owner_role: str = "production_representative"
    exit_criteria: str
    daily_effectiveness_evidence: str
    is_critical: bool = True


class ContainmentAgent(BaseAgent):
    name = "containment_agent"
    prompt_name = "containment"

    def _rule_extract(self, ctx: AgentContext) -> ContainmentProposal:
        case = ctx.case
        genealogy = [e for e in ctx.evidence if e.evidence_type == EvidenceType.GENEALOGY
                     and e.validation_state != EvidenceValidationState.REJECTED]
        lots = ", ".join(case.product.lots) or "Not available"
        window = f"{case.product.production_date_from or 'Not available'} .. {case.product.production_date_to or 'Not available'}"
        qty = None
        for g in genealogy:
            qty = g.structured.get("quantity_in_window", qty)
        unbounded = []
        if not genealogy:
            unbounded.append("No genealogy evidence - population cannot be bounded by traceability; contain by production window")
        if not case.product.production_date_from:
            unbounded.append("Production window unknown - extend suspect population to last clean audit")
        locations = ["supplier WIP", "supplier finished-goods", "in transit", f"{case.customer} warehouse", f"{case.customer} line"]
        return ContainmentProposal(
            population_description=f"All {case.product.part_name} produced on {case.line or case.plant} in window {window}",
            part_lot_serial_range=f"lots {lots}; window {window}",
            locations=locations, quantity_estimated=qty,
            genealogy_evidence_ids=[g.evidence_id for g in genealogy], unbounded_segments=unbounded,
            inspection_method=f"100 % inspection for '{case.failure.failure_mode}' at {case.failure.location or 'affected feature'}; "
                              f"certified-part label after sorting; segregate NOK parts",
            exit_criteria="Three consecutive clean lots AND validated corrective action implemented AND customer agreement",
            daily_effectiveness_evidence="Daily sorting log: quantity inspected / NOK / PPM, signed by shift leader",
        )

    def run(self, ctx: AgentContext) -> Tuple[SuspectPopulation, Action]:
        proposal = self.provider.structured(self.name, self.system_prompt(), self.user_prompt(ctx), ContainmentProposal,
                                            case_id=ctx.case.case_id, rule_fallback=lambda: self._rule_extract(ctx))
        case = ctx.case
        population = SuspectPopulation(case_id=case.case_id, description=proposal.population_description,
                                       part_lot_serial_range=proposal.part_lot_serial_range, plant=case.plant, line=case.line,
                                       quantity_estimated=proposal.quantity_estimated, locations=proposal.locations,
                                       genealogy_evidence_ids=proposal.genealogy_evidence_ids)
        self.store.put(population, actor=self.name, action="containment:population_proposed",
                       detail={"unbounded": proposal.unbounded_segments})
        owner = case.team.get(proposal.owner_role, case.owner)
        action = Action(case_id=case.case_id, action_type=ActionType.CONTAINMENT, discipline=Discipline.D3,
                        description=proposal.inspection_method, owner=owner,
                        due_date=case.discipline_due_dates.get("D3", case.opened_date + timedelta(days=1)),
                        evidence_required=proposal.daily_effectiveness_evidence, exit_criteria=proposal.exit_criteria,
                        is_critical=proposal.is_critical)
        self.store.put(action, actor=self.name, action="containment:action_proposed")
        ctx.extra["containment_unbounded"] = proposal.unbounded_segments
        self.record_run(ctx, [population.population_id, action.action_id], tools=["genealogy_lookup"])
        return population, action
