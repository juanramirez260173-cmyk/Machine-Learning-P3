# P3 Rank 6 - 8D / PQM Quality-Escalation Agent

Human-controlled AI agent for automotive complaint handling (IATF 16949 / VDA 6.3): it assembles
controlled evidence, drafts and challenges the 8D, retrieves similar past cases, proposes containment
and escalation, verifies with document diffs that the PFMEA, Control Plan and Work Instruction were
*actually* updated, and publishes the 8D / PQM cockpit with an auditable log of every decision.

> **Core design principle** - AI may propose, summarise, compare and challenge. It must not declare a
> root cause proven, close an 8D, change a controlled quality document, or communicate an official
> customer position without named human approval. The workflow enforces this; prompt wording cannot bypass it.

Reference documents (copied to `docs/reference/`): Solution Reference, Technical Reference and the
ES/EN project-definition one-pager. New to 8D, PFMEA, PQM or RAG? Start with
[`docs/CONCEPTS_AUTOMOTIVE.md`](docs/CONCEPTS_AUTOMOTIVE.md).

## What it does (end-to-end loop)

| Step | Agent / service | Output | Human control |
|---|---|---|---|
| 1 Intake | `CaseIntakeAgent` | canonical case, discipline due dates, missing-information questions | quality lead accepts scope |
| 2 Evidence | `EvidenceAgent` | evidence register: hash, revision, duplicates, superseded, quarantine | 8D owner approves evidence set |
| 3 D2 | `D2ProblemDefinitionAgent` | 5W2H + Is/Is Not, facts vs assumptions, each fact cited | 8D owner approves problem statement |
| 4 Similar cases | `SimilarCaseService` (RAG) | top-k past 8Ds with citations and matched factors, customer-scoped | engineer compares |
| 5 D4 | `RCAAssistant` | occurrence / escape / systemic hypotheses, 5-Why, validation plans | engineer validates with evidence -> named approval |
| 6 D3 | `ContainmentAgent` | suspect population, sorting logic, exit criteria, unbounded segments | quality / operations approve |
| 7 D5 | `CorrectiveActionAgent` | occurrence, detection, systemic actions with owners, evidence, metrics | responsible function approves |
| 8 D7 | `ReadAcrossAuditor` | RA-1..RA-6 Present / Missing / Ambiguous / Revision Mismatch, revision diff, closure block | document owners release revisions |
| 9 D6 | `EffectivenessAgent` | result vs target, observation period, recurrence, closure blockers | 8D owner / customer quality accept |
| 10 Escalation | `EscalationEngine` | level 0-3 with trigger, impact, decision, owner, latest decision date | management decides |
| 11 Risk | `DueDateRiskModel` | explainable P(miss customer due date) | monitoring |
| 12 Draft | `EightDDraftingAgent` | D1-D8 internal + customer variants, citation coverage, unsupported-claim flags | quality lead approves, customer lead submits |
| 13 Cockpit | `CockpitService` | executive / case-control / agent-quality views, CSV for Power BI, HTML | management monitors |

## Quick start

```bash
pip install -e ".[dev]"            # core: pydantic, PyYAML, numpy, openpyxl (+ anthropic, pytest)
PQM_AGENT_LLM=rules pytest -q      # 24 tests: schema, guardrails, RAG leakage, document diff, workflow, cockpit, e2e
python scripts/demo.py             # full pilot journey on the synthetic BMW EGR-cooler weld-leak case
```

The demo writes `out/demo/`: internal and customer 8D drafts (Markdown), the read-across report, the
PFMEA B->C diff, similar cases, and `cockpit/` (CSV + `cockpit.html`).

### With Claude

```bash
export ANTHROPIC_API_KEY=...       # or `ant auth login`
PQM_AGENT_LLM=anthropic python scripts/demo.py
```

Model and effort are in `config/default.yaml` (`llm.*`; default `claude-opus-5`, adaptive thinking,
structured JSON outputs, server-side refusal fallbacks). Without credentials the deterministic
rule-based provider runs the same pipeline - it only copies fields that exist in the evidence.

### CLI

```bash
pqm-agent ingest data/samples/case_bmw_egr_leak/case.json --evidence data/samples/case_bmw_egr_leak/evidence
pqm-agent run CASE-2026-0001 --documents data/samples/case_bmw_egr_leak/documents_before.json --today 2026-09-12 --out out/run
pqm-agent approve CASE-2026-0001 validated_root_cause HYP-XXXX --role process_engineer --approver "A. Kumar"
pqm-agent transition CASE-2026-0001 containment_active --actor "S. Weber"
pqm-agent cockpit --out out/cockpit --today 2026-09-12
pqm-agent audit --case-id CASE-2026-0001
streamlit run pqm_agent/cockpit_app.py -- --db out/pqm_agent.db     # optional interactive cockpit
```

## Repository layout

