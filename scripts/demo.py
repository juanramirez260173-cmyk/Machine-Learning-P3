"""End-to-end demo on the synthetic BMW EGR-cooler weld-leak case.

Runs the pilot journey exactly as the reference documents describe it:
intake -> evidence qualification -> D2 -> similar cases -> hypotheses -> containment ->
corrective actions -> read-across (before and after document release) -> named approvals ->
effectiveness -> escalation -> 8D drafts -> cockpit export -> audit chain verification.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "samples" / "case_bmw_egr_leak"


def _specs(name: str):
    specs = json.loads((SAMPLE / name).read_text(encoding="utf-8"))
    for s in specs:
        s["path"] = str(SAMPLE / s["path"])
    return specs


def run_demo(db_path: Path, out_dir: Path, llm: str | None = None, today: date = date(2026, 9, 12), verbose: bool = True):
    if llm:
        os.environ["PQM_AGENT_LLM"] = llm
    from pqm_agent.agents.drafting import EightDDraftingAgent
    from pqm_agent.cockpit import CockpitService
    from pqm_agent.config import load_settings
    from pqm_agent.models import ActionStatus, ActionType, ApprovalDecision, ApprovalRole, CaseState, EvidenceType, HypothesisStatus
    from pqm_agent.orchestrator import QualityEscalationSupervisor
    from pqm_agent.store import Store

    log = print if verbose else (lambda *a, **k: None)
    if db_path.exists():
        db_path.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    store = Store(db_path)
    sup = QualityEscalationSupervisor(store, settings, history_dir=ROOT / "data" / "history")
    log(f"LLM provider: {sup.provider.name} ({sup.provider.model})")

    # 1) Intake
    pkg = sup.ingest(SAMPLE / "case.json", SAMPLE / "evidence")
    case = pkg.case
    log(f"\n[1] Case {case.case_id} ingested: {len(pkg.evidence)} evidence items; findings: {pkg.evidence_findings}")
    for q in pkg.missing_information:
        log(f"    ? {q}")

    # 2) First pipeline run on the documents as released BEFORE the corrective action
    pkg = sup.run_case(case.case_id, today=today, document_specs=_specs("documents_before.json"))
    log(f"\n[2] First run: D2 completeness {pkg.d2.completeness():.0%}; {len(pkg.similar_cases)} similar cases; "
        f"{len(pkg.hypotheses)} hypotheses; {len(pkg.actions)} actions; D7 complete = {pkg.read_across.d7_complete}")
    for h in pkg.similar_cases:
        log(f"    similar: {h.case_id} ({h.score}) {h.title} - {h.explanation}")
    for f in pkg.read_across.findings:
        log(f"    read-across {f.check_id} [{f.document_type.value}] {f.status.value}: {f.detail}")
    log(f"    escalation level {pkg.escalation.level.value}: {pkg.escalation.triggers}")
    log(f"    P(miss due date) = {pkg.risk['p_miss_due_date']:.0%} ({pkg.risk['mode']}) drivers {pkg.risk['top_drivers']}")

    # 3) Named human approvals (the agent cannot do these)
    svc = sup.workflow.approvals
    svc.record(case.case_id, "problem_statement", pkg.d2.d2_id, pkg.d2.version, ApprovalRole.EIGHT_D_OWNER, "M. Ortega",
               ApprovalDecision.APPROVED, "D2 reviewed against PQM record")
    svc.record(case.case_id, "containment", pkg.population.population_id, pkg.population.version, ApprovalRole.QUALITY_LEAD,
               "S. Weber", ApprovalDecision.APPROVED, "Scope agreed with customer: 100 % re-test at 2.5 bar")
    sup.workflow.transition(case, CaseState.CONTAINMENT_ACTIVE, actor="S. Weber")
    occurrence = next(h for h in pkg.hypotheses if h.cause_type.value == "occurrence" and h.status == HypothesisStatus.EVIDENCE_SUPPORTED and "glass" in h.description.lower())
    escape = next(h for h in pkg.hypotheses if h.cause_type.value == "escape" and h.status == HypothesisStatus.EVIDENCE_SUPPORTED)
    svc.validate_hypothesis(occurrence, ApprovalRole.PROCESS_ENGINEER, "A. Kumar", "Power meter test confirmed 22 % power loss with contaminated glass")
    svc.validate_hypothesis(escape, ApprovalRole.PROCESS_ENGINEER, "A. Kumar", "Returned parts fail at 2.5 bar, pass at 1.5 bar")
    sup.workflow.transition(case, CaseState.ROOT_CAUSE_UNDER_VALIDATION, actor="S. Weber")
    log("\n[3] Approvals recorded: D2, containment, occurrence + escape root causes validated by named engineers")

    # 4) Mark corrective actions implemented with evidence, approve them
    for a in pkg.actions:
        if a.action_type != ActionType.CONTAINMENT:
            a.status = ActionStatus.DONE
            a.evidence_ids = ["EV-TEST-01"]
            a.effectiveness_result = {ActionType.OCCURRENCE: "0 rejects in 3 lots at 2.5 bar",
                                      ActionType.DETECTION: "100 % on seeded samples",
                                      ActionType.SYSTEMIC: "100 % similar products reviewed"}[a.action_type]
            a.effectiveness_accepted = True
            store.put(a, actor="A. Kumar", action="action:implemented")
        else:
            a.evidence_ids = ["EV-CONT-01"]
            store.put(a, actor="J. Novak", action="action:containment_evidence")
    svc.record(case.case_id, "corrective_action", pkg.actions[0].action_id, 1, ApprovalRole.PROCESS_ENGINEER, "A. Kumar", ApprovalDecision.APPROVED)
    svc.record(case.case_id, "effectiveness", pkg.actions[0].action_id, 1, ApprovalRole.EIGHT_D_OWNER, "M. Ortega", ApprovalDecision.APPROVED)
    sup.workflow.transition(case, CaseState.CORRECTIVE_ACTION_APPROVED, actor="S. Weber")
    sup.workflow.transition(case, CaseState.EFFECTIVENESS_MONITORING, actor="S. Weber")

    # 5) Second run BEFORE documents are released: read-across must block closure
    pkg = sup.run_case(case.case_id, today=today, expected_min_revisions={"pfmea": "C", "control_plan": "4", "work_instruction": "3"})
    log(f"\n[4] Second run with old document revisions: D7 complete = {pkg.read_across.d7_complete} (expected False)")
    for f in pkg.read_across.findings:
        log(f"    {f.check_id} {f.status.value}: {f.detail}")
    check = sup.workflow.transition_check(case, CaseState.DOCUMENTS_UPDATED)
    log(f"    transition to documents_updated allowed? {check.allowed} -> {check.blockers}")

    # 6) Document owners release PFMEA C / CP 4 / WI 3 (with implementation evidence) - third run
    from pqm_agent.models import Evidence
    impl = Evidence(evidence_id="EV-TRACK-01", case_id=case.case_id, evidence_type=EvidenceType.ACTION_TRACKER,
                    title="Action tracker with release notes and training record", source_path="sharepoint://quality/8d/8D-2026-017/tracker.xlsx",
                    owner="M. Ortega", created_date=date(2026, 9, 11), excerpt="PFMEA C, CP 4, WI 3 released 2026-09-10; training done 2026-09-11",
                    structured={"implementation_evidence": True, "action_ids": [a.action_id for a in pkg.actions]})
    store.put(impl, actor="M. Ortega", action="evidence:registered")
    svc.record(case.case_id, "pfmea_change", "PFMEA-EGR-L2", 3, ApprovalRole.PFMEA_MODERATOR, "L. Fischer", ApprovalDecision.APPROVED, "rev C released")
    svc.record(case.case_id, "control_plan_change", "CP-EGR-L2", 4, ApprovalRole.CONTROL_PLAN_OWNER, "R. Diaz", ApprovalDecision.APPROVED, "rev 4 released")
    pkg = sup.run_case(case.case_id, today=today, document_specs=_specs("documents_after.json"),
                       expected_min_revisions={"pfmea": "C", "control_plan": "4", "work_instruction": "3"})
    log(f"\n[5] Third run with released revisions: D7 complete = {pkg.read_across.d7_complete} (expected True)")
    for f in pkg.read_across.findings:
        log(f"    {f.check_id} {f.status.value}: {f.detail}")
    from pqm_agent.auditor import diff_revisions
    from pqm_agent.models import ControlledDocument
    docs = store.list(ControlledDocument, case_id=case.case_id)
    pf = {d.revision: d for d in docs if d.document_type.value == "pfmea"}
    diff = diff_revisions(pf["B"], pf["C"])
    log(f"    PFMEA diff B->C: {diff['summary']}")
    (out_dir / "pfmea_diff_B_C.json").write_text(json.dumps(diff, indent=2), encoding="utf-8")
    sup.workflow.transition(case, CaseState.DOCUMENTS_UPDATED, actor="S. Weber")
    log(f"    closure blockers now: {pkg.closure_blockers or 'none except closure approval'}")

    # 7) Closure approval by quality lead - only then can the case move to customer closure
    svc.record(case.case_id, "closure", case.case_id, case.version, ApprovalRole.QUALITY_LEAD, "S. Weber", ApprovalDecision.APPROVED,
               "Customer accepted 8D on 2026-09-12")
    check = sup.workflow.transition_check(case, CaseState.CUSTOMER_CLOSURE)
    log(f"\n[6] Closure check: allowed={check.allowed} blockers={check.blockers}")
    if check.allowed:
        sup.workflow.transition(case, CaseState.CUSTOMER_CLOSURE, actor="S. Weber")
        case.closure_date = today
        store.put(case, actor="S. Weber", action="case:closed")

    # 8) Outputs
    (out_dir / f"{case.case_id}_8D_internal.md").write_text(EightDDraftingAgent.to_markdown(pkg.draft_internal, case), encoding="utf-8")
    (out_dir / f"{case.case_id}_8D_customer.md").write_text(EightDDraftingAgent.to_markdown(pkg.draft_customer, case), encoding="utf-8")
    (out_dir / f"{case.case_id}_read_across.json").write_text(pkg.read_across.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / f"{case.case_id}_similar_cases.json").write_text(json.dumps([h.__dict__ for h in pkg.similar_cases], indent=2), encoding="utf-8")
    cockpit = CockpitService(store, settings)
    paths = cockpit.export(out_dir / "cockpit", today=today)
    log(f"\n[7] 8D drafts, read-across and cockpit written to {out_dir}")
    log(f"    executive KPIs: {json.dumps(cockpit.executive_kpis(today=today), default=str)}")
    log(f"    agent quality: {json.dumps(cockpit.agent_quality(), default=str)}")
    log(f"    audit chain valid: {store.verify_audit_chain()} ({len(store.audit_trail())} events)")
    return store, pkg, paths


if __name__ == "__main__":
    run_demo(ROOT / "out" / "demo" / "pqm_agent.db", ROOT / "out" / "demo")
