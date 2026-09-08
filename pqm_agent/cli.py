"""Command-line interface for the 8D / PQM Quality-Escalation Agent."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .config import load_settings
from .models import ApprovalDecision, ApprovalRole, Case, Hypothesis
from .orchestrator import QualityEscalationSupervisor
from .store import Store
from .cockpit import CockpitService
from .agents.drafting import EightDDraftingAgent

ROOT = Path(__file__).resolve().parent.parent


def _supervisor(args) -> QualityEscalationSupervisor:
    settings = load_settings(args.config_dir)
    store = Store(args.db)
    return QualityEscalationSupervisor(store, settings, history_dir=Path(args.history) if args.history else None)


def cmd_ingest(args) -> int:
    sup = _supervisor(args)
    pkg = sup.ingest(Path(args.record), Path(args.evidence) if args.evidence else None)
    print(f"Case {pkg.case.case_id} created for {pkg.case.customer} with {len(pkg.evidence)} evidence items")
    for q in pkg.missing_information:
        print(f"  ? {q}")
    for k, v in pkg.evidence_findings.items():
        for line in v:
            print(f"  [{k}] {line}")
    return 0


def cmd_run(args) -> int:
    sup = _supervisor(args)
    specs = json.loads(Path(args.documents).read_text(encoding="utf-8")) if args.documents else None
    if specs:
        base = Path(args.documents).parent
        for s in specs:
            s["path"] = str((base / s["path"]).resolve()) if not Path(s["path"]).is_absolute() else s["path"]
    today = date.fromisoformat(args.today) if args.today else None
    pkg = sup.run_case(args.case_id, today=today, document_specs=specs,
                       expected_min_revisions=json.loads(args.expected_revisions) if args.expected_revisions else None)
    print(EightDDraftingAgent.to_markdown(pkg.draft_internal, pkg.case))
    print("## Similar cases")
    for h in pkg.similar_cases:
        print(f"- {h.case_id} score {h.score}: {h.title} ({h.explanation})")
    print("\n## Escalation")
    print(f"level {pkg.escalation.level.value} | owner {pkg.escalation.accountable_owner} | decide by {pkg.escalation.latest_acceptable_decision_date}")
    for t in pkg.escalation.triggers:
        print(f"- {t}")
    print(f"\n## Due-date risk ({pkg.risk['mode']}): P(miss) = {pkg.risk['p_miss_due_date']:.0%}; drivers: {', '.join(pkg.risk['top_drivers'])}")
    print("\n## Approvals required")
    for a in pkg.approvals_required:
        print(f"- {a}")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{pkg.case.case_id}_8D_internal.md").write_text(EightDDraftingAgent.to_markdown(pkg.draft_internal, pkg.case), encoding="utf-8")
        (out / f"{pkg.case.case_id}_8D_customer.md").write_text(EightDDraftingAgent.to_markdown(pkg.draft_customer, pkg.case), encoding="utf-8")
        (out / f"{pkg.case.case_id}_read_across.json").write_text(pkg.read_across.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nWritten to {out}")
    return 0


def cmd_approve(args) -> int:
    sup = _supervisor(args)
    svc = sup.workflow.approvals
    if args.decision_type == "validated_root_cause":
        hyp = sup.store.require(Hypothesis, args.object_id)
        svc.validate_hypothesis(hyp, ApprovalRole(args.role), args.approver, args.comment)
        print(f"Hypothesis {hyp.hypothesis_id} VALIDATED by {args.approver} ({args.role})")
        return 0
    obj_version = args.version
    approval = svc.record(args.case_id, args.decision_type, args.object_id, obj_version, ApprovalRole(args.role),
                          args.approver, ApprovalDecision(args.decision), args.comment)
    print(f"Approval {approval.approval_id}: {args.decision_type} {args.decision} by {args.approver}")
    return 0


def cmd_transition(args) -> int:
    from .models import CaseState

    sup = _supervisor(args)
    case = sup.store.require(Case, args.case_id)
    check = sup.workflow.transition_check(case, CaseState(args.target))
    if not check.allowed:
        print("BLOCKED:")
        for b in check.blockers:
            print(f"- {b}")
        return 2
    sup.workflow.transition(case, CaseState(args.target), args.actor)
    print(f"Case {case.case_id} -> {case.state.value}")
    return 0


def cmd_cockpit(args) -> int:
    settings = load_settings(args.config_dir)
    store = Store(args.db)
    svc = CockpitService(store, settings)
    today = date.fromisoformat(args.today) if args.today else None
    paths = svc.export(Path(args.out) if args.out else None, today)
    kpis = svc.executive_kpis(today=today)
    print(json.dumps(kpis, indent=2, default=str))
    for k, p in paths.items():
        print(f"{k}: {p}")
    return 0


def cmd_audit(args) -> int:
    store = Store(args.db)
    for e in store.audit_trail(args.case_id):
        print(f"{e['timestamp']} {e['actor']:<28} {e['action']:<34} {e['object_type']}:{e['object_id']} v{e['object_version']}")
    print("chain valid:", store.verify_audit_chain())
    return 0


def cmd_demo(args) -> int:
    from scripts.demo import run_demo  # type: ignore

    run_demo(Path(args.db), Path(args.out) if args.out else ROOT / "out" / "demo", llm=args.llm)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pqm-agent", description="P3 Rank 6 - 8D / PQM Quality-Escalation Agent")
    p.add_argument("--db", default="out/pqm_agent.db", help="SQLite case store path")
    p.add_argument("--config-dir", default=None)
    p.add_argument("--history", default=str(ROOT / "data" / "history"), help="Historical 8D folder for RAG")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="Create a case from a complaint / PQM record and an evidence folder")
    s.add_argument("record"); s.add_argument("--evidence"); s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("run", help="Run the full agent pipeline for a case")
    s.add_argument("case_id"); s.add_argument("--documents", help="JSON list of controlled document specs")
    s.add_argument("--expected-revisions", help='JSON e.g. {"pfmea":"C","control_plan":"4"}')
    s.add_argument("--today"); s.add_argument("--out"); s.set_defaults(fn=cmd_run)

    s = sub.add_parser("approve", help="Record a named human approval")
    s.add_argument("case_id"); s.add_argument("decision_type"); s.add_argument("object_id")
    s.add_argument("--role", required=True, choices=[r.value for r in ApprovalRole]); s.add_argument("--approver", required=True)
    s.add_argument("--decision", default="approved", choices=[d.value for d in ApprovalDecision])
    s.add_argument("--version", type=int, default=1); s.add_argument("--comment", default=""); s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("transition", help="Request an official case state transition")
    s.add_argument("case_id"); s.add_argument("target"); s.add_argument("--actor", required=True); s.set_defaults(fn=cmd_transition)

    s = sub.add_parser("cockpit", help="Compute KPIs and export CSV / HTML for Power BI")
    s.add_argument("--out"); s.add_argument("--today"); s.set_defaults(fn=cmd_cockpit)

    s = sub.add_parser("audit", help="Print the audit trail and verify the hash chain")
    s.add_argument("--case-id"); s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("demo", help="Run the end-to-end demo on the synthetic BMW EGR-cooler case")
    s.add_argument("--out"); s.add_argument("--llm", default=None, choices=[None, "rules", "anthropic", "auto"]); s.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
