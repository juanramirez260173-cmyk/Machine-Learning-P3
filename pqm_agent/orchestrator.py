"""Supervisor: runs the specialised agents against a controlled source set and
returns a case package. It never changes the official state itself - every
official transition goes through WorkflowService with named approvals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .agents import (AgentContext, CaseIntakeAgent, ContainmentAgent, CorrectiveActionAgent, D2ProblemDefinitionAgent,
                     EffectivenessAgent, EightDDraftingAgent, EvidenceAgent, RCAAssistant)
from .auditor import ReadAcrossAuditor
from .config import Settings, load_settings
from .escalation import EscalationEngine
from .llm import LLMProvider, make_provider
from .ml import DueDateRiskModel, build_features
from .models import (Action, Approval, Case, ControlledDocument, EightDDraft, Escalation, Evidence, EvidenceType,
                     Hypothesis, ProblemDefinition, ReadAcrossReport, SuspectPopulation)
from .retrieval import RetrievalHit, SimilarCaseService
from .store import Store
from .workflow import WorkflowService


@dataclass
class CasePackage:
    case: Case
    evidence: List[Evidence]
    missing_information: List[str]
    evidence_findings: Dict[str, List[str]]
    d2: Optional[ProblemDefinition] = None
    similar_cases: List[RetrievalHit] = field(default_factory=list)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    population: Optional[SuspectPopulation] = None
    actions: List[Action] = field(default_factory=list)
    read_across: Optional[ReadAcrossReport] = None
    escalation: Optional[Escalation] = None
    closure_blockers: List[str] = field(default_factory=list)
    risk: Optional[Dict[str, Any]] = None
    draft_internal: Optional[EightDDraft] = None
    draft_customer: Optional[EightDDraft] = None
    approvals_required: List[str] = field(default_factory=list)


class QualityEscalationSupervisor:
    def __init__(self, store: Store, settings: Optional[Settings] = None, provider: Optional[LLMProvider] = None,
                 history_dir: Optional[Path] = None):
        self.store = store
        self.settings = settings or load_settings()
        self.provider = provider or make_provider(self.settings)
        self.workflow = WorkflowService(store, self.settings)
        self.similar = SimilarCaseService(self.settings, history_dir)
        self.auditor = ReadAcrossAuditor(self.settings)
        self.escalation = EscalationEngine(self.settings)
        self.risk_model = DueDateRiskModel(self.settings)
        self.intake = CaseIntakeAgent(store, self.settings, self.provider)
        self.evidence_agent = EvidenceAgent(store, self.settings, self.provider)
        self.d2_agent = D2ProblemDefinitionAgent(store, self.settings, self.provider)
        self.rca = RCAAssistant(store, self.settings, self.provider)
        self.containment = ContainmentAgent(store, self.settings, self.provider)
        self.corrective = CorrectiveActionAgent(store, self.settings, self.provider)
        self.effectiveness = EffectivenessAgent(store, self.settings, self.provider)
        self.drafter = EightDDraftingAgent(store, self.settings, self.provider)

    # ------------------------------------------------------------------ intake
    def ingest(self, record_path: Path, evidence_dir: Optional[Path] = None) -> CasePackage:
        case, evidence, questions = self.intake.ingest(record_path, evidence_dir)
        findings = self.evidence_agent.qualify(case, evidence)
        return CasePackage(case=case, evidence=evidence, missing_information=questions, evidence_findings=findings)

    def load_documents(self, case: Case, specs: Sequence[Dict[str, Any]]) -> List[ControlledDocument]:
        from .auditor import load_controlled_document
        from .models import DocumentType, ReleaseState

        docs = []
        for s in specs:
            doc = load_controlled_document(Path(s["path"]), DocumentType(s["document_type"]), s["document_number"],
                                           s["revision"], ReleaseState(s.get("release_state", "released")), s["owner"],
                                           case_id=case.case_id,
                                           effective_date=date.fromisoformat(s["effective_date"]) if s.get("effective_date") else None)
            self.store.put(doc, actor="document_loader", action="controlled_document:loaded")
            docs.append(doc)
        return docs

    # --------------------------------------------------------------- pipeline
    def run_case(self, case_id: str, today: Optional[date] = None, document_specs: Optional[Sequence[Dict[str, Any]]] = None,
                 expected_min_revisions: Optional[Dict[str, str]] = None) -> CasePackage:
        today = today or date.today()
        case = self.store.require(Case, case_id)
        evidence = self.store.list(Evidence, case_id=case_id)
        ctx = AgentContext(case=case, evidence=evidence, settings=self.settings, extra={})
        pkg = CasePackage(case=case, evidence=evidence, missing_information=self.intake.missing_information(case, evidence),
                          evidence_findings={})
        # D2
        pkg.d2 = self.d2_agent.run(ctx)
        # Similar cases (scope-guarded RAG)
        pkg.similar_cases = self.similar.search(case)
        # D4 hypotheses (existing validated hypotheses are preserved)
        existing = self.store.list(Hypothesis, case_id=case_id)
        if existing:
            pkg.hypotheses = existing
        else:
            pkg.hypotheses = self.rca.run(ctx, pkg.similar_cases)
        # D3 containment
        existing_actions = self.store.list(Action, case_id=case_id)
        if not any(a.action_type.value == "containment" for a in existing_actions):
            pkg.population, containment_action = self.containment.run(ctx)
            existing_actions.append(containment_action)
        else:
            pops = self.store.list(SuspectPopulation, case_id=case_id)
            pkg.population = pops[-1] if pops else None
        # D5 corrective actions
        if not any(a.action_type.value != "containment" for a in existing_actions):
            existing_actions.extend(self.corrective.run(ctx, pkg.hypotheses))
        pkg.actions = self.store.list(Action, case_id=case_id)
        # D7 read-across on released documents
        if document_specs:
            self.load_documents(case, document_specs)
        documents = self.store.list(ControlledDocument, case_id=case_id)
        implementation_evidence = any(e.evidence_type in (EvidenceType.ACTION_TRACKER,) and e.structured.get("implementation_evidence")
                                      for e in evidence)
        pkg.read_across = self.auditor.audit(case_id, case.failure.failure_mode, pkg.hypotheses, pkg.actions, documents,
                                             implementation_evidence_present=implementation_evidence,
                                             expected_min_revisions=expected_min_revisions)
        self.store.put(pkg.read_across, actor=self.auditor.__class__.__name__, action="read_across:audited",
                       detail={"d7_complete": pkg.read_across.d7_complete})
        # D6 effectiveness
        closure_rec = self.effectiveness.assess(ctx, pkg.actions)
        # Escalation (deterministic)
        approvals = self.store.list(Approval, case_id=case_id)
        pkg.escalation = self.escalation.evaluate(case, pkg.actions, approvals, [pkg.read_across], documents, today)
        self.store.put(pkg.escalation, actor="escalation_engine", action="escalation:evaluated",
                       detail={"level": pkg.escalation.level.value, "triggers": pkg.escalation.triggers})
        # Closure blockers + risk
        pkg.closure_blockers = self.workflow.closure_blockers(case, today) + [f"effectiveness: {b}" for b in closure_rec.blockers]
        median = sorted(h.duration_days for h in pkg.similar_cases)[len(pkg.similar_cases) // 2] if pkg.similar_cases else 0.0
        feats = build_features(case, evidence, pkg.actions, approvals, pkg.hypotheses, [pkg.read_across], median, today)
        risk = self.risk_model.predict(feats)
        pkg.risk = {"p_miss_due_date": risk.probability, "mode": risk.mode, "top_drivers": risk.top_drivers, "features": feats}
        # Drafts
        pkg.draft_internal = self.drafter.build(ctx, pkg.d2, pkg.population, pkg.hypotheses, pkg.actions, pkg.read_across,
                                                approvals, pkg.closure_blockers, pkg.escalation, variant="internal")
        pkg.draft_customer = self.drafter.build(ctx, pkg.d2, pkg.population, pkg.hypotheses, pkg.actions, pkg.read_across,
                                                approvals, pkg.closure_blockers, pkg.escalation, variant="customer")
        pkg.approvals_required = self._approvals_required(case, pkg, approvals)
        return pkg

    def _approvals_required(self, case: Case, pkg: CasePackage, approvals: Sequence[Approval]) -> List[str]:
        have = {a.decision_type for a in approvals if a.decision.value == "approved"}
        required = []
        for decision, obj in (("problem_statement", pkg.d2), ("containment", pkg.population),
                              ("corrective_action", pkg.actions or None), ("effectiveness", pkg.actions or None)):
            if obj is not None and decision not in have:
                roles = self.settings.get(f"approvals.{decision}", [])
                required.append(f"{decision}: needs {' / '.join(roles)}")
        if not any(h.status.value == "validated" for h in pkg.hypotheses):
            required.append("validated_root_cause: needs process_engineer / quality_lead on an EVIDENCE_SUPPORTED hypothesis")
        if pkg.read_across and not pkg.read_across.d7_complete:
            required.append("pfmea_change / control_plan_change: document owners must release revisions closing the read-across gaps")
        if "closure" not in have:
            required.append("closure: needs quality_lead / customer_quality_lead after all blockers are cleared")
        return required
