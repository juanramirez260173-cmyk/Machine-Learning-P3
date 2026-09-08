"""8D Drafting Agent: builds the D1-D8 draft only from the approved case state, with
claim-level citations, unsupported-claim flags and conclusive-language blocking."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..guardrails import ConclusiveLanguageGuard, UnsupportedClaimDetector
from ..models import (Action, ActionType, Approval, Case, Citation, Claim, Discipline, DisciplineDraft, DraftLabel,
                      EightDDraft, Escalation, Evidence, EvidenceType, EvidenceValidationState, Hypothesis, HypothesisStatus,
                      ProblemDefinition, ReadAcrossReport, SuspectPopulation)
from .base import AgentContext, BaseAgent


class EightDDraftingAgent(BaseAgent):
    name = "eight_d_drafting_agent"
    prompt_name = "eight_d_draft"

    def build(self, ctx: AgentContext, d2: Optional[ProblemDefinition], population: Optional[SuspectPopulation],
              hypotheses: Sequence[Hypothesis], actions: Sequence[Action], report: Optional[ReadAcrossReport],
              approvals: Sequence[Approval], closure_blockers: Sequence[str], escalation: Optional[Escalation],
              variant: str = "internal") -> EightDDraft:
        case = ctx.case
        template = self.settings.for_customer(case.customer).get("eight_d_template", {})
        titles = {d: template.get(d.value, d.value) for d in Discipline}
        detector = UnsupportedClaimDetector(self.settings)
        language = ConclusiveLanguageGuard(self.settings)
        complaint = next((e for e in ctx.evidence if e.evidence_type == EvidenceType.COMPLAINT_RECORD), None)
        cite_c = [Citation(evidence_id=complaint.evidence_id)] if complaint else []
        sections: List[DisciplineDraft] = []

        # D1
        d1 = DisciplineDraft(discipline=Discipline.D1, title=titles[Discipline.D1])
        d1.claims.append(Claim(text=f"8D owner: {case.owner}; team: " + (", ".join(f"{r}: {p}" for r, p in case.team.items()) or "Not available"),
                               kind="fact", citations=cite_c))
        if not case.team.get("quality_lead"):
            d1.completeness_findings.append("Quality lead not named")
        sections.append(d1)

        # D2
        d2s = DisciplineDraft(discipline=Discipline.D2, title=titles[Discipline.D2])
        if d2:
            d2s.claims.extend(d2.facts)
            if variant == "internal":
                d2s.claims.extend(d2.assumptions)
            for row in d2.is_is_not:
                d2s.claims.append(Claim(text=f"IS/IS NOT [{row.dimension}]: is '{row.is_}' / is not '{row.is_not}'", kind="fact", citations=row.citations))
            d2s.completeness_findings.extend(d2.missing_information)
            d2s.label = DraftLabel.APPROVED_INTERNAL if any(a.decision_type == "problem_statement" for a in approvals) else DraftLabel.AI_DRAFT
        else:
            d2s.completeness_findings.append("D2 not drafted")
        sections.append(d2s)

        # D3
        d3 = DisciplineDraft(discipline=Discipline.D3, title=titles[Discipline.D3])
        if population:
            d3.claims.append(Claim(text=f"Suspect population: {population.description}; range {population.part_lot_serial_range}; "
                                        f"locations {', '.join(population.locations)}", kind="recommendation",
                                   citations=[Citation(evidence_id=i) for i in population.genealogy_evidence_ids]))
        for a in actions:
            if a.action_type == ActionType.CONTAINMENT:
                d3.claims.append(Claim(text=f"Containment: {a.description}; owner {a.owner}; due {a.due_date}; exit: {a.exit_criteria}",
                                       kind="recommendation"))
                if not a.evidence_ids:
                    d3.completeness_findings.append("Containment effectiveness evidence not yet attached")
        if not any(a.decision_type == "containment" for a in approvals):
            d3.completeness_findings.append("Containment scope not approved")
        sections.append(d3)

        # D4
        d4 = DisciplineDraft(discipline=Discipline.D4, title=titles[Discipline.D4])
        ordered = sorted(hypotheses, key=lambda h: (h.status != HypothesisStatus.VALIDATED,
                                                    h.status != HypothesisStatus.EVIDENCE_SUPPORTED, h.priority))
        for h in ordered:
            if h.status == HypothesisStatus.VALIDATED:
                text = f"VALIDATED {h.cause_type.value} root cause (approval {h.validated_by_approval_id}): {h.description}"
                kind = "fact"
            else:
                text = f"{h.cause_type.value.upper()} hypothesis [{h.status.value}]: {h.description}. Validation plan: {h.validation_plan}"
                kind = "hypothesis"
            d4.claims.append(Claim(text=language.sanitize(text) if kind != "fact" else text, kind=kind,
                                   citations=[Citation(evidence_id=i) for i in h.supporting_evidence_ids]))
        if not any(h.status == HypothesisStatus.VALIDATED and h.cause_type.value == "occurrence" for h in hypotheses):
            d4.completeness_findings.append("Occurrence root cause not validated")
        if not any(h.status == HypothesisStatus.VALIDATED and h.cause_type.value == "escape" for h in hypotheses):
            d4.completeness_findings.append("Escape root cause not validated")
        for f in ctx.extra.get("rca_challenge_findings", []):
            d4.completeness_findings.append(f"Challenge: {f}")
        sections.append(d4)

        # D5 / D6
        d5 = DisciplineDraft(discipline=Discipline.D5, title=titles[Discipline.D5])
        d6 = DisciplineDraft(discipline=Discipline.D6, title=titles[Discipline.D6])
        for a in actions:
            if a.action_type == ActionType.CONTAINMENT:
                continue
            d5.claims.append(Claim(text=f"{a.action_type.value} action: {a.description}; owner {a.owner}; due {a.due_date}; "
                                        f"evidence required: {a.evidence_required}", kind="recommendation"))
            d6.claims.append(Claim(text=f"{a.action_id} status {a.status.value}; metric {a.effectiveness_metric}; "
                                        f"target {a.effectiveness_target}; result {a.effectiveness_result or 'Not available'}",
                                   kind="fact" if a.evidence_ids else "assumption",
                                   citations=[Citation(evidence_id=i) for i in a.evidence_ids]))
        if not any(a.decision_type == "corrective_action" for a in approvals):
            d5.completeness_findings.append("Corrective actions not approved by responsible function")
        sections.extend([d5, d6])

        # D7
        d7 = DisciplineDraft(discipline=Discipline.D7, title=titles[Discipline.D7])
        if report:
            for f in report.findings:
                d7.claims.append(Claim(text=f"{f.check_id} [{f.document_type.value}] {f.status.value.upper()}: {f.requirement}. {f.detail}"
                                            + (f" Proposed: {f.proposed_change}" if f.proposed_change else ""), kind="fact",
                                       citations=[Citation(evidence_id=e.evidence_id) for e in ctx.evidence
                                                  if e.evidence_type.value == f.document_type.value
                                                  and e.validation_state in (EvidenceValidationState.VERIFIED,
                                                                             EvidenceValidationState.UNVERIFIED)][-1:]))
                if f.blocks_closure:
                    d7.completeness_findings.append(f"{f.check_id} blocks closure ({f.status.value})")
        else:
            d7.completeness_findings.append("Read-across not performed")
        sections.append(d7)

        # D8
        d8 = DisciplineDraft(discipline=Discipline.D8, title=titles[Discipline.D8])
        if closure_blockers:
            d8.claims.append(Claim(text="Closure NOT recommended. Open blockers: " + "; ".join(closure_blockers), kind="recommendation"))
            d8.completeness_findings.extend(closure_blockers)
        else:
            d8.claims.append(Claim(text="All closure checks satisfied - awaiting named closure approval", kind="recommendation"))
        if escalation:
            d8.claims.append(Claim(text=f"Escalation level {escalation.level.value}: {'; '.join(escalation.triggers)}", kind="fact", citations=cite_c))
        sections.append(d8)

        # Guardrails over the whole draft
        all_claims = [c for s in sections for c in s.claims]
        detector.check(all_claims, ctx.evidence)
        language.check(all_claims, hypotheses, approvals)
        if variant == "customer":  # customer-facing variant never carries unsupported claims or assumptions
            for s in sections:
                s.claims = [c for c in s.claims if c.supported is not False and c.kind != "assumption"]
            all_claims = [c for s in sections for c in s.claims]
        draft = EightDDraft(case_id=case.case_id, variant=variant, disciplines=sections,
                            unsupported_claim_count=sum(1 for c in all_claims if c.supported is False),
                            citation_coverage=round(detector.citation_coverage(all_claims), 3))
        self.store.put(draft, actor=self.name, action=f"8d_draft:{variant}",
                       detail={"unsupported": draft.unsupported_claim_count, "coverage": draft.citation_coverage})
        self.record_run(ctx, [draft.draft_id], tools=["unsupported_claim_detector", "conclusive_language_guard"])
        return draft

    @staticmethod
    def to_markdown(draft: EightDDraft, case: Case) -> str:
        lines = [f"# 8D Report draft - {case.case_id} ({draft.variant}) - label: {draft.label.value}",
                 f"Customer: {case.customer} | Part: {case.product.part_number} {case.product.part_name} | PQM: {case.pqm_number or '-'}",
                 f"Citation coverage: {draft.citation_coverage:.0%} | Unsupported claims: {draft.unsupported_claim_count}", ""]
        for s in draft.disciplines:
            lines.append(f"## {s.discipline.value} - {s.title}  [{s.label.value}]")
            for c in s.claims:
                cites = ", ".join(ci.evidence_id for ci in c.citations) or "-"
                flag = f"  ⚠ {c.flag_reason}" if c.flag_reason else ""
                lines.append(f"- ({c.kind}) {c.text} [cites: {cites}]{flag}")
            for f in s.completeness_findings:
                lines.append(f"  - OPEN: {f}")
            lines.append("")
        return "\n".join(lines)
