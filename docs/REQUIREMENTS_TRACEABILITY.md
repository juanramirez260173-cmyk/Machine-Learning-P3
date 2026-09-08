# Requirements traceability (Technical Reference FR-01..FR-12 and Solution Reference capabilities)

| ID | Requirement | Implementation | Verified by |
|---|---|---|---|
| FR-01 | Unique case ID and source lineage | `Case.case_id`, `Case.source_lineage`, `Evidence.content_hash` | `tests/test_schema.py` |
| FR-02 | Structured D2 facts (5W2H, Is/Is Not) | `agents/d2.py`, `ProblemDefinition` | demo (`D2 completeness 100%`), `test_workflow` |
| FR-03 | Evidence register with source, date, owner, link, validation state | `Evidence`, `EvidenceAgent.qualify` (duplicates, superseded, quarantine) | demo findings, `test_guardrails` |
| FR-04 | Evidence-grounded draft, unsupported claims flagged | `Claim`, `UnsupportedClaimDetector`, `EightDDraftingAgent` | `test_guardrails::test_seeded_unsupported_claims_are_flagged`, `test_end_to_end` |
| FR-05 | Similar past cases with citations and similarity factors | `retrieval.SimilarCaseService` (`matched_factors`, `explanation`, `source_path`) | `test_retrieval` |
| FR-06 | Hypothesis -> Evidence Supported -> Validated | `HypothesisStatus`, `ApprovalService.validate_hypothesis` | `test_workflow::test_hypothesis_validation_requires_named_engineer` |
| FR-07 | Containment scope, effectiveness, exit criteria | `agents/containment.py`, `SuspectPopulation`, `Action.exit_criteria` | demo, escalation rule "containment lacks effectiveness evidence" |
| FR-08 | Compare validated cause / actions against PFMEA, CP, WI | `auditor.ReadAcrossAuditor`, `diff_revisions` | `test_auditor` |
| FR-09 | Named human approval for official decisions | `ApprovalService`, `WorkflowService.transition_check`, `WriteBackGuard` | `test_workflow`, `test_guardrails::test_write_back_requires_named_approval` |
| FR-10 | Cockpit KPIs, aging, open evidence, repeats, high-risk | `cockpit.CockpitService` (executive, case-control, agent-quality) | `test_escalation_and_cockpit` |
| FR-11 | Audit log of prompts / tool calls / approvals / versions | `AgentRun`, `Store.audit`, `entity_versions`, `verify_audit_chain` | `test_workflow::test_state_machine_enforces_gates_and_versions` |
| FR-12 | Due-date risk with explainable features (later) | `ml.DueDateRiskModel` (rules now, logistic once labelled) | `test_escalation_and_cockpit::test_risk_model_*` |

## Guardrail table (Technical Reference "Human approval, governance and guardrails")

| Decision | AI role in code | Human authority enforced by |
|---|---|---|
| Root-cause hypothesis | `RCAAssistant.run` creates hypotheses | none needed |
| Validated root cause | presents evidence / contradictions | `approvals.validated_root_cause` (process_engineer, quality_lead) |
| Containment / disposition | `ContainmentAgent` proposes | `approvals.containment` |
| PFMEA / CP / WI change | `ReadAcrossAuditor` proposes change text | `approvals.pfmea_change`, `control_plan_change`, `work_instruction_change` |
| 8D closure | `closure_blockers` checks | `approvals.closure`, transition to `customer_closure` |
| External customer response | `draft_customer` variant only | `approvals.customer_submission` |
| Shipment / release | no code path exists | existing release process |

## Read-across checklist (Technical Reference "PFMEA / Control Plan / WI read-across")

RA-1..RA-6 in `config/default.yaml` map one-to-one to the six numbered items; `d7_complete` is
false when any item is Missing or Revision Mismatch (closure blocker rule); Ambiguous items go to
manual review and are listed in the D7 draft.

## KPI coverage (pilot targets)

| KPI | Where measured |
|---|---|
| Evidence traceability >= 95 % | `EightDDraft.citation_coverage` (agent-quality view) |
| D2 completeness >= 95 % | `ProblemDefinition.completeness()` (case-control view) |
| Unsupported-claim detection >= 95 % | golden test `test_seeded_unsupported_claims_are_flagged` |
| Similar-case usefulness >= 80 % top-5 | SME rating to be recorded against `RetrievalHit` output (G2) |
| PFMEA/CP read-across coverage 100 % | `pfmea_cp_status` per case |
| Human approval on official decisions 100 % | audit log + `WriteBackGuard` |
| 8D draft preparation time -60 % | time study at G3 (agent run latency recorded in `AgentRun`) |
| On-time closure trend -> 75 % | `executive_kpis.on_time_closure_rate` vs target/baseline |
