"""Similar-case retrieval (RAG) over approved historical 8Ds and lessons learned.

Design (FR-05, Solution Ref 7.2 / 12.2):
1. Deterministic scope filter FIRST (customer / access scope) - semantic search
   never sees documents outside the case's scope, so wrong-customer retrieval is
   structurally impossible rather than merely unlikely.
2. Ranking by a pluggable embedder:
   * BM25 (pure Python, default, no model download) - lexical similarity.
   * Sentence-BERT via `sentence-transformers` (tracker tool choice) when installed
     and `retrieval.embedder` = sentence-transformers or auto.
3. Every hit returns the cited excerpt, metadata and the *similarity factors* that
   matched (failure mode, process step, component, symptom, root-cause family) so
   the engineer can see why a case was proposed (no black-box scores).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .config import Settings
from .guardrails import RetrievalScopeGuard
from .models import Case

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


def tokenize(text: str) -> List[str]:
    return _TOKEN.findall(text.lower())


@dataclass
class HistoricalCase:
    """One approved historical 8D / lesson learned (from data/history/<customer>/*.json)."""

    case_id: str
    customer: str
    title: str
    failure_mode: str
    process_step: str
    component: str
    symptom: str
    root_cause_family: str
    occurrence_cause: str
    escape_cause: str
    corrective_actions: List[str]
    lessons_learned: str
    outcome: str
    duration_days: int
    source_path: str
    access_scope: str = "customer"
    year: Optional[int] = None

    def text(self) -> str:
        return " ".join([self.title, self.failure_mode, self.process_step, self.component, self.symptom,
                         self.root_cause_family, self.occurrence_cause, self.escape_cause,
                         " ".join(self.corrective_actions), self.lessons_learned])

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class RetrievalHit:
    case_id: str
    score: float
    title: str
    matched_factors: Dict[str, str]
    excerpt: str
    source_path: str
    root_cause_family: str
    corrective_actions: List[str]
    duration_days: int
    explanation: str = ""


# --------------------------------------------------------------------------- #
# Embedders
# --------------------------------------------------------------------------- #
class BM25Embedder:
    name = "bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._docs: List[List[str]] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0

    def fit(self, texts: Sequence[str]) -> None:
        self._docs = [tokenize(t) for t in texts]
        self._df = Counter()
        for d in self._docs:
            self._df.update(set(d))
        self._avgdl = sum(len(d) for d in self._docs) / max(1, len(self._docs))

    def scores(self, query: str) -> List[float]:
        q = tokenize(query)
        n = len(self._docs)
        out = []
        for d in self._docs:
            tf = Counter(d)
            s = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                denom = tf[term] + self.k1 * (1 - self.b + self.b * len(d) / max(1e-9, self._avgdl))
                s += idf * tf[term] * (self.k1 + 1) / denom
            out.append(s)
        mx = max(out) if out else 0.0
        return [x / mx if mx > 0 else 0.0 for x in out]


class SentenceTransformerEmbedder:
    name = "sentence-transformers"

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer  # optional dependency

        self._model = SentenceTransformer(model_name)
        self._matrix = None

    def fit(self, texts: Sequence[str]) -> None:
        import numpy as np

        emb = self._model.encode(list(texts), normalize_embeddings=True)
        self._matrix = np.asarray(emb)

    def scores(self, query: str) -> List[float]:
        import numpy as np

        q = self._model.encode([query], normalize_embeddings=True)[0]
        sims = self._matrix @ q
        return [float(max(0.0, s)) for s in sims]


def make_embedder(settings: Settings):
    mode = settings.get("retrieval.embedder", "auto")
    if mode in ("sentence-transformers", "auto"):
        try:
            return SentenceTransformerEmbedder(settings.get("retrieval.sentence_transformer_model", "all-MiniLM-L6-v2"))
        except Exception:
            if mode == "sentence-transformers":
                raise
    return BM25Embedder()


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class SimilarCaseService:
    def __init__(self, settings: Settings, history_dir: Optional[Path] = None, embedder=None):
        self.settings = settings
        self.scope_guard = RetrievalScopeGuard(settings)
        self.embedder = embedder or make_embedder(settings)
        self.history: List[HistoricalCase] = []
        self.top_k = int(settings.get("retrieval.top_k", 5))
        self.min_score = float(settings.get("retrieval.min_score", 0.05))
        self.factors = list(settings.get("retrieval.similarity_factors", []))
        if history_dir:
            self.load_directory(history_dir)

    def load_directory(self, history_dir: Path) -> int:
        count = 0
        for path in sorted(Path(history_dir).rglob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("source_path", str(path))
            self.history.append(HistoricalCase(**data))
            count += 1
        return count

    def add(self, cases: Iterable[HistoricalCase]) -> None:
        self.history.extend(cases)

    @staticmethod
    def query_from_case(case: Case) -> str:
        f = case.failure
        return " ".join(filter(None, [f.failure_mode, f.location or "", case.product.part_name,
                                      f.actual_result or "", f.detection_point or "", f.defect_code or ""]))

    def search(self, case: Case, query: Optional[str] = None, top_k: Optional[int] = None,
               explicit_cross_customer_approval: bool = False) -> List[RetrievalHit]:
        # 1) deterministic scope filter BEFORE semantic ranking
        in_scope = self.scope_guard.filter((h.to_dict() for h in self.history), case.customer,
                                           case.access_scope, explicit_cross_customer_approval)
        scoped = [h for h in self.history if h.case_id in {d["case_id"] for d in in_scope}
                  and h.case_id != case.case_id]
        if not scoped:
            return []
        # 2) rank
        q = query or self.query_from_case(case)
        self.embedder.fit([h.text() for h in scoped])
        scores = self.embedder.scores(q)
        ranked = sorted(zip(scoped, scores), key=lambda t: t[1], reverse=True)
        # 3) explain
        q_tokens = set(tokenize(q))
        hits: List[RetrievalHit] = []
        for h, score in ranked[: (top_k or self.top_k)]:
            if score < self.min_score:
                continue
            matched = {}
            for factor in self.factors:
                value = getattr(h, factor, "")
                if value and (set(tokenize(value)) & q_tokens):
                    matched[factor] = value
            explanation = ("matched on " + ", ".join(f"{k}='{v}'" for k, v in matched.items())) if matched else "lexical/semantic similarity only"
            hits.append(RetrievalHit(case_id=h.case_id, score=round(float(score), 3), title=h.title,
                                     matched_factors=matched, excerpt=h.lessons_learned[:300],
                                     source_path=h.source_path, root_cause_family=h.root_cause_family,
                                     corrective_actions=h.corrective_actions, duration_days=h.duration_days,
                                     explanation=explanation))
        return hits
