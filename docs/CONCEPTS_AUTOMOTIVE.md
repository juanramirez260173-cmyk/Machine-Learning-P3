# Automotive quality concepts used in this agent (EN / ES)

This glossary explains every domain and AI concept the code relies on, so that a
new team member (data engineer, developer, PMO) can read the modules without a
quality-engineering background. Spanish terms are given where the plant teams use them.

## Quality process

| Concept | Meaning in this project | Where it lives in the code |
|---|---|---|
| **8D (Eight Disciplines)** / *8D* | The structured problem-solving report most OEMs demand from a supplier after a complaint: D1 team, D2 problem description, D3 interim containment, D4 root cause, D5 chosen permanent actions, D6 implementation/validation, D7 prevention of recurrence, D8 closure. | `models.Discipline`, `agents/drafting.py`, `workflow.STATE_DISCIPLINE` |
| **PQM** / *reclamación PQM* | BMW's supplier problem/quality-management ticket. Every 8D maps to one PQM number; the customer due date comes from the PQM. | `Case.pqm_number`, `Case.customer_due_date` |
| **Interim containment (D3)** / *contención* | Immediate protection of the customer: bound the suspect population (lots, serials, production window, locations), sort or 100 % inspect, certify parts, and define exit criteria. Must start within hours (often 24 h). | `agents/containment.py`, `SuspectPopulation`, `config.discipline_deadlines_days.D3` |
| **Occurrence vs escape (non-detection) root cause** | Every complaint has two causes: why the defect was *produced* and why it was *not detected* before shipping. Both must be validated and both need actions (prevention control and detection control). | `models.CauseType`, `workflow.closure_blockers` |
| **Systemic cause** | Why the management system allowed the two causes (e.g. change management, FMEA discipline). Drives D7 lessons learned. | `CauseType.SYSTEMIC`, systemic actions |
| **Ishikawa / 6M** / *espina de pescado* | Cause categories: man, machine, material, method, measurement, environment. | `models.CauseCategory` |
| **5-Why** | Chain of "why" questions from symptom to root cause; stored per hypothesis so reviewers can challenge circular logic. | `Hypothesis.five_why_chain` |
| **Is / Is Not** | D2 technique that states what the problem is and what it is not (what, where, when, how many) to bound the analysis. | `models.IsIsNot`, `agents/d2.py` |
| **5W2H** | Who, what, where, when, why, how, how many - the mandatory D2 facts. | `ProblemDefinition.MANDATORY_FIELDS` |
| **PFMEA** (Process Failure Mode and Effects Analysis) / *AMEF de proceso* | Living risk document per process step: failure mode, effect, severity, cause, occurrence, prevention control, detection control, detection, action priority (AIAG-VDA). A validated root cause **must** appear here with its new controls. | `auditor.PFMEA_COLUMNS`, `ReadAcrossAuditor` (RA-1..RA-3, RA-6) |
| **Control Plan** / *plan de control* | Released document listing, per process step, the characteristic, specification, measurement method, sample size, frequency, control method and reaction plan. The approved action must change the relevant row (e.g. leak-test pressure). | `auditor.CP_COLUMNS`, RA-4 |
| **Work Instruction / standardised work** / *instrucción de trabajo* | Operator-facing document. Operator-facing changes (new check, new key point) must be written here and trained. | `auditor.WI_COLUMNS`, RA-5 |
| **Read-across** / *lecciones aprendidas transversales* | Checking that the lesson from one case is carried to the documents (and to similar products/lines). The agent classifies each item as Present / Missing / Ambiguous / Revision Mismatch. | `models.ReadAcrossStatus`, `ReadAcrossReport` |
| **Revision control / release state** | Only *released* revisions count. Superseded or draft revisions are kept for traceability but never support closure. | `ReleaseState`, `EvidenceAgent.qualify`, `ReadAcrossAuditor._released` |
| **DMC (Data Matrix Code)** | Laser-marked serial identity on the part; the key to genealogy and suspect-population bounding. | `ProductIdentity.dmc_or_serials`, `EvidenceType.GENEALOGY` |
| **Genealogy / traceability** / *trazabilidad* | MES record linking serials to lots, line, shift, time and delivery. Without it containment scope cannot be bounded and the agent says so explicitly. | `SuspectPopulation.genealogy_evidence_ids`, `ContainmentProposal.unbounded_segments` |
| **Effectiveness (D6/D8)** | Objective before/after evidence over an agreed observation period that the action works (e.g. 0 rejects in 3 lots at 2.5 bar). No effectiveness, no closure. | `agents/effectiveness.py`, `Action.effectiveness_*` |
| **Escalation / CS2 / Q-escalation** / *escalación* | OEM escalation ladders (e.g. controlled shipping CS1/CS2, BMW Q-escalation, line-stop risk). Internally: team -> management -> executive, triggered by transparent rules (overdue, missing owner, unreleased document). | `escalation.EscalationEngine`, `config.escalation` |
| **IATF 16949 / VDA 6.3** | The automotive QMS standard and the process-audit standard whose auditors check exactly the evidence links this agent enforces. | Guardrails and audit log |
| **PPM** | Parts per million defective, the standard supplier quality metric. | Containment results, effectiveness metrics |
| **Gauge R&R / detection capability** | Study proving a measurement or test can actually detect the failure mode. Requested as validation plan for escape hypotheses. | `RCAAssistant` escape hypothesis |

## AI / data concepts

| Concept | Meaning here | Code |
|---|---|---|
| **Canonical ID** | Stable identifier per case, evidence, hypothesis, action, document, approval; never rely on file names. | `models.new_id`, `store.Store` |
| **Evidence register** | Catalogue of every source with type, owner, date, revision, hash, validation state and the passage used for citation. | `models.Evidence` |
| **Claim with citation** | A material statement in the 8D that carries the evidence ids supporting it. Facts without a usable citation are flagged *UNSUPPORTED*. | `models.Claim`, `guardrails.UnsupportedClaimDetector` |
| **Structured extraction** | The LLM returns JSON that must validate against a Pydantic schema (Claude structured outputs). Invalid JSON never enters the store. | `llm.AnthropicProvider`, agent `*Extraction` schemas |
| **RAG (retrieval-augmented generation)** | Similar past 8Ds are retrieved *first* and shown to the model/engineer with citations; the model never answers from memory alone. | `retrieval.SimilarCaseService` |
| **BM25 / Sentence-BERT** | Two ways to rank similarity: lexical (BM25, default, no download) and semantic embeddings (Sentence-BERT, optional). | `retrieval.BM25Embedder`, `SentenceTransformerEmbedder` |
| **Scope guard** | Deterministic customer/access filter applied before ranking so cross-customer leakage is impossible. | `guardrails.RetrievalScopeGuard` |
| **Hypothesis state machine** | Hypothesis -> Evidence supported -> Validated (human approval) or Rejected. The model cannot set Validated. | `HypothesisStatus`, `ApprovalService.validate_hypothesis` |
| **Approval gate** | A named person with an allowed role records a decision; the workflow refuses transitions without it. | `workflow.ApprovalService`, `config.approvals` |
| **Hash-chained audit log** | Append-only log where each event hashes the previous one; tampering breaks the chain. | `store.Store.audit`, `verify_audit_chain` |
| **Explainable due-date risk** | P(miss customer due date) from a transparent feature vector; rule weights now, logistic regression once >= 30 governed labels exist. | `ml.DueDateRiskModel` |
| **Deterministic fallback provider** | Every agent has a rule-based path that only copies fields from evidence. Used for tests, golden sets and offline demos. | `llm.RuleBasedProvider` |
