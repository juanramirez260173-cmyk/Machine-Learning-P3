"""Deterministic guardrails around probabilistic AI.

* UnsupportedClaimDetector - every `fact` claim must cite at least one evidence item
  that exists in the case register and is not rejected/quarantined (FR-04).
* ConclusiveLanguageGuard  - "confirmed root cause" wording is only allowed when a
  VALIDATED hypothesis with a named approval exists for the case (FR-06, FR-09).
* RetrievalScopeGuard      - customer / access-scope filter applied *before* any
  semantic search (zero cross-customer leakage, Solution Ref 7.2 / 12.2).
* WriteBackGuard           - the LLM never writes the official state; every
  structured result passes schema validation + approval check first (8.3 constraint).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, ValidationError

from .config import Settings
from .models import (Approval, ApprovalDecision, Claim, Evidence, EvidenceValidationState,
                     Hypothesis, HypothesisStatus)


@dataclass
class GuardrailResult:
    passed: bool
    findings: List[str] = field(default_factory=list)
    flagged_claim_ids: List[str] = field(default_factory=list)


class UnsupportedClaimDetector:
    def __init__(self, settings: Settings):
        self.require_kinds = set(settings.get("guardrails.require_citation_kinds", ["fact"]))
        self.usable_states = {EvidenceValidationState.VERIFIED, EvidenceValidationState.UNVERIFIED}

    def check(self, claims: Sequence[Claim], evidence: Iterable[Evidence]) -> GuardrailResult:
        register: Dict[str, Evidence] = {e.evidence_id: e for e in evidence}
        result = GuardrailResult(passed=True)
        for claim in claims:
            if claim.kind not in self.require_kinds:
                claim.supported = True
                continue
            usable = [c for c in claim.citations
                      if c.evidence_id in register and register[c.evidence_id].validation_state in self.usable_states]
            if not usable:
                claim.supported = False
                if not claim.citations:
                    claim.flag_reason = "UNSUPPORTED: no evidence citation"
                else:
                    claim.flag_reason = "UNSUPPORTED: citation points to unknown, rejected or quarantined evidence"
                result.passed = False
                result.flagged_claim_ids.append(claim.claim_id)
                result.findings.append(f"{claim.claim_id}: {claim.flag_reason} -> '{claim.text[:80]}'")
            else:
                claim.supported = True
                claim.flag_reason = None
        return result

    @staticmethod
    def citation_coverage(claims: Sequence[Claim]) -> float:
        facts = [c for c in claims if c.kind == "fact"]
        if not facts:
            return 1.0
        return sum(1 for c in facts if c.supported) / len(facts)


class ConclusiveLanguageGuard:
    def __init__(self, settings: Settings):
        phrases = settings.get("guardrails.forbidden_conclusive_phrases", [])
        self._pattern = re.compile("|".join(re.escape(p) for p in phrases), re.IGNORECASE) if phrases else None

    def case_has_validated_cause(self, hypotheses: Iterable[Hypothesis], approvals: Iterable[Approval]) -> bool:
        approved_ids = {a.linked_object_id for a in approvals
                        if a.decision_type == "validated_root_cause" and a.decision == ApprovalDecision.APPROVED}
        return any(h.status == HypothesisStatus.VALIDATED and h.hypothesis_id in approved_ids for h in hypotheses)

    def check(self, claims: Sequence[Claim], hypotheses: Iterable[Hypothesis],
              approvals: Iterable[Approval]) -> GuardrailResult:
        result = GuardrailResult(passed=True)
        if self._pattern is None:
            return result
        allowed = self.case_has_validated_cause(hypotheses, approvals)
        for claim in claims:
            if self._pattern.search(claim.text) and not allowed:
                claim.supported = False
                claim.flag_reason = "BLOCKED: conclusive root-cause language without validated + approved hypothesis"
                result.passed = False
                result.flagged_claim_ids.append(claim.claim_id)
                result.findings.append(f"{claim.claim_id}: {claim.flag_reason}")
        return result

    def sanitize(self, text: str) -> str:
        """Rewrite conclusive wording into hypothesis wording for AI drafts."""
        if self._pattern is None:
            return text
        return self._pattern.sub("root-cause hypothesis (validation pending)", text)


class RetrievalScopeGuard:
    def __init__(self, settings: Settings):
        self.allow_cross_customer = bool(settings.get("retrieval.allow_cross_customer", False))

    def filter(self, documents: Iterable[dict], customer: str, access_scope: str = "customer",
               explicit_cross_customer_approval: bool = False) -> List[dict]:
        cross_ok = self.allow_cross_customer or explicit_cross_customer_approval
        out = []
        for doc in documents:
            doc_customer = doc.get("customer")
            if doc_customer != customer and not cross_ok:
                continue
            if doc.get("access_scope", "customer") not in (access_scope, "public"):
                continue
            out.append(doc)
        return out


class WriteBackGuard:
    """Schema-validating write-back. Raises on any invalid or unapproved official write."""

    OFFICIAL_DECISIONS = {"validated_root_cause", "closure", "customer_submission",
                          "pfmea_change", "control_plan_change", "work_instruction_change"}

    def __init__(self, settings: Settings):
        self.settings = settings

    def validate(self, model_cls: type[BaseModel], payload: dict) -> BaseModel:
        try:
            return model_cls.model_validate(payload)
        except ValidationError as exc:  # fail loud
            raise ValueError(f"Write-back rejected by schema {model_cls.__name__}: {exc}") from exc

    def require_approval(self, decision_type: str, object_id: str, approvals: Iterable[Approval]) -> Approval:
        allowed_roles = set(self.settings.get(f"approvals.{decision_type}", []))
        for a in approvals:
            if (a.decision_type == decision_type and a.linked_object_id == object_id
                    and a.decision == ApprovalDecision.APPROVED and a.role.value in allowed_roles):
                return a
        raise PermissionError(
            f"Official decision '{decision_type}' on {object_id} requires named human approval by one of {sorted(allowed_roles)}"
        )
