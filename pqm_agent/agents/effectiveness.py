"""Effectiveness Agent (D6/D8): compares post-action results with the baseline,
detects recurrence or insufficient observation and prepares (never executes) closure."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Sequence

from ..models import Action, ActionStatus, ActionType, Case, Evidence, EvidenceType
from .base import AgentContext, BaseAgent


@dataclass
class EffectivenessAssessment:
    action_id: str
    metric: str
    target: str
    result: str
    meets_target: bool
    observation_sufficient: bool
    recurrence_detected: bool
    note: str = ""


@dataclass
class ClosureRecommendation:
    ready_for_human_closure_review: bool
    assessments: List[EffectivenessAssessment] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)


class EffectivenessAgent(BaseAgent):
    name = "effectiveness_agent"

    @staticmethod
    def _numeric(text: str):
        m = re.search(r"(-?\d+(?:\.\d+)?)", text or "")
        return float(m.group(1)) if m else None

    def assess(self, ctx: AgentContext, actions: Sequence[Action]) -> ClosureRecommendation:
        rec = ClosureRecommendation(ready_for_human_closure_review=True)
        evidence_by_action = {}
        for e in ctx.evidence:
            for aid in e.structured.get("action_ids", []):
                evidence_by_action.setdefault(aid, []).append(e)
        for a in actions:
            if a.action_type == ActionType.CONTAINMENT:
                continue
            evs = evidence_by_action.get(a.action_id, [])
            result = a.effectiveness_result or next((e.structured.get("result") for e in evs if e.structured.get("result")), None)
            observation_ok = any(e.structured.get("observation_period_ok") for e in evs) or bool(a.effectiveness_accepted)
            recurrence = any(e.structured.get("recurrence") for e in evs)
            target_num = self._numeric(a.effectiveness_target or "")
            result_num = self._numeric(result or "")
            meets = (result_num is not None and target_num is not None and result_num <= target_num) or bool(a.effectiveness_accepted)
            if a.status not in (ActionStatus.DONE, ActionStatus.VERIFIED):
                rec.blockers.append(f"{a.action_id}: not implemented ({a.status.value})")
            if result is None:
                rec.blockers.append(f"{a.action_id}: no effectiveness result evidence")
            elif not meets:
                rec.blockers.append(f"{a.action_id}: result '{result}' does not meet target '{a.effectiveness_target}'")
            if not observation_ok:
                rec.blockers.append(f"{a.action_id}: observation period not confirmed")
            if recurrence:
                rec.blockers.append(f"{a.action_id}: recurrence detected after action")
            rec.assessments.append(EffectivenessAssessment(a.action_id, a.effectiveness_metric or "", a.effectiveness_target or "",
                                                           result or "Not available", meets, observation_ok, recurrence))
        rec.ready_for_human_closure_review = not rec.blockers
        self.record_run(ctx, [a.action_id for a in actions], tools=["baseline_comparison", "recurrence_check"])
        return rec
