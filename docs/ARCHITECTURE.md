# Architecture

```
                 complaint / PQM record + evidence folder + controlled documents
                                          |
                                          v
 +------------------- QualityEscalationSupervisor (orchestrator.py) -------------------+
 |                                                                                     |
 |  CaseIntakeAgent -> EvidenceAgent -> D2ProblemDefinitionAgent -> SimilarCaseService  |
 |        |                 |                    |                      (RAG, scoped)   |
 |        v                 v                    v                            |         |
 |  RCAAssistant  <---------------------------------------------------------- +         |
 |        |                                                                             |
 |  ContainmentAgent -> CorrectiveActionAgent -> ReadAcrossAuditor -> EffectivenessAgent |
 |                                                   |                                  |
 |  EscalationEngine (rules) -> DueDateRiskModel -> EightDDraftingAgent (internal/customer)
 +-------------------------------------------------------------------------------------+
                                          |
     deterministic services: guardrails.py (claims, language, scope, write-back)
                             workflow.py (state machine + named approvals)
                             store.py (SQLite JSON store, versions, hash-chained audit)
                                          |
                                          v
                cockpit.py -> CSV (Power BI) / HTML / Streamlit  +  audit trail
```

## Layers (mapped to Solution Reference 8.1)

| Layer | Module(s) |
|---|---|
| Experience | `cli.py`, `cockpit.py` (HTML), `cockpit_app.py` (Streamlit), CSV for Power BI |
| Workflow | `workflow.py` (case state machine, approvals, closure blockers), `escalation.py` |
| Agent orchestration | `orchestrator.py` supervisor + `agents/*` |
| Deterministic services | `guardrails.py`, `auditor.py`, `models.py` (schema), `config.py` |
| Knowledge services | `retrieval.py` (scoped RAG, BM25 / Sentence-BERT) |
| Data services | `store.py` (entities, versions, audit log) |
| Integration | evidence folder + JSON metadata; Excel/CSV loaders for PFMEA, CP, WI (`auditor.load_controlled_document`) |
| Platform controls | Config-driven roles, allow-listed history folders, `PQM_AGENT_LLM` switch, no LLM write-back |

## Mandatory architecture constraint

The LLM never writes to the official case record. `AnthropicProvider.structured` returns a
validated Pydantic object; the agent turns it into entities; `Store.put` records a version and
an audit event; official state changes go through `WorkflowService.transition`, which refuses
without approvals from `config.approvals`.

## Reference processing flow (Solution Reference 8.2)

1. Ingest source item, keep original link, hash and metadata (`CaseIntakeAgent.load_evidence_folder`).
2. Resolve case scope, identifiers, document type, revision and release state (`Case`, `Evidence`, `EvidenceAgent.qualify`).
3. Extract text/tables/structured fields; quarantine low-confidence items (`extraction_confidence < guardrails.min_extraction_confidence`).
4. Index approved content in the case-scoped retrieval (`SimilarCaseService`, scope guard first).
5. Invoke deterministic tools and specialised agents against the controlled source set (`run_case`).
6. Generate a draft with evidence links and explicit uncertainty labels (`Claim.kind`, `DraftLabel`).
7. Route to the required approval role (`approvals_required` in the case package).
8. Write approved structured results through the schema-validating service (`WriteBackGuard`, `Store.put`).
9. Record source, version, approval and agent-run history (`AgentRun`, `AuditEvent`).

## LLM configuration

* Model `claude-opus-5` (config `llm.model`), adaptive thinking, effort `medium`, structured JSON outputs via
  `client.beta.messages.parse(output_format=<schema>)`, prompt caching on the stable system prompt,
  server-side refusal fallbacks (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
* `PQM_AGENT_LLM=rules|anthropic|auto` selects the provider; `auto` uses Claude only when
  `ANTHROPIC_API_KEY` (or an `ant auth login` profile via `ANTHROPIC_AUTH_TOKEN`) is present.
* Prompts are versioned files in `pqm_agent/prompts/` (`llm.prompt_version`).

## Extension points

* `Store` -> PostgreSQL / SQL Server: same `put/get/list/audit` interface.
* `SentenceTransformerEmbedder` -> any embedding service; keep `fit/scores`.
* `load_controlled_document` -> native PLM / FMEA tool connector returning the same row dicts.
* `DueDateRiskModel.fit/predict` -> LightGBM + SHAP once governed labels exist.
* `config/customers/<customer>.yaml` -> per-customer deadlines, templates, terminology, roles.