```
pqm_agent/
  models.py        canonical, versioned data model (Case, Evidence, Hypothesis, Action, ControlledDocument, Approval, AgentRun, AuditEvent, EightDDraft)
  store.py         SQLite JSON store with entity versions and a hash-chained audit log
  workflow.py      case state machine, approval service, closure blockers
  guardrails.py    unsupported-claim detector, conclusive-language guard, retrieval scope guard, write-back guard
  llm.py           Claude provider (structured outputs) + deterministic rule provider; AgentRun tracing
  retrieval.py     customer-scoped similar-case RAG (BM25 default, Sentence-BERT optional)
  auditor.py       PFMEA / Control Plan / WI loaders (Excel, CSV), revision diff, RA-1..RA-6 read-across
  escalation.py    deterministic aging / due-date / blocker escalation rules
  ml.py            explainable due-date risk (rules now, logistic regression after governed labels)
  cockpit.py       KPIs, CSV export for Power BI, self-contained HTML
  orchestrator.py  supervisor running the agents against the controlled source set
  agents/          intake, evidence, d2, rca, containment, corrective, effectiveness, drafting
  prompts/         versioned prompt files
config/            default.yaml + customers/bmw.yaml (deadlines, roles, thresholds, templates, terminology)
data/samples/      synthetic pilot case: PQM record, evidence metadata, PFMEA B/C, CP 3/4, WI 2/3
data/history/      synthetic historical 8Ds per customer for the RAG index
scripts/demo.py    end-to-end pilot journey
tests/             golden-set style tests per validation layer
docs/              ARCHITECTURE, CONCEPTS_AUTOMOTIVE, REQUIREMENTS_TRACEABILITY, EXISTING_COCKPIT_INTEGRATION, reference/
```

## How the guardrails work

* **Evidence before narrative.** Every `fact` claim must cite an evidence id that exists and is not
  rejected / quarantined / superseded; otherwise it is flagged `UNSUPPORTED` and excluded from the
  customer variant. Coverage is a cockpit KPI (target >= 95 %).
* **No self-validated root cause.** Hypotheses move Hypothesis -> Evidence supported automatically, but
  only `ApprovalService.validate_hypothesis` with an allowed role and a named approver sets Validated.
  Conclusive wording ("root cause is confirmed") is rewritten to hypothesis wording until then.
* **Closure blockers are deterministic.** D2 approved, both occurrence and escape causes validated,
  containment approved, actions implemented with accepted effectiveness, read-across complete
  (no Missing / Revision Mismatch), closure approval. The auditor never sets "ready to close".
* **Zero cross-customer leakage.** The scope filter runs before ranking; the test suite asserts the
  textually closest other-customer case is never returned.
* **Fail loud.** Missing mandatory identifiers raise; low-confidence extractions are quarantined for
  manual review; unknown revisions produce Revision Mismatch rather than silent normalisation.
* **Immutable trail.** Every entity write stores a version; every action appends a hash-chained audit
  event (`pqm-agent audit` verifies the chain); every model call records model, prompt version, tokens,
  latency and cost.

## Data contracts

* **Case record** - `data/samples/case_bmw_egr_leak/case.json` (fields in Appendix B of the Solution Reference).
* **Evidence metadata** - one JSON per item in the evidence folder: `evidence_type`, `title`,
  `source_path`, `owner`, `created_date`, optional `revision`, `release_state`, `extraction_confidence`,
  `excerpt`, `structured`. `structured.candidate_causes` feeds the rule-based RCA path.
* **Controlled documents** - Excel or CSV exports of PFMEA / Control Plan / WI with the usual AIAG-VDA
  headers (aliases in `auditor.py`), registered through a `documents_*.json` spec with revision, release
  state, owner and effective date.
* **Historical 8Ds** - `data/history/<customer>/*.json` with the `HistoricalCase` fields.

## Configuration over code

Customer-specific deadlines, escalation thresholds, approval roles, the read-across checklist, D2
mandatory fields, evidence requirements per discipline, 8D template titles and terminology are YAML
(`config/`). `config/customers/bmw.yaml` shows an override set; values are illustrative until G1.

## Gate mapping

| Gate | This repository provides |
|---|---|
| G1 Technical review | architecture, canonical model, guardrails, golden-set tests, backlog traceability (`docs/`) |
| G2 Pilot readiness | end-to-end pipeline on controlled cases (`scripts/demo.py`), baseline KPIs in cockpit |
| G3 Production trial | approval workflow, audit log, override / correction capture, CLI + Streamlit for key users |
| G4 Release | RBAC roles and thresholds externalised, CI (`.github/workflows/ci.yml`), export for Power BI |
| G5 Productisation | per-customer YAML, pluggable store / embedder / document connectors, reusable evaluation pack |

## Known limits of the pilot build

* Read-across matching in rules mode is lexical (token containment + sequence ratio). Ambiguous items
  are routed to manual review; the Claude provider adds semantic comparison but the SME decision remains.
* The Sentence-BERT embedder is optional (`pip install -e ".[embeddings]"`); BM25 is the default.
* The due-date risk model refuses to train below 30 governed labels and reports rule-based scores until then.
* The local P3 working folder with the existing cockpit model was not accessible from the build
  environment; `docs/EXISTING_COCKPIT_INTEGRATION.md` defines the contract to plug it in.
