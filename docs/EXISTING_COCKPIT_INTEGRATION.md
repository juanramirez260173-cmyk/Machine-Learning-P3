# Integrating the existing cockpit Python model

The P3 working folder (`06_P3_BMW_8D_PQM quality_escalation agent`, starting asset
`04_P3_BW_IA_Agent_8D_PQM_Auditor`) already contains a cockpit Python model. That folder is on a
local drive and was not reachable from the build environment, so this repository ships its own
cockpit service and defines the contract the existing model can plug into.

## Contract

`pqm_agent.cockpit.CockpitService` exposes three tables that are stable inputs for any cockpit model:

| Method | Grain | Use |
|---|---|---|
| `case_control_rows(today)` | one row per case | case-control view, escalation colouring, closure blockers |
| `executive_kpis(rows, today)` | one dict | executive tiles (open cases, on-time %, at risk, aging, overdue, read-across missing) |
| `agent_quality()` | one dict | agent-quality view (citation coverage, unsupported-claim rate, override rate, cost per case) |

`export()` writes `case_control.csv`, `actions.csv`, `audit_log.csv`, `executive_kpis.json` and
`cockpit.html` to `out/cockpit` (config `cockpit.export_dir`). Point the Power BI model at that folder.

## Two integration options

1. **Keep the existing model as the presentation layer.** Replace its data-loading step with
   `CockpitService(...).case_control_rows()` (or read the exported CSVs). Field names are documented in
   the CSV header and in `cockpit.py`.
2. **Move the existing model's measures into this service.** Add methods next to `executive_kpis`;
   the tests in `tests/test_escalation_and_cockpit.py` show how KPIs are reconciled to the store.

## Checklist when merging the local folder

* Copy the historical 8Ds it holds into `data/history/<customer>/*.json` using the `HistoricalCase`
  fields (see `retrieval.py`) - the RAG is only as good as the digitised history.
* Copy customer PFMEA / Control Plan exports next to the case and register them with a
  `documents_*.json` spec (see `data/samples/case_bmw_egr_leak/`).
* If the existing model has its own escalation thresholds, move them to `config/customers/bmw.yaml`.
