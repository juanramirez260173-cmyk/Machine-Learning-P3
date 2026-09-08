"""Case Intake Agent: builds the structured case from complaint / PQM sources,
computes discipline due dates and asks missing-information questions (FR-01, FR-02)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import (Case, Discipline, Evidence, EvidenceType, EvidenceValidationState, FailureDescription,
                      ProductIdentity, ReleaseState, Severity, content_hash)
from .base import AgentContext, BaseAgent


class CaseIntakeAgent(BaseAgent):
    name = "case_intake_agent"

    def build_case(self, record: Dict[str, Any], source_path: str) -> Case:
        """Fail-loud construction of the canonical case from a complaint / PQM record."""
        opened = date.fromisoformat(record["opened_date"])
        settings = self.settings.for_customer(record["customer"])
        deadlines = settings.get("discipline_deadlines_days", {})
        due_dates = {d: (opened + timedelta(days=int(deadlines.get(d, 0)))) for d in deadlines}
        customer_due = record.get("customer_due_date")
        case = Case(
            case_id=record.get("case_id") or Case().case_id,
            pqm_number=record.get("pqm_number"),
            eight_d_number=record.get("eight_d_number"),
            customer=record["customer"],
            plant=record["plant"],
            line=record.get("line"),
            project=record.get("project"),
            product=ProductIdentity(**record["product"]),
            failure=FailureDescription(**record["failure"]),
            severity=Severity(record.get("severity", "medium")),
            owner=record["owner"],
            detection_date=date.fromisoformat(record["detection_date"]),
            opened_date=opened,
            customer_due_date=date.fromisoformat(customer_due) if customer_due else due_dates.get("D8", opened + timedelta(days=60)),
            discipline_due_dates=due_dates,
            source_lineage=[source_path] + list(record.get("source_lineage", [])),
            team=record.get("team", {}),
            tags=record.get("tags", []),
        )
        return case

    def load_evidence_folder(self, case: Case, evidence_dir: Path) -> List[Evidence]:
        """Each *.json in the evidence folder is one evidence metadata record.
        The original document stays in the controlled repository; we keep the link,
        hash, revision and the extracted passage / structured fields."""
        items: List[Evidence] = []
        for path in sorted(Path(evidence_dir).glob("*.json")):
            meta = json.loads(path.read_text(encoding="utf-8"))
            source = Path(evidence_dir) / meta.get("source_path", path.name)
            payload = source.read_bytes() if source.exists() and source.is_file() else path.read_bytes()
            confidence = float(meta.get("extraction_confidence", 1.0))
            ev = Evidence(
                evidence_id=meta.get("evidence_id") or Evidence(case_id=case.case_id, evidence_type=EvidenceType.OTHER,
                                                                 title="x", source_path="x", owner="x",
                                                                 created_date=date.today()).evidence_id,
                case_id=case.case_id,
                evidence_type=EvidenceType(meta["evidence_type"]),
                title=meta["title"],
                source_path=str(source if source.exists() else meta.get("source_path", path.name)),
                owner=meta["owner"],
                created_date=date.fromisoformat(meta["created_date"]),
                revision=meta.get("revision"),
                release_state=ReleaseState(meta.get("release_state", "released")),
                extraction_confidence=confidence,
                content_hash=content_hash(payload),
                excerpt=meta.get("excerpt"),
                structured=meta.get("structured", {}),
                access_class=meta.get("access_class", "internal"),
                validation_state=EvidenceValidationState.QUARANTINED
                if confidence < float(self.settings.get("guardrails.min_extraction_confidence", 0.7))
                else EvidenceValidationState(meta.get("validation_state", "unverified")),
            )
            items.append(ev)
        return items

    def missing_information(self, case: Case, evidence: List[Evidence]) -> List[str]:
        questions: List[str] = []
        f = case.failure
        if not f.quantity_affected:
            questions.append("How many parts are affected (quantity rejected vs inspected)?")
        if not f.requirement or not f.actual_result:
            questions.append("What is the specified requirement and the measured actual result?")
        if not f.detection_point:
            questions.append("Where was the defect detected (customer EOL, 0 km, field, incoming inspection)?")
        if not case.product.dmc_or_serials and not case.product.lots:
            questions.append("Which serial numbers / DMC or lots are affected (needed to bound the suspect population)?")
        if not case.product.production_date_from:
            questions.append("What is the production date window of the affected parts?")
        present = {e.evidence_type for e in evidence if e.validation_state != EvidenceValidationState.REJECTED}
        reqs = self.settings.for_customer(case.customer).get("evidence_requirements_by_discipline", {})
        for disc, types in reqs.items():
            for t in types:
                if EvidenceType(t) not in present:
                    questions.append(f"{disc}: evidence of type '{t}' is not in the register - please provide or mark not available.")
        return questions

    def ingest(self, record_path: Path, evidence_dir: Optional[Path] = None) -> Tuple[Case, List[Evidence], List[str]]:
        record = json.loads(Path(record_path).read_text(encoding="utf-8"))
        case = self.build_case(record, str(record_path))
        self.store.put(case, actor=self.name, action="case:created", detail={"source": str(record_path)})
        evidence = self.load_evidence_folder(case, evidence_dir) if evidence_dir else []
        for ev in evidence:
            self.store.put(ev, actor=self.name, action="evidence:registered")
        questions = self.missing_information(case, evidence)
        ctx = AgentContext(case=case, evidence=evidence, settings=self.settings, extra={})
        self.record_run(ctx, [case.case_id] + [e.evidence_id for e in evidence], tools=["load_json", "hash"])
        return case, evidence, questions
