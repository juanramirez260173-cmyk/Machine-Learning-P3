from datetime import date, timedelta

from conftest import TODAY, doc_specs

from pqm_agent.cockpit import CockpitService
from pqm_agent.escalation import EscalationEngine
from pqm_agent.models import Action, ActionType, Discipline, EscalationLevel


def test_escalation_levels_from_due_date(settings, ingested):
    eng = EscalationEngine(settings)
    case = ingested.case
    containment = Action(case_id=case.case_id, action_type=ActionType.CONTAINMENT, discipline=Discipline.D3,
                         description="sort", owner="J", due_date=case.customer_due_date, evidence_ids=["EV-CONT-01"])
    # far from due date, nothing overdue -> no escalation
    esc = eng.evaluate(case, [containment], [], [], [], today=case.opened_date)
    assert esc.level == EscalationLevel.NONE
    # 1 day before due -> management (critical window)
    esc = eng.evaluate(case, [containment], [], [], [], today=case.customer_due_date - timedelta(days=1))
    assert esc.level.value >= EscalationLevel.MANAGEMENT.value
    # 10 days overdue -> executive
    esc = eng.evaluate(case, [containment], [], [], [], today=case.customer_due_date + timedelta(days=10))
    assert esc.level == EscalationLevel.EXECUTIVE
    assert esc.accountable_owner == case.team["plant_manager"]
    assert esc.triggers and esc.decision_required


def test_cockpit_reconciles_to_store(supervisor, ingested, store, settings):
    supervisor.run_case(ingested.case.case_id, today=TODAY, document_specs=doc_specs("documents_before.json"))
    svc = CockpitService(store, settings)
    rows = svc.case_control_rows(TODAY)
    assert len(rows) == 1 and rows[0]["case_id"] == ingested.case.case_id
    kpis = svc.executive_kpis(rows, TODAY)
    assert kpis["open_cases"] == 1 and kpis["root_cause_not_confirmed"] == 1
    assert kpis["pfmea_cp_confirmation_missing"] == 1
    quality = svc.agent_quality()
    assert quality["agent_runs"] > 0 and quality["processing_cost_usd_per_case"] == 0.0
    html = svc.render_html(rows, kpis, quality)
    assert "Case-control view" in html and ingested.case.case_id in html


def test_risk_model_refuses_to_train_without_labels(settings):
    import pytest

    from pqm_agent.ml import DueDateRiskModel, FEATURES

    m = DueDateRiskModel(settings)
    with pytest.raises(ValueError):
        m.fit([{f: 0 for f in FEATURES}] * 5, [0, 1, 0, 1, 0])
    exp = m.predict({f: 1 for f in FEATURES})
    assert exp.mode == "rules" and 0 <= exp.probability <= 1 and exp.top_drivers


def test_risk_model_trains_with_enough_labels(settings):
    import random

    from pqm_agent.ml import DueDateRiskModel, FEATURES

    random.seed(1)
    X, y = [], []
    for _ in range(60):
        row = {f: random.random() for f in FEATURES}
        row["overdue_actions"] = random.randint(0, 5)
        X.append(row)
        y.append(1 if row["overdue_actions"] >= 3 else 0)
    m = DueDateRiskModel(settings)
    m.fit(X, y)
    hi = m.predict({**X[0], "overdue_actions": 5}).probability
    lo = m.predict({**X[0], "overdue_actions": 0}).probability
    assert hi > lo and m.predict(X[0]).mode == "logistic"
