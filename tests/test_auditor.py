"""Document diff: known PFMEA / CP revisions with inserted controls; verify correct gap detection."""
from datetime import date

from conftest import SAMPLE

from pqm_agent.auditor import ReadAcrossAuditor, diff_revisions, load_controlled_document
from pqm_agent.models import (Action, ActionType, CauseCategory, CauseType, Discipline, DocumentType, Hypothesis,
                              HypothesisStatus, ReadAcrossStatus, ReleaseState)

FM = "Coolant leakage at laser weld seam of the inlet tank"


def _doc(name, dtype, number, rev, state=ReleaseState.RELEASED, eff=date(2026, 9, 10)):
    return load_controlled_document(SAMPLE / "docs" / name, dtype, number, rev, state, "owner", effective_date=eff)


def _validated():
    occ = Hypothesis(case_id="C", cause_type=CauseType.OCCURRENCE, category=CauseCategory.MACHINE,
                     description="Protective glass contamination on laser optic reduced power at seam start",
                     status=HypothesisStatus.VALIDATED, supporting_evidence_ids=["EV-1"])
    esc = Hypothesis(case_id="C", cause_type=CauseType.ESCAPE, category=CauseCategory.MEASUREMENT,
                     description="In-house leak test pressure 1.5 bar below customer EOL pressure 2.5 bar so marginal welds pass",
                     status=HypothesisStatus.VALIDATED, supporting_evidence_ids=["EV-2"])
    acts = [Action(case_id="C", action_type=ActionType.OCCURRENCE, discipline=Discipline.D5, owner="A", due_date=date(2026, 9, 30),
                   description="Install laser power monitoring at workpiece with interlock and change protective glass on power drop"),
            Action(case_id="C", action_type=ActionType.DETECTION, discipline=Discipline.D5, owner="A", due_date=date(2026, 9, 30),
                   description="Raise in-house leak test pressure to 2.5 bar with 100 % test and reaction plan")]
    return [occ, esc], acts


def test_diff_detects_inserted_controls():
    old = _doc("pfmea_rev_B.csv", DocumentType.PFMEA, "PFMEA", "B")
    new = _doc("pfmea_rev_C.csv", DocumentType.PFMEA, "PFMEA", "C")
    d = diff_revisions(old, new)
    assert len(d["added"]) == 3 and len(d["removed"]) == 2
    assert any("power monitoring" in r["prevention_control"].lower() for r in d["added"])


def test_old_revision_has_gaps_new_revision_is_complete(settings):
    auditor = ReadAcrossAuditor(settings)
    hyps, acts = _validated()
    old_docs = [_doc("pfmea_rev_B.csv", DocumentType.PFMEA, "PFMEA", "B"),
                _doc("control_plan_rev_3.csv", DocumentType.CONTROL_PLAN, "CP", "3"),
                _doc("wi_laser_rev_2.csv", DocumentType.WORK_INSTRUCTION, "WI", "2")]
    report = auditor.audit("C", FM, hyps, acts, old_docs, implementation_evidence_present=True)
    statuses = {f.check_id: f.status for f in report.findings}
    assert not report.d7_complete
    assert statuses["RA-2"] in (ReadAcrossStatus.MISSING, ReadAcrossStatus.AMBIGUOUS)  # glass contamination absent in rev B
    assert statuses["RA-4"] != ReadAcrossStatus.PRESENT  # CP still at 1.5 bar
    new_docs = [_doc("pfmea_rev_C.csv", DocumentType.PFMEA, "PFMEA", "C"),
                _doc("control_plan_rev_4.csv", DocumentType.CONTROL_PLAN, "CP", "4"),
                _doc("wi_laser_rev_3.csv", DocumentType.WORK_INSTRUCTION, "WI", "3")]
    report = auditor.audit("C", FM, hyps, acts, new_docs, implementation_evidence_present=True)
    assert report.d7_complete
    assert all(f.status == ReadAcrossStatus.PRESENT for f in report.findings), [(f.check_id, f.status) for f in report.findings]
    assert report.ready_to_close_recommendation is False  # never automated


def test_revision_mismatch_and_draft_documents_block_closure(settings):
    auditor = ReadAcrossAuditor(settings)
    hyps, acts = _validated()
    docs = [_doc("pfmea_rev_C.csv", DocumentType.PFMEA, "PFMEA", "C"),
            _doc("control_plan_rev_4.csv", DocumentType.CONTROL_PLAN, "CP", "4", state=ReleaseState.DRAFT),
            _doc("wi_laser_rev_3.csv", DocumentType.WORK_INSTRUCTION, "WI", "3")]
    report = auditor.audit("C", FM, hyps, acts, docs, implementation_evidence_present=True,
                           expected_min_revisions={"pfmea": "D"})
    st = {f.check_id: f.status for f in report.findings}
    assert st["RA-1"] == ReadAcrossStatus.REVISION_MISMATCH
    assert st["RA-4"] == ReadAcrossStatus.MISSING  # only a DRAFT control plan exists -> not usable
    assert not report.d7_complete


def test_missing_implementation_evidence_blocks_ra6(settings):
    auditor = ReadAcrossAuditor(settings)
    hyps, acts = _validated()
    docs = [_doc("pfmea_rev_C.csv", DocumentType.PFMEA, "PFMEA", "C"),
            _doc("control_plan_rev_4.csv", DocumentType.CONTROL_PLAN, "CP", "4"),
            _doc("wi_laser_rev_3.csv", DocumentType.WORK_INSTRUCTION, "WI", "3")]
    report = auditor.audit("C", FM, hyps, acts, docs, implementation_evidence_present=False)
    assert {f.check_id: f.status for f in report.findings}["RA-6"] == ReadAcrossStatus.MISSING
    assert not report.d7_complete
