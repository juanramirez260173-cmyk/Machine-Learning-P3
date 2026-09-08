"""Later ML layer (FR-12): explainable due-date risk.

Phase 1 (now): transparent rule-based risk score computed from the same feature
vector, so the cockpit shows P(miss due date) with visible feature contributions.
Phase 2 (after >= `ml.due_date_risk.min_labelled_cases` governed labels): a logistic
regression trained in numpy (no external ML dependency). The interface is stable,
so swapping in LightGBM/XGBoost + SHAP at G5 only changes `fit`/`predict`.

The model refuses to train below the label threshold ("do not train before labels
and data lineage are reliable").
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from .config import Settings
from .models import Action, ActionStatus, ActionType, Approval, Case, Evidence, EvidenceValidationState, Hypothesis, \
    HypothesisStatus, ReadAcrossReport, Severity

SEVERITY_RANK = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}
FEATURES = ["severity_rank", "days_open", "days_to_due", "discipline_index", "evidence_completeness", "open_actions",
            "overdue_actions", "missing_approvals", "hypotheses_validated", "read_across_gaps", "supplier_dependency",
            "similar_case_median_duration"]
REQUIRED_APPROVALS = ["problem_statement", "containment", "validated_root_cause", "corrective_action", "effectiveness"]


def build_features(case: Case, evidence: Sequence[Evidence], actions: Sequence[Action], approvals: Sequence[Approval],
                   hypotheses: Sequence[Hypothesis], reports: Sequence[ReadAcrossReport],
                   similar_median_duration: float = 0.0, today: Optional[date] = None) -> Dict[str, float]:
    today = today or date.today()
    usable = [e for e in evidence if e.validation_state in (EvidenceValidationState.VERIFIED, EvidenceValidationState.UNVERIFIED)]
    approved = {a.decision_type for a in approvals if a.decision.value == "approved"}
    gaps = sum(1 for f in reports[-1].findings if f.blocks_closure) if reports else 6
    return {
        "severity_rank": SEVERITY_RANK[case.severity],
        "days_open": (today - case.opened_date).days,
        "days_to_due": (case.customer_due_date - today).days,
        "discipline_index": int(case.current_discipline.value[1:]),
        "evidence_completeness": len(usable) / max(1, len(evidence)) if evidence else 0.0,
        "open_actions": sum(1 for a in actions if a.status not in (ActionStatus.DONE, ActionStatus.VERIFIED)),
        "overdue_actions": sum(1 for a in actions if a.due_date < today and a.status not in (ActionStatus.DONE, ActionStatus.VERIFIED)),
        "missing_approvals": sum(1 for r in REQUIRED_APPROVALS if r not in approved),
        "hypotheses_validated": sum(1 for h in hypotheses if h.status == HypothesisStatus.VALIDATED),
        "read_across_gaps": gaps,
        "supplier_dependency": 1.0 if any("supplier" in t.lower() for t in case.tags) else 0.0,
        "similar_case_median_duration": similar_median_duration,
    }


@dataclass
class RiskExplanation:
    probability: float
    contributions: Dict[str, float]
    mode: str  # rules | logistic
    top_drivers: List[str] = field(default_factory=list)


class DueDateRiskModel:
    # transparent rule weights (log-odds per unit) used until a trained model exists
    RULE_WEIGHTS = {"severity_rank": 0.25, "days_open": 0.02, "days_to_due": -0.08, "discipline_index": -0.15,
                    "evidence_completeness": -1.0, "open_actions": 0.15, "overdue_actions": 0.6, "missing_approvals": 0.35,
                    "hypotheses_validated": -0.5, "read_across_gaps": 0.2, "supplier_dependency": 0.5,
                    "similar_case_median_duration": 0.01}
    RULE_BIAS = -1.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.min_labels = int(settings.get("ml.due_date_risk.min_labelled_cases", 30))
        self.weights: Optional[np.ndarray] = None
        self.bias: float = 0.0
        self.mu: Optional[np.ndarray] = None
        self.sigma: Optional[np.ndarray] = None

    @property
    def trained(self) -> bool:
        return self.weights is not None

    def fit(self, X: Sequence[Dict[str, float]], y: Sequence[int], epochs: int = 500, lr: float = 0.1, l2: float = 0.01) -> None:
        if len(y) < self.min_labels:
            raise ValueError(f"Refusing to train: {len(y)} labelled cases < required {self.min_labels} (governed labels first)")
        A = np.array([[row[f] for f in FEATURES] for row in X], dtype=float)
        t = np.array(y, dtype=float)
        self.mu, self.sigma = A.mean(axis=0), A.std(axis=0) + 1e-9
        Z = (A - self.mu) / self.sigma
        w, b = np.zeros(Z.shape[1]), 0.0
        for _ in range(epochs):
            p = 1 / (1 + np.exp(-(Z @ w + b)))
            grad_w = Z.T @ (p - t) / len(t) + l2 * w
            grad_b = float((p - t).mean())
            w -= lr * grad_w
            b -= lr * grad_b
        self.weights, self.bias = w, b

    def predict(self, features: Dict[str, float]) -> RiskExplanation:
        x = np.array([features[f] for f in FEATURES], dtype=float)
        if self.trained:
            z = (x - self.mu) / self.sigma
            contrib = {f: float(c) for f, c in zip(FEATURES, z * self.weights)}
            logit = float(z @ self.weights + self.bias)
            mode = "logistic"
        else:
            contrib = {f: float(x[i] * self.RULE_WEIGHTS[f]) for i, f in enumerate(FEATURES)}
            logit = sum(contrib.values()) + self.RULE_BIAS
            mode = "rules"
        prob = float(1 / (1 + np.exp(-logit)))
        drivers = [f"{k} ({v:+.2f})" for k, v in sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:4]]
        return RiskExplanation(probability=round(prob, 3), contributions={k: round(v, 3) for k, v in contrib.items()},
                               mode=mode, top_drivers=drivers)

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps({"weights": self.weights.tolist() if self.trained else None, "bias": self.bias,
                                          "mu": self.mu.tolist() if self.trained else None,
                                          "sigma": self.sigma.tolist() if self.trained else None, "features": FEATURES}), encoding="utf-8")

    def load(self, path: Path) -> None:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if d["weights"] is not None:
            self.weights, self.bias = np.array(d["weights"]), float(d["bias"])
            self.mu, self.sigma = np.array(d["mu"]), np.array(d["sigma"])
