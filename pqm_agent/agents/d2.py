"""D2 Problem Definition Agent: 5W2H + Is / Is Not, facts vs assumptions (FR-02, FR-04)."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from ..guardrails import UnsupportedClaimDetector
from ..models import Citation, Claim, Evidence, EvidenceType, IsIsNot, ProblemDefinition
from .base import AgentContext, BaseAgent


class _ClaimOut(BaseModel):
    text: str
    evidence_ids: List[str] = Field(default_factory=list)


class _IsIsNotOut(BaseModel):
    dimension: str
    is_value: str
    is_not_value: str
    evidence_ids: List[str] = Field(default_factory=list)


class D2Extraction(BaseModel):
    who: Optional[str] = None
    what: Optional[str] = None
    where: Optional[str] = None
    when: Optional[str] = None
    why_problem: Optional[str] = None
    how_detected: Optional[str] = None
    how_many: Optional[str] = None
    boundary_conditions: Optional[str] = None
    is_is_not: List[_IsIsNotOut] = Field(default_factory=list)
    facts: List[_ClaimOut] = Field(default_factory=list)
    assumptions: List[_ClaimOut] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)


class D2ProblemDefinitionAgent(BaseAgent):
    name = "d2_problem_definition_agent"
    prompt_name = "d2_problem_definition"

    def _rule_extract(self, ctx: AgentContext) -> D2Extraction:
        """Deterministic extraction: only copies what exists in the complaint record and evidence."""
        case = ctx.case
        f = case.failure
        complaint = next((e for e in ctx.evidence if e.evidence_type == EvidenceType.COMPLAINT_RECORD), None)
        cite = [complaint.evidence_id] if complaint else []
        measurement = [e for e in ctx.evidence if e.evidence_type in (EvidenceType.MEASUREMENT_REPORT, EvidenceType.TEST_REPORT)]
        ext = D2Extraction()
        ext.who = f"Customer {case.customer}; reported to {case.plant}" if complaint else None
        ext.what = f"{f.failure_mode} on {case.product.part_name} ({case.product.part_number})"
        ext.where = f.location and f"{f.location}; detected at {f.detection_point or 'Not available'}" or None
        ext.when = f"Detected {case.detection_date.isoformat()}; production window {case.product.production_date_from or 'Not available'} to {case.product.production_date_to or 'Not available'}" if case.product.production_date_from else None
        ext.why_problem = f"Requirement: {f.requirement}; actual: {f.actual_result}" if f.requirement and f.actual_result else None
        ext.how_detected = f.detection_point
        ext.how_many = f"{f.quantity_affected} of {f.quantity_inspected or 'Not available'} inspected" if f.quantity_affected else None
        ext.boundary_conditions = f"Lots {', '.join(case.product.lots) or 'Not available'}; serials {', '.join(case.product.dmc_or_serials) or 'Not available'}"
        if ext.what:
            ext.facts.append(_ClaimOut(text=ext.what, evidence_ids=cite))
        if ext.why_problem:
            ext.facts.append(_ClaimOut(text=ext.why_problem, evidence_ids=cite + [m.evidence_id for m in measurement[:1]]))
        if ext.how_many:
            ext.facts.append(_ClaimOut(text=f"Quantity affected: {ext.how_many}", evidence_ids=cite))
        for m in measurement:
            if m.excerpt:
                ext.facts.append(_ClaimOut(text=m.excerpt[:200], evidence_ids=[m.evidence_id]))
        ext.is_is_not = [
            _IsIsNotOut(dimension="what", is_value=f.failure_mode, is_not_value="Other failure modes on the same part (not reported)", evidence_ids=cite),
            _IsIsNotOut(dimension="where", is_value=f.location or "Not available", is_not_value="Other locations on the part", evidence_ids=cite),
            _IsIsNotOut(dimension="when", is_value=case.detection_date.isoformat(), is_not_value="Earlier deliveries (no complaint on record)", evidence_ids=cite),
            _IsIsNotOut(dimension="how_many", is_value=str(f.quantity_affected or "Not available"), is_not_value="Remaining inspected parts", evidence_ids=cite),
        ]
        for field in ProblemDefinition.MANDATORY_FIELDS:
            if not getattr(ext, field):
                ext.missing_information.append(f"D2 field '{field}' cannot be filled from evidence - request from customer / plant")
        return ext

    def run(self, ctx: AgentContext) -> ProblemDefinition:
        extraction = self.provider.structured(self.name, self.system_prompt(), self.user_prompt(ctx), D2Extraction,
                                              case_id=ctx.case.case_id, rule_fallback=lambda: self._rule_extract(ctx))
        detector = UnsupportedClaimDetector(self.settings)
        to_claims = lambda items, kind: [Claim(text=c.text, kind=kind, citations=[Citation(evidence_id=i) for i in c.evidence_ids]) for c in items]
        facts = to_claims(extraction.facts, "fact")
        assumptions = to_claims(extraction.assumptions, "assumption")
        detector.check(facts, ctx.evidence)
        d2 = ProblemDefinition(
            case_id=ctx.case.case_id, who=extraction.who, what=extraction.what, where=extraction.where,
            when=extraction.when, why_problem=extraction.why_problem, how_detected=extraction.how_detected,
            how_many=extraction.how_many, boundary_conditions=extraction.boundary_conditions,
            is_is_not=[IsIsNot(dimension=r.dimension, is_=r.is_value, is_not=r.is_not_value,
                               citations=[Citation(evidence_id=i) for i in r.evidence_ids]) for r in extraction.is_is_not],
            facts=facts, assumptions=assumptions, missing_information=list(extraction.missing_information),
        )
        for field in ProblemDefinition.MANDATORY_FIELDS:
            if not getattr(d2, field) and not any(field in m for m in d2.missing_information):
                d2.missing_information.append(f"D2 field '{field}' missing")
        self.store.put(d2, actor=self.name, action="d2:drafted",
                       detail={"completeness": d2.completeness(), "unsupported": [c.claim_id for c in facts if c.supported is False]})
        self.record_run(ctx, [d2.d2_id], tools=["structured_extraction", "unsupported_claim_detector"])
        return d2
