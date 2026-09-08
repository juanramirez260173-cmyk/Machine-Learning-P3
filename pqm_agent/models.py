"""Canonical, versioned data model for the 8D / PQM Quality-Escalation Agent.

Every object carries a stable canonical identifier (FR-01, "Canonical IDs are
mandatory"). Pydantic validation is the "fail loud" layer: a missing mandatory
field raises instead of being silently normalised.

Automotive vocabulary used here
--------------------------------
* 8D  - Eight Disciplines problem-solving method (D1 team ... D8 closure) required
        by most OEMs (BMW, VW, Stellantis ...) for supplier complaints.
* PQM - Problem/Quality Management record, i.e. the OEM-side complaint ticket
        (BMW uses the term "PQM" for its supplier quality-issue process).
* PFMEA - Process Failure Mode and Effects Analysis (AIAG-VDA aligned).
* Control Plan - the released inspection / reaction-plan document per process step.
* WI  - Work Instruction / standardised work for the operator.
* DMC - Data Matrix Code, the laser-marked serial identity on the part.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def content_hash(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Enumerations (state machines are explicit and deterministic)
# --------------------------------------------------------------------------- #
class CaseState(str, Enum):
    """Controlled case lifecycle (Solution Reference 4.1)."""

    NEW = "new_case"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    CONTAINMENT_ACTIVE = "containment_active"
    ROOT_CAUSE_UNDER_VALIDATION = "root_cause_under_validation"
    CORRECTIVE_ACTION_APPROVED = "corrective_action_approved"
    EFFECTIVENESS_MONITORING = "effectiveness_monitoring"
    DOCUMENTS_UPDATED = "documents_updated"
    CUSTOMER_CLOSURE = "customer_closure"
    ARCHIVED = "archived"


class Discipline(str, Enum):
    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"
    D5 = "D5"
    D6 = "D6"
    D7 = "D7"
    D8 = "D8"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceType(str, Enum):
    COMPLAINT_RECORD = "complaint_record"
    PHOTO = "photo"
    MEASUREMENT_REPORT = "measurement_report"
    TEST_REPORT = "test_report"
    CONTAINMENT_RESULT = "containment_result"
    PFMEA = "pfmea"
    CONTROL_PLAN = "control_plan"
    WORK_INSTRUCTION = "work_instruction"
    ACTION_TRACKER = "action_tracker"
    MEETING_MINUTES = "meeting_minutes"
    EMAIL = "email"
    GENEALOGY = "genealogy"
    OTHER = "other"


class EvidenceValidationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"  # low extraction confidence -> manual review


class HypothesisStatus(str, Enum):
    """Hypothesis -> Evidence Supported -> Validated Root Cause (FR-06)."""

    HYPOTHESIS = "hypothesis"
    EVIDENCE_SUPPORTED = "evidence_supported"
    VALIDATED = "validated"
    REJECTED = "rejected"


class CauseType(str, Enum):
    OCCURRENCE = "occurrence"  # why the defect was produced
    ESCAPE = "escape"          # why the defect was not detected
    SYSTEMIC = "systemic"      # why the process/system allowed both


class CauseCategory(str, Enum):
    """Ishikawa (6M) categories."""

    MAN = "man"
    MACHINE = "machine"
    MATERIAL = "material"
    METHOD = "method"
    MEASUREMENT = "measurement"
    ENVIRONMENT = "environment"


class ActionType(str, Enum):
    CONTAINMENT = "containment"
    OCCURRENCE = "occurrence"
    DETECTION = "detection"
    SYSTEMIC = "systemic"


class ActionStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    VERIFIED = "verified"


class DocumentType(str, Enum):
    PFMEA = "pfmea"
    CONTROL_PLAN = "control_plan"
    WORK_INSTRUCTION = "work_instruction"


class ReleaseState(str, Enum):
    DRAFT = "draft"
    RELEASED = "released"
    SUPERSEDED = "superseded"


class ReadAcrossStatus(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    REVISION_MISMATCH = "revision_mismatch"


class ApprovalRole(str, Enum):
    QUALITY_LEAD = "quality_lead"
    EIGHT_D_OWNER = "8d_owner"
    PROCESS_ENGINEER = "process_engineer"
    PFMEA_MODERATOR = "pfmea_moderator"
    CONTROL_PLAN_OWNER = "control_plan_owner"
    WI_OWNER = "work_instruction_owner"
    PRODUCTION_REP = "production_representative"
    CUSTOMER_QUALITY_LEAD = "customer_quality_lead"
    OPERATIONS_OWNER = "operations_owner"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFY = "modify"


class DraftLabel(str, Enum):
    """Approval status labels (Solution Reference 4.3)."""

    AI_DRAFT = "AI draft"
    AWAITING_ENGINEERING_VALIDATION = "Awaiting engineering validation"
    APPROVED_INTERNAL = "Approved for internal use"
    APPROVED_CUSTOMER = "Approved for customer submission"
    CLOSED = "Closed by authorised owner"


class EscalationLevel(int, Enum):
    NONE = 0
    TEAM = 1
    MANAGEMENT = 2
    EXECUTIVE = 3


# --------------------------------------------------------------------------- #
# Base entity
# --------------------------------------------------------------------------- #
class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    schema_version: str = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    version: int = 1

    @property
    def entity_id(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Case and product identity
# --------------------------------------------------------------------------- #
class ProductIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: str
    part_name: str
    drawing_revision: Optional[str] = None
    variant: Optional[str] = None
    dmc_or_serials: List[str] = Field(default_factory=list)
    lots: List[str] = Field(default_factory=list)
    production_date_from: Optional[date] = None
    production_date_to: Optional[date] = None


class FailureDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_mode: str
    location: Optional[str] = None
    requirement: Optional[str] = None
    actual_result: Optional[str] = None
    detection_point: Optional[str] = None  # e.g. "customer EOL", "0 km", "field"
    quantity_affected: Optional[int] = None
    quantity_inspected: Optional[int] = None
    defect_code: Optional[str] = None


class Case(Entity):
    case_id: str = Field(default_factory=lambda: new_id("CASE"))
    pqm_number: Optional[str] = None
    eight_d_number: Optional[str] = None
    customer: str
    plant: str
    line: Optional[str] = None
    project: Optional[str] = None
    product: ProductIdentity
    failure: FailureDescription
    severity: Severity
    owner: str
    state: CaseState = CaseState.NEW
    current_discipline: Discipline = Discipline.D1
    detection_date: date
    opened_date: date
    customer_due_date: date
    discipline_due_dates: Dict[str, date] = Field(default_factory=dict)
    closure_date: Optional[date] = None
    source_lineage: List[str] = Field(default_factory=list)  # original file paths / URLs
    access_scope: str = "customer"  # segregation label
    team: Dict[str, str] = Field(default_factory=dict)  # role -> person
    tags: List[str] = Field(default_factory=list)

    @field_validator("customer", "plant", "owner")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("mandatory identifier must not be empty")
        return v.strip()

    @property
    def entity_id(self) -> str:
        return self.case_id


class SuspectPopulation(Entity):
    population_id: str = Field(default_factory=lambda: new_id("POP"))
    case_id: str
    description: str
    part_lot_serial_range: str
    plant: str
    line: Optional[str] = None
    shift: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    quantity_estimated: Optional[int] = None
    locations: List[str] = Field(default_factory=list)  # supplier WIP, transit, customer stock ...
    genealogy_evidence_ids: List[str] = Field(default_factory=list)
    containment_status: str = "proposed"

    @property
    def entity_id(self) -> str:
        return self.population_id


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
class Evidence(Entity):
    evidence_id: str = Field(default_factory=lambda: new_id("EV"))
    case_id: str
    evidence_type: EvidenceType
    title: str
    source_path: str
    owner: str
    created_date: date
    revision: Optional[str] = None
    release_state: ReleaseState = ReleaseState.RELEASED
    validation_state: EvidenceValidationState = EvidenceValidationState.UNVERIFIED
    extraction_confidence: float = 1.0
    content_hash: Optional[str] = None
    excerpt: Optional[str] = None  # text passage used for citation
    structured: Dict[str, Any] = Field(default_factory=dict)  # extracted table/fields
    access_class: str = "internal"

    @property
    def entity_id(self) -> str:
        return self.evidence_id


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    passage: Optional[str] = None
    field: Optional[str] = None


class Claim(BaseModel):
    """A material statement in the 8D. Must carry citations (FR-04)."""

    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(default_factory=lambda: new_id("CLM"))
    text: str
    kind: str = "fact"  # fact | assumption | hypothesis | recommendation
    citations: List[Citation] = Field(default_factory=list)
    supported: Optional[bool] = None  # set by guardrail
    flag_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# D2 problem definition (5W2H + Is / Is Not)
# --------------------------------------------------------------------------- #
class IsIsNot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    dimension: str  # what / where / when / how many / who
    is_: str = Field(alias="is")
    is_not: str
    citations: List[Citation] = Field(default_factory=list)


class ProblemDefinition(Entity):
    d2_id: str = Field(default_factory=lambda: new_id("D2"))
    case_id: str
    who: Optional[str] = None
    what: Optional[str] = None
    where: Optional[str] = None
    when: Optional[str] = None
    why_problem: Optional[str] = None  # why it is a problem (requirement violated)
    how_detected: Optional[str] = None
    how_many: Optional[str] = None
    boundary_conditions: Optional[str] = None
    is_is_not: List[IsIsNot] = Field(default_factory=list)
    facts: List[Claim] = Field(default_factory=list)
    assumptions: List[Claim] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)
    label: DraftLabel = DraftLabel.AI_DRAFT

    MANDATORY_FIELDS: ClassVar[Tuple[str, ...]] = ("who", "what", "where", "when", "why_problem", "how_detected", "how_many")

    def completeness(self) -> float:
        filled = sum(1 for f in self.MANDATORY_FIELDS if getattr(self, f))
        return filled / len(self.MANDATORY_FIELDS)

    @property
    def entity_id(self) -> str:
        return self.d2_id


# --------------------------------------------------------------------------- #
# Root cause hypotheses
# --------------------------------------------------------------------------- #
class Hypothesis(Entity):
    hypothesis_id: str = Field(default_factory=lambda: new_id("HYP"))
    case_id: str
    cause_type: CauseType
    category: CauseCategory
    description: str
    five_why_chain: List[str] = Field(default_factory=list)
    supporting_evidence_ids: List[str] = Field(default_factory=list)
    contradicting_evidence_ids: List[str] = Field(default_factory=list)
    validation_plan: Optional[str] = None
    status: HypothesisStatus = HypothesisStatus.HYPOTHESIS
    priority: int = 3  # 1 = highest
    validated_by_approval_id: Optional[str] = None

    @property
    def entity_id(self) -> str:
        return self.hypothesis_id


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
class Action(Entity):
    action_id: str = Field(default_factory=lambda: new_id("ACT"))
    case_id: str
    action_type: ActionType
    discipline: Discipline
    description: str
    owner: str
    due_date: date
    status: ActionStatus = ActionStatus.OPEN
    linked_hypothesis_id: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    evidence_required: str = ""
    evidence_ids: List[str] = Field(default_factory=list)
    exit_criteria: Optional[str] = None
    effectiveness_metric: Optional[str] = None
    effectiveness_baseline: Optional[str] = None
    effectiveness_target: Optional[str] = None
    effectiveness_result: Optional[str] = None
    effectiveness_accepted: bool = False
    change_risk: Optional[str] = None
    is_critical: bool = False

    @property
    def entity_id(self) -> str:
        return self.action_id


# --------------------------------------------------------------------------- #
# Controlled documents (PFMEA / Control Plan / WI)
# --------------------------------------------------------------------------- #
class ControlledDocument(Entity):
    document_id: str = Field(default_factory=lambda: new_id("DOC"))
    case_id: Optional[str] = None
    document_type: DocumentType
    document_number: str
    revision: str
    release_state: ReleaseState
    effective_date: Optional[date] = None
    owner: str
    source_path: str
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_key: str = "row_id"

    @property
    def entity_id(self) -> str:
        return self.document_id


class ReadAcrossFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: str
    requirement: str
    document_type: DocumentType
    status: ReadAcrossStatus
    matched_rows: List[str] = Field(default_factory=list)
    detail: str = ""
    proposed_change: Optional[str] = None
    blocks_closure: bool = False


class ReadAcrossReport(Entity):
    report_id: str = Field(default_factory=lambda: new_id("RAR"))
    case_id: str
    findings: List[ReadAcrossFinding] = Field(default_factory=list)
    revision_diff: Dict[str, Any] = Field(default_factory=dict)
    d7_complete: bool = False
    ready_to_close_recommendation: bool = False

    @property
    def entity_id(self) -> str:
        return self.report_id


# --------------------------------------------------------------------------- #
# Approvals, escalation, AI trace and audit
# --------------------------------------------------------------------------- #
class Approval(Entity):
    approval_id: str = Field(default_factory=lambda: new_id("APR"))
    case_id: str
    decision_type: str  # e.g. validated_root_cause, containment, pfmea_change, closure ...
    linked_object_id: str
    linked_object_version: int
    role: ApprovalRole
    approver: str
    decision: ApprovalDecision
    comment: str = ""
    timestamp: datetime = Field(default_factory=now_utc)

    @property
    def entity_id(self) -> str:
        return self.approval_id


class Escalation(Entity):
    escalation_id: str = Field(default_factory=lambda: new_id("ESC"))
    case_id: str
    level: EscalationLevel
    triggers: List[str]
    impact: str
    decision_required: str
    accountable_owner: str
    latest_acceptable_decision_date: date
    days_to_customer_due: int
    days_open: int

    @property
    def entity_id(self) -> str:
        return self.escalation_id


class AgentRun(Entity):
    run_id: str = Field(default_factory=lambda: new_id("RUN"))
    case_id: Optional[str]
    agent: str
    model: str
    prompt_version: str
    tools: List[str] = Field(default_factory=list)
    source_evidence_ids: List[str] = Field(default_factory=list)
    output_object_ids: List[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    evaluation: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None

    @property
    def entity_id(self) -> str:
        return self.run_id


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(default_factory=lambda: new_id("AUD"))
    timestamp: datetime = Field(default_factory=now_utc)
    case_id: Optional[str]
    actor: str  # human user id or agent name
    action: str
    object_type: str
    object_id: str
    object_version: int
    detail: Dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""


# --------------------------------------------------------------------------- #
# 8D draft container
# --------------------------------------------------------------------------- #
class DisciplineDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discipline: Discipline
    title: str
    claims: List[Claim] = Field(default_factory=list)
    completeness_findings: List[str] = Field(default_factory=list)
    label: DraftLabel = DraftLabel.AI_DRAFT


class EightDDraft(Entity):
    draft_id: str = Field(default_factory=lambda: new_id("8D"))
    case_id: str
    variant: str = "internal"  # internal | customer
    disciplines: List[DisciplineDraft] = Field(default_factory=list)
    unsupported_claim_count: int = 0
    citation_coverage: float = 0.0
    label: DraftLabel = DraftLabel.AI_DRAFT

    @property
    def entity_id(self) -> str:
        return self.draft_id


ENTITY_TYPES = {
    "case": Case,
    "suspect_population": SuspectPopulation,
    "evidence": Evidence,
    "problem_definition": ProblemDefinition,
    "hypothesis": Hypothesis,
    "action": Action,
    "controlled_document": ControlledDocument,
    "read_across_report": ReadAcrossReport,
    "approval": Approval,
    "escalation": Escalation,
    "agent_run": AgentRun,
    "eight_d_draft": EightDDraft,
}
