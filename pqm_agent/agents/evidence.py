"""Evidence Agent: maintains the evidence register, detects duplicates and superseded
revisions, quarantines low-confidence extractions and lists missing evidence (FR-03)."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Sequence

from ..models import Case, Evidence, EvidenceType, EvidenceValidationState
from .base import AgentContext, BaseAgent


class EvidenceAgent(BaseAgent):
    name = "evidence_agent"

    def qualify(self, case: Case, evidence: Sequence[Evidence]) -> Dict[str, List[str]]:
        findings: Dict[str, List[str]] = defaultdict(list)
        seen_hash: Dict[str, Evidence] = {}
        by_doc: Dict[str, List[Evidence]] = defaultdict(list)
        for ev in evidence:
            if ev.content_hash and ev.content_hash in seen_hash:
                findings["duplicates"].append(f"{ev.evidence_id} duplicates {seen_hash[ev.content_hash].evidence_id}")
                ev.validation_state = EvidenceValidationState.REJECTED
            elif ev.content_hash:
                seen_hash[ev.content_hash] = ev
            if ev.evidence_type in (EvidenceType.PFMEA, EvidenceType.CONTROL_PLAN, EvidenceType.WORK_INSTRUCTION) and ev.revision:
                by_doc[(ev.evidence_type.value, ev.title.split(" rev")[0].strip().lower())].append(ev)
            if ev.validation_state == EvidenceValidationState.QUARANTINED:
                findings["quarantined"].append(f"{ev.evidence_id} confidence {ev.extraction_confidence:.2f} < threshold -> manual review")
        for key, docs in by_doc.items():
            if len(docs) > 1:
                docs_sorted = sorted(docs, key=lambda e: (e.created_date, e.revision or ""))
                for older in docs_sorted[:-1]:
                    if older.validation_state != EvidenceValidationState.REJECTED:
                        older.validation_state = EvidenceValidationState.SUPERSEDED
                        findings["superseded"].append(f"{older.evidence_id} ({older.revision}) superseded by {docs_sorted[-1].evidence_id} ({docs_sorted[-1].revision})")
        for ev in evidence:
            self.store.put(ev, actor=self.name, action="evidence:qualified")
        ctx = AgentContext(case=case, evidence=list(evidence), settings=self.settings, extra={})
        self.record_run(ctx, [e.evidence_id for e in evidence], tools=["hash_dedupe", "revision_check"])
        return dict(findings)

    def verify(self, evidence: Evidence, actor: str) -> Evidence:
        """A named human marks an evidence item as verified."""
        evidence.validation_state = EvidenceValidationState.VERIFIED
        self.store.put(evidence, actor=actor, action="evidence:verified")
        return evidence

    def completeness(self, case: Case, evidence: Sequence[Evidence]) -> float:
        reqs = self.settings.for_customer(case.customer).get("evidence_requirements_by_discipline", {})
        required = {EvidenceType(t) for types in reqs.values() for t in types}
        present = {e.evidence_type for e in evidence
                   if e.validation_state in (EvidenceValidationState.VERIFIED, EvidenceValidationState.UNVERIFIED)}
        return len(required & present) / len(required) if required else 1.0
