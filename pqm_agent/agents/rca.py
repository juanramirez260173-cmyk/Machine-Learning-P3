"""RCA Assistant: Ishikawa / 5-Why hypotheses with validation plans. It never
self-validates a root cause (FR-06); validation is a named approval (workflow)."""
from __future__ import annotations

import json
from typing import List, Optional, Sequence

from pydantic import BaseModel, Field

from ..models import (CauseCategory, CauseType, Evidence, EvidenceType, EvidenceValidationState, Hypothesis,
                      HypothesisStatus)
from ..retrieval import RetrievalHit
from .base import AgentContext, BaseAgent


class _HypothesisOut(BaseModel):
    cause_type: str  # occurrence | escape | systemic
    category: str    # man | machine | material | method | measurement | environment
    description: str
    five_why_chain: List[str] = Field(default_factory=list)
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    contradicting_evidence_ids: List[str] = Field(default_factory=list)
    validation_plan: str
    priority: int = 3


class RCAExtraction(BaseModel):
    hypotheses: List[_HypothesisOut] = Field(default_factory=list)
    challenge_findings: List[str] = Field(default_factory=list)  # symptoms, circular logic, unsupported chains


class RCAAssistant(BaseAgent):
    name = "rca_assistant"
    prompt_name = "rca_hypotheses"

    KEYWORD_CATEGORY = {
        "operator": CauseCategory.MAN, "training": CauseCategory.MAN, "shift": CauseCategory.MAN,
        "fixture": CauseCategory.MACHINE, "laser": CauseCategory.MACHINE, "weld": CauseCategory.MACHINE,
        "nozzle": CauseCategory.MACHINE, "tool": CauseCategory.MACHINE, "wear": CauseCategory.MACHINE,
        "material": CauseCategory.MATERIAL, "supplier": CauseCategory.MATERIAL, "batch": CauseCategory.MATERIAL,
        "parameter": CauseCategory.METHOD, "setup": CauseCategory.METHOD, "procedure": CauseCategory.METHOD,
        "gauge": CauseCategory.MEASUREMENT, "leak test": CauseCategory.MEASUREMENT, "inspection": CauseCategory.MEASUREMENT,
        "temperature": CauseCategory.ENVIRONMENT, "humidity": CauseCategory.ENVIRONMENT,
    }

    def _rule_extract(self, ctx: AgentContext, similar: Sequence[RetrievalHit]) -> RCAExtraction:
        """Deterministic hypotheses: from evidence 'structured.candidate_causes' fields and
        from similar historical cases (as prior cases, not as evidence)."""
        out = RCAExtraction()
        usable = [e for e in ctx.evidence if e.validation_state in (EvidenceValidationState.VERIFIED, EvidenceValidationState.UNVERIFIED)]
        seen = set()
        for e in usable:
            for cand in e.structured.get("candidate_causes", []):
                desc = cand["description"] if isinstance(cand, dict) else str(cand)
                ctype = (cand.get("cause_type") if isinstance(cand, dict) else None) or "occurrence"
                key = (ctype, desc.lower())
                if key in seen:
                    continue
                seen.add(key)
                cat = next((c for k, c in self.KEYWORD_CATEGORY.items() if k in desc.lower()), CauseCategory.METHOD)
                out.hypotheses.append(_HypothesisOut(
                    cause_type=ctype, category=cat.value, description=desc,
                    five_why_chain=(cand.get("five_why") if isinstance(cand, dict) else None) or [desc],
                    supporting_evidence_ids=[e.evidence_id],
                    validation_plan=(cand.get("validation_plan") if isinstance(cand, dict) else None)
                    or "Define discriminating test (measurement / DOE / capability study) before acceptance",
                    priority=int(cand.get("priority", 2)) if isinstance(cand, dict) else 2))
        for hit in similar[:3]:
            key = ("occurrence", hit.root_cause_family.lower())
            if key in seen:
                continue
            seen.add(key)
            out.hypotheses.append(_HypothesisOut(
                cause_type="occurrence", category=CauseCategory.METHOD.value,
                description=f"Similar past case {hit.case_id}: {hit.root_cause_family}",
                five_why_chain=[f"Prior case {hit.case_id} showed {hit.root_cause_family}"],
                supporting_evidence_ids=[],  # historical case is a prior, not case evidence
                validation_plan=f"Check whether conditions of {hit.case_id} apply; replicate its discriminating test",
                priority=3))
        if not any(h.cause_type == "escape" for h in out.hypotheses):
            out.hypotheses.append(_HypothesisOut(
                cause_type="escape", category=CauseCategory.MEASUREMENT.value,
                description="Escape hypothesis: end-of-line detection did not cover this failure mode / defect location",
                five_why_chain=["Defect reached the customer", "EOL test did not detect it", "Detection method or coverage insufficient"],
                supporting_evidence_ids=[e.evidence_id for e in usable if e.evidence_type == EvidenceType.TEST_REPORT][:1],
                validation_plan="Gauge R&R / detection capability study on known-defective samples", priority=2))
        # challenge: hypotheses without evidence, or only symptom restatement
        for h in out.hypotheses:
            if not h.supporting_evidence_ids:
                out.challenge_findings.append(f"'{h.description[:60]}' has no supporting evidence in this case - remains a hypothesis")
            if h.description.lower().startswith(ctx.case.failure.failure_mode.lower()):
                out.challenge_findings.append(f"'{h.description[:60]}' restates the symptom, not a cause")
        return out

    def run(self, ctx: AgentContext, similar: Sequence[RetrievalHit]) -> List[Hypothesis]:
        similar_text = "\n".join(json.dumps({"case_id": h.case_id, "title": h.title, "root_cause_family": h.root_cause_family,
                                             "actions": h.corrective_actions, "explanation": h.explanation}) for h in similar)
        extraction = self.provider.structured(
            self.name, self.system_prompt(), self.user_prompt(ctx, {"Similar historical cases (priors, not evidence)": similar_text}),
            RCAExtraction, case_id=ctx.case.case_id, rule_fallback=lambda: self._rule_extract(ctx, similar))
        register = {e.evidence_id: e for e in ctx.evidence}
        hyps: List[Hypothesis] = []
        for h in extraction.hypotheses:
            supporting = [i for i in h.supporting_evidence_ids if i in register
                          and register[i].validation_state in (EvidenceValidationState.VERIFIED, EvidenceValidationState.UNVERIFIED)]
            contradicting = [i for i in h.contradicting_evidence_ids if i in register]
            hyp = Hypothesis(case_id=ctx.case.case_id, cause_type=CauseType(h.cause_type), category=CauseCategory(h.category),
                             description=h.description, five_why_chain=h.five_why_chain,
                             supporting_evidence_ids=supporting, contradicting_evidence_ids=contradicting,
                             validation_plan=h.validation_plan, priority=h.priority,
                             status=HypothesisStatus.EVIDENCE_SUPPORTED if supporting and not contradicting else HypothesisStatus.HYPOTHESIS)
            self.store.put(hyp, actor=self.name, action="hypothesis:proposed")
            hyps.append(hyp)
        ctx.extra["rca_challenge_findings"] = list(extraction.challenge_findings)
        self.record_run(ctx, [h.hypothesis_id for h in hyps], tools=["similar_case_rag", "ishikawa_5why"])
        return hyps
