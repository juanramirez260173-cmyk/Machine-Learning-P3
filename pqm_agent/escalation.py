"""PQM Escalation Agent - deterministic aging, due-date proximity and escalation rules.

No black-box scores: every escalation package lists the trigger, impact, decision
required, accountable owner and latest acceptable decision date (Solution Ref 12.3).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Sequence

from .config import Settings
from .models import (Action, ActionStatus, ActionType, Approval, Case, CaseState, ControlledDocument,
                     Escalation, EscalationLevel, ReadAcrossReport, ReleaseState)


class EscalationEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    def evaluate(self, case: Case, actions: Sequence[Action], approvals: Sequence[Approval],
                 reports: Sequence[ReadAcrossReport], documents: Sequence[ControlledDocument],
                 today: Optional[date] = None) -> Escalation:
        today = today or date.today()
        days_to_due = (case.customer_due_date - today).days
        days_open = (today - case.opened_date).days
        warn = int(self.s.get("escalation.warn_days_to_due", 5))
        critical = int(self.s.get("escalation.critical_days_to_due", 2))
        lvl2_overdue = int(self.s.get("escalation.overdue_days_level_2", 3))
        lvl3_overdue = int(self.s.get("escalation.overdue_days_level_3", 7))

        triggers: List[str] = []
        level = EscalationLevel.NONE

        def raise_to(new: EscalationLevel, why: str) -> None:
            nonlocal level
            triggers.append(why)
            if new.value > level.value:
                level = new

        if case.state in (CaseState.CUSTOMER_CLOSURE, CaseState.ARCHIVED):
            return self._package(case, level, ["case closed"], days_to_due, days_open, today)

        # Customer due date proximity / overdue
        if days_to_due < 0:
            overdue = -days_to_due
            if overdue >= lvl3_overdue:
                raise_to(EscalationLevel.EXECUTIVE, f"customer due date overdue by {overdue} days")
            elif overdue >= lvl2_overdue:
                raise_to(EscalationLevel.MANAGEMENT, f"customer due date overdue by {overdue} days")
            else:
                raise_to(EscalationLevel.TEAM, f"customer due date overdue by {overdue} days")
        elif days_to_due <= critical:
            raise_to(EscalationLevel.MANAGEMENT, f"customer due date in {days_to_due} days (critical)")
        elif days_to_due <= warn:
            raise_to(EscalationLevel.TEAM, f"customer due date in {days_to_due} days (warning)")

        # Discipline deadlines
        for disc, due in sorted(case.discipline_due_dates.items()):
            disc_index = int(disc[1:])
            current_index = int(case.current_discipline.value[1:])
            if disc_index >= current_index and due < today:
                raise_to(EscalationLevel.TEAM, f"{disc} deadline {due.isoformat()} passed while still in {case.current_discipline.value}")

        # Critical action overdue
        for a in actions:
            if a.status in (ActionStatus.DONE, ActionStatus.VERIFIED):
                continue
            if a.due_date < today:
                lvl = EscalationLevel.MANAGEMENT if a.is_critical else EscalationLevel.TEAM
                raise_to(lvl, f"{'critical ' if a.is_critical else ''}action {a.action_id} ({a.action_type.value}) overdue since {a.due_date.isoformat()}")
            if a.status == ActionStatus.BLOCKED:
                raise_to(EscalationLevel.TEAM, f"action {a.action_id} blocked: {a.description[:60]}")

        # Containment without effectiveness evidence
        containment = [a for a in actions if a.action_type == ActionType.CONTAINMENT]
        if case.state != CaseState.NEW and containment and not any(a.evidence_ids for a in containment):
            raise_to(EscalationLevel.MANAGEMENT, "containment lacks effectiveness evidence")
        if case.state != CaseState.NEW and not containment:
            raise_to(EscalationLevel.MANAGEMENT, "no containment action defined")

        # Missing decision owner
        if not case.owner or not case.team.get("quality_lead"):
            raise_to(EscalationLevel.TEAM, "required decision owner missing (quality lead)")

        # Closure depends on unreleased document revision
        if reports and not reports[-1].d7_complete:
            unreleased = [d for d in documents if d.release_state == ReleaseState.DRAFT]
            if unreleased:
                raise_to(EscalationLevel.MANAGEMENT,
                         "closure depends on unreleased document revision(s): " + ", ".join(f"{d.document_number} {d.revision}" for d in unreleased))
            else:
                raise_to(EscalationLevel.TEAM, "PFMEA / Control Plan read-across gaps open")

        if days_open >= int(self.s.get("escalation.aging_warn_days", 30)) and level == EscalationLevel.NONE:
            raise_to(EscalationLevel.TEAM, f"case aging {days_open} days")

        return self._package(case, level, triggers or ["no trigger"], days_to_due, days_open, today)

    def _package(self, case: Case, level: EscalationLevel, triggers: List[str], days_to_due: int,
                 days_open: int, today: date) -> Escalation:
        labels = self.s.get("escalation.level_labels", {})
        label = labels.get(level.value, labels.get(str(level.value), str(level.value)))
        owner = {EscalationLevel.NONE: case.owner,
                 EscalationLevel.TEAM: case.team.get("quality_lead", case.owner),
                 EscalationLevel.MANAGEMENT: case.team.get("plant_quality_manager", case.team.get("quality_lead", case.owner)),
                 EscalationLevel.EXECUTIVE: case.team.get("plant_manager", case.team.get("plant_quality_manager", case.owner))}[level]
        decision = {EscalationLevel.NONE: "none - monitor",
                    EscalationLevel.TEAM: "re-plan overdue items and confirm resource for next D-step",
                    EscalationLevel.MANAGEMENT: "management decision on resources, customer extension request or containment scope",
                    EscalationLevel.EXECUTIVE: "executive decision: customer escalation response, line-stop risk, controlled shipping (e.g. CS2) mitigation"}[level]
        latest = min(case.customer_due_date, today + timedelta(days=max(0, min(days_to_due, 2))))
        impact = (f"Escalation level {level.value} ({label}); {days_to_due} days to customer due date; case open {days_open} days; "
                  f"severity {case.severity.value}")
        return Escalation(case_id=case.case_id, level=level, triggers=triggers, impact=impact,
                          decision_required=decision, accountable_owner=owner,
                          latest_acceptable_decision_date=latest, days_to_customer_due=days_to_due, days_open=days_open)
