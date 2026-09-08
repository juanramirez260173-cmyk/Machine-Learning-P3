"""Cockpit / Reporting Service: executive, case-control and agent-quality views.

Outputs are plain tables (list of dicts) so they can be:
* exported as CSV for the Power BI cockpit (tracker tool choice),
* rendered as a self-contained HTML page (no external dependencies), or
* consumed by the existing cockpit Python model in the P3 working folder
  (see docs/EXISTING_COCKPIT_INTEGRATION.md).
"""
from __future__ import annotations

import csv
import html
import json
import statistics
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Settings
from .escalation import EscalationEngine
from .models import (Action, ActionStatus, ActionType, AgentRun, Approval, Case, CaseState, ControlledDocument,
                     EightDDraft, Evidence, EvidenceValidationState, Hypothesis, HypothesisStatus, ProblemDefinition,
                     ReadAcrossReport)
from .store import Store
from .workflow import WorkflowService


class CockpitService:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.workflow = WorkflowService(store, settings)
        self.escalation = EscalationEngine(settings)

    # ------------------------------------------------------------ case rows
    def case_control_rows(self, today: Optional[date] = None) -> List[Dict[str, Any]]:
        today = today or date.today()
        rows = []
        for case in self.store.list(Case):
            cid = case.case_id
            actions = self.store.list(Action, case_id=cid)
            approvals = self.store.list(Approval, case_id=cid)
            hyps = self.store.list(Hypothesis, case_id=cid)
            reports = self.store.list(ReadAcrossReport, case_id=cid)
            docs = self.store.list(ControlledDocument, case_id=cid)
            evidence = self.store.list(Evidence, case_id=cid)
            d2 = self.store.list(ProblemDefinition, case_id=cid)
            drafts = self.store.list(EightDDraft, case_id=cid)
            esc = self.escalation.evaluate(case, actions, approvals, reports, docs, today)
            closed = case.state in (CaseState.CUSTOMER_CLOSURE, CaseState.ARCHIVED)
            on_time = (case.closure_date <= case.customer_due_date) if (closed and case.closure_date) else None
            containment = [a for a in actions if a.action_type == ActionType.CONTAINMENT]
            corrective = [a for a in actions if a.action_type != ActionType.CONTAINMENT]
            report = reports[-1] if reports else None
            gaps = [f.check_id for f in report.findings if f.blocks_closure] if report else []
            rows.append({
                "case_id": cid, "pqm_number": case.pqm_number, "eight_d_number": case.eight_d_number,
                "customer": case.customer, "plant": case.plant, "part_number": case.product.part_number,
                "failure_mode": case.failure.failure_mode, "severity": case.severity.value,
                "state": case.state.value, "current_discipline": case.current_discipline.value, "owner": case.owner,
                "opened_date": case.opened_date.isoformat(), "customer_due_date": case.customer_due_date.isoformat(),
                "days_open": (today - case.opened_date).days, "days_to_due": (case.customer_due_date - today).days,
                "next_milestone": self._next_milestone(case, today),
                "closed": closed, "on_time": on_time,
                "evidence_items": len(evidence),
                "evidence_quarantined": sum(1 for e in evidence if e.validation_state == EvidenceValidationState.QUARANTINED),
                "d2_completeness": round(d2[-1].completeness(), 2) if d2 else 0.0,
                "missing_information": len(d2[-1].missing_information) if d2 else None,
                "containment_status": "approved" if any(a.decision_type == "containment" for a in approvals)
                else ("proposed" if containment else "none"),
                "containment_overdue": any(a.due_date < today and a.status not in (ActionStatus.DONE, ActionStatus.VERIFIED) for a in containment),
                "root_cause_status": "validated" if any(h.status == HypothesisStatus.VALIDATED for h in hyps)
                else ("evidence_supported" if any(h.status == HypothesisStatus.EVIDENCE_SUPPORTED for h in hyps) else "hypothesis"),
                "corrective_actions": len(corrective),
                "corrective_overdue": sum(1 for a in corrective if a.due_date < today and a.status not in (ActionStatus.DONE, ActionStatus.VERIFIED)),
                "effectiveness_accepted": all(a.effectiveness_accepted for a in corrective) if corrective else False,
                "pfmea_cp_status": ("complete" if report and report.d7_complete else ("gaps: " + ", ".join(gaps) if report else "not checked")),
                "read_across_gaps": len(gaps),
                "approvals": len([a for a in approvals if a.decision.value == "approved"]),
                "escalation_level": esc.level.value, "escalation_triggers": "; ".join(esc.triggers),
                "escalation_owner": esc.accountable_owner, "latest_decision_date": esc.latest_acceptable_decision_date.isoformat(),
                "closure_blockers": len(self.workflow.closure_blockers(case, today)) if not closed else 0,
                "draft_label": drafts[-1].label.value if drafts else "none",
                "citation_coverage": drafts[-1].citation_coverage if drafts else None,
                "unsupported_claims": drafts[-1].unsupported_claim_count if drafts else None,
            })
        return rows

    @staticmethod
    def _next_milestone(case: Case, today: date) -> str:
        current = int(case.current_discipline.value[1:])
        upcoming = sorted((d, due) for d, due in case.discipline_due_dates.items() if int(d[1:]) >= current)
        return f"{upcoming[0][0]} due {upcoming[0][1].isoformat()}" if upcoming else f"customer due {case.customer_due_date.isoformat()}"

    # -------------------------------------------------------- executive view
    def executive_kpis(self, rows: Optional[Sequence[Dict[str, Any]]] = None, today: Optional[date] = None) -> Dict[str, Any]:
        rows = rows if rows is not None else self.case_control_rows(today)
        open_rows = [r for r in rows if not r["closed"]]
        closed_rows = [r for r in rows if r["closed"] and r["on_time"] is not None]
        on_time = (sum(1 for r in closed_rows if r["on_time"]) / len(closed_rows)) if closed_rows else None
        ages = [r["days_open"] for r in open_rows]
        by_sev: Dict[str, int] = {}
        by_cust: Dict[str, int] = {}
        for r in open_rows:
            by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
            by_cust[r["customer"]] = by_cust.get(r["customer"], 0) + 1
        return {
            "open_cases": len(open_rows),
            "open_by_severity": by_sev, "open_by_customer": by_cust,
            "on_time_closure_rate": round(on_time, 3) if on_time is not None else None,
            "on_time_target": self.settings.get("cockpit.on_time_target", 0.75),
            "on_time_baseline": self.settings.get("cockpit.on_time_baseline", 0.53),
            "cases_at_risk": sum(1 for r in open_rows if r["escalation_level"] >= 1),
            "cases_escalated_management_or_above": sum(1 for r in open_rows if r["escalation_level"] >= 2),
            "aging_avg_days": round(statistics.mean(ages), 1) if ages else 0,
            "aging_median_days": statistics.median(ages) if ages else 0,
            "containment_overdue": sum(1 for r in open_rows if r["containment_overdue"]),
            "root_cause_not_confirmed": sum(1 for r in open_rows if r["root_cause_status"] != "validated"),
            "corrective_action_overdue": sum(r["corrective_overdue"] for r in open_rows),
            "pfmea_cp_confirmation_missing": sum(1 for r in open_rows if r["read_across_gaps"] > 0 or r["pfmea_cp_status"] == "not checked"),
            "awaiting_customer_closure": sum(1 for r in rows if r["state"] == CaseState.CUSTOMER_CLOSURE.value),
            "cases_with_owner_and_due_date": sum(1 for r in open_rows if r["owner"] and r["customer_due_date"]),
        }

    # ------------------------------------------------------- agent-quality view
    def agent_quality(self) -> Dict[str, Any]:
        runs = self.store.list(AgentRun)
        drafts = self.store.list(EightDDraft)
        approvals = self.store.list(Approval)
        cases = self.store.list(Case)
        modified = sum(1 for a in approvals if a.decision.value == "modify")
        approved = sum(1 for a in approvals if a.decision.value == "approved")
        rejected = sum(1 for a in approvals if a.decision.value == "rejected")
        unsupported = sum(d.unsupported_claim_count for d in drafts)
        claims = sum(len(s.claims) for d in drafts for s in d.disciplines)
        return {
            "agent_runs": len(runs), "runs_with_error": sum(1 for r in runs if r.error),
            "drafts_generated": len(drafts),
            "decisions_approved_without_modification": approved, "decisions_with_modification": modified,
            "decisions_rejected": rejected,
            "user_override_rate": round((modified + rejected) / (approved + modified + rejected), 3) if approvals else None,
            "unsupported_claim_rate": round(unsupported / claims, 3) if claims else 0.0,
            "citation_coverage_avg": round(statistics.mean(d.citation_coverage for d in drafts), 3) if drafts else None,
            "blocked_unsafe_outputs": unsupported,
            "processing_cost_usd_per_case": round(sum(r.cost_usd for r in runs) / len(cases), 4) if cases else 0.0,
            "avg_latency_ms": round(statistics.mean(r.latency_ms for r in runs), 1) if runs else 0,
            "models_used": sorted({r.model for r in runs}),
        }

    # ----------------------------------------------------------------- export
    def export(self, out_dir: Optional[Path] = None, today: Optional[date] = None) -> Dict[str, Path]:
        out = Path(out_dir or self.settings.get("cockpit.export_dir", "out/cockpit"))
        out.mkdir(parents=True, exist_ok=True)
        rows = self.case_control_rows(today)
        kpis = self.executive_kpis(rows, today)
        quality = self.agent_quality()
        actions = [a.model_dump(mode="json") for a in self.store.list(Action)]
        paths = {}
        paths["cases_csv"] = self._csv(out / "case_control.csv", rows)
        paths["actions_csv"] = self._csv(out / "actions.csv", actions)
        paths["kpis_json"] = out / "executive_kpis.json"
        paths["kpis_json"].write_text(json.dumps({"executive": kpis, "agent_quality": quality}, indent=2, default=str), encoding="utf-8")
        paths["audit_csv"] = self._csv(out / "audit_log.csv", self.store.audit_trail())
        paths["html"] = out / "cockpit.html"
        paths["html"].write_text(self.render_html(rows, kpis, quality), encoding="utf-8")
        return paths

    @staticmethod
    def _csv(path: Path, rows: Sequence[Dict[str, Any]]) -> Path:
        with path.open("w", encoding="utf-8", newline="") as fh:
            if not rows:
                fh.write("")
                return path
            keys = sorted({k for r in rows for k in r.keys()}, key=lambda k: list(rows[0].keys()).index(k) if k in rows[0] else 999)
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        return path

    # ----------------------------------------------------------------- HTML
    def render_html(self, rows: Sequence[Dict[str, Any]], kpis: Dict[str, Any], quality: Dict[str, Any]) -> str:
        def tile(label: str, value: Any, sub: str = "") -> str:
            return f"<div class='tile'><div class='v'>{html.escape(str(value))}</div><div class='l'>{html.escape(label)}</div><div class='s'>{html.escape(sub)}</div></div>"

        ontime = kpis["on_time_closure_rate"]
        tiles = [
            tile("Open cases", kpis["open_cases"], ", ".join(f"{k}: {v}" for k, v in kpis["open_by_severity"].items())),
            tile("8D on-time", f"{ontime:.0%}" if ontime is not None else "n/a", f"target {kpis['on_time_target']:.0%} / baseline {kpis['on_time_baseline']:.0%}"),
            tile("Cases at risk", kpis["cases_at_risk"], f"{kpis['cases_escalated_management_or_above']} at management level or above"),
            tile("Aging (avg / median days)", f"{kpis['aging_avg_days']} / {kpis['aging_median_days']}"),
            tile("Containment overdue", kpis["containment_overdue"]),
            tile("Root cause not confirmed", kpis["root_cause_not_confirmed"]),
            tile("Corrective actions overdue", kpis["corrective_action_overdue"]),
            tile("PFMEA / CP confirmation missing", kpis["pfmea_cp_confirmation_missing"]),
            tile("Awaiting customer closure", kpis["awaiting_customer_closure"]),
        ]
        qtiles = [tile("Citation coverage", f"{quality['citation_coverage_avg']:.0%}" if quality["citation_coverage_avg"] is not None else "n/a", "target >= 95 %"),
                  tile("Unsupported-claim rate", f"{quality['unsupported_claim_rate']:.1%}"),
                  tile("Blocked unsafe outputs", quality["blocked_unsafe_outputs"]),
                  tile("User override rate", f"{quality['user_override_rate']:.0%}" if quality["user_override_rate"] is not None else "n/a"),
                  tile("Cost per case (USD)", quality["processing_cost_usd_per_case"]),
                  tile("Agent runs", quality["agent_runs"], f"{quality['runs_with_error']} with error")]
        cols = ["case_id", "customer", "part_number", "failure_mode", "severity", "state", "current_discipline", "owner",
                "days_open", "days_to_due", "next_milestone", "d2_completeness", "containment_status", "root_cause_status",
                "corrective_overdue", "pfmea_cp_status", "escalation_level", "escalation_triggers", "closure_blockers", "citation_coverage"]
        thead = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        body = ""
        for r in rows:
            cls = f"lvl{r['escalation_level']}"
            body += f"<tr class='{cls}'>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>"
        return f"""<!doctype html><html><head><meta charset='utf-8'><title>8D / PQM Cockpit</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}} h1{{margin:0 0 4px}} .sub{{color:#666;margin-bottom:16px}}
.tiles{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}} .tile{{border:1px solid #ddd;border-radius:8px;padding:12px 16px;min-width:160px;background:#fafafa}}
.tile .v{{font-size:26px;font-weight:600}} .tile .l{{font-size:12px;color:#555;margin-top:2px}} .tile .s{{font-size:11px;color:#888}}
table{{border-collapse:collapse;width:100%;font-size:12px}} th,td{{border:1px solid #e3e3e3;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f0f0f0;position:sticky;top:0}} tr.lvl1 td{{background:#fff8e1}} tr.lvl2 td{{background:#ffe0b2}} tr.lvl3 td{{background:#ffcdd2}}
.note{{font-size:12px;color:#666;margin-top:16px}}</style></head><body>
<h1>8D / PQM Quality-Escalation Cockpit</h1><div class='sub'>P3 Rank 6 - executive, case-control and agent-quality views. Every official decision requires a named human approval.</div>
<h2>Executive view</h2><div class='tiles'>{''.join(tiles)}</div>
<h2>Agent-quality view</h2><div class='tiles'>{''.join(qtiles)}</div>
<h2>Case-control view</h2><div style='overflow-x:auto'><table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table></div>
<div class='note'>Row colour = escalation level (1 team, 2 management, 3 executive). KPIs reconcile to the controlled case store; export CSVs feed the Power BI model.</div>
</body></html>"""
