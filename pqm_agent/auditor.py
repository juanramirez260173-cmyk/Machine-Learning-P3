"""PFMEA / Control Plan / Work Instruction read-across auditor (D7).

This is the "loop almost nobody closes": after a corrective action is approved,
the auditor checks whether the *released* controlled documents actually reflect
the validated failure mode, causes and controls, and classifies each checklist
requirement as Present / Missing / Ambiguous / Revision Mismatch. A Missing or
Revision Mismatch result marks D7 incomplete and blocks an automated
"ready to close" recommendation (closure blocker rule).

Document loading supports Excel (openpyxl) and CSV exports of the PFMEA and
Control Plan, which is how most plants keep them (AIAG-VDA templates).
"""
from __future__ import annotations

import csv
import difflib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Settings
from .models import (Action, ActionType, ControlledDocument, DocumentType, Hypothesis, HypothesisStatus,
                     ReadAcrossFinding, ReadAcrossReport, ReadAcrossStatus, ReleaseState)
from .retrieval import tokenize

# Column normalisation: many plants use different headers for the same thing.
PFMEA_COLUMNS = {
    "process_step": ["process step", "process_step", "step", "operation", "op", "prozessschritt"],
    "function": ["function", "requirement", "funktion"],
    "failure_mode": ["failure mode", "failure_mode", "potential failure mode", "fehlerart"],
    "effect": ["effect", "potential effect", "failure effect", "fehlerfolge"],
    "severity": ["s", "sev", "severity"],
    "cause": ["cause", "failure cause", "potential cause", "fehlerursache"],
    "occurrence": ["o", "occ", "occurrence"],
    "prevention_control": ["prevention control", "prevention", "current prevention control", "vermeidung"],
    "detection_control": ["detection control", "detection", "current detection control", "entdeckung"],
    "detection": ["d", "det"],
    "ap": ["ap", "action priority", "rpn"],
}
CP_COLUMNS = {
    "process_step": ["process step", "process_step", "operation", "step"],
    "characteristic": ["characteristic", "product characteristic", "process characteristic", "merkmal"],
    "specification": ["specification", "spec", "tolerance", "spezifikation"],
    "method": ["evaluation method", "measurement technique", "method", "prüfmittel"],
    "sample_size": ["sample size", "size", "stichprobe"],
    "frequency": ["frequency", "freq", "häufigkeit"],
    "control_method": ["control method", "control", "regelmethode"],
    "reaction_plan": ["reaction plan", "reaction", "reaktionsplan"],
}
WI_COLUMNS = {
    "step": ["step", "no", "nr", "sequence"],
    "instruction": ["instruction", "text", "work instruction", "description", "anweisung"],
    "key_point": ["key point", "keypoint", "critical", "why"],
}


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _map_columns(headers: Sequence[str], mapping: Dict[str, List[str]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    normalised = [_norm(h) for h in headers]
    for canonical, aliases in mapping.items():
        for alias in aliases:
            if alias in normalised and canonical not in out:
                out[canonical] = normalised.index(alias)
                break
    return out


def load_rows(path: Path) -> Tuple[List[str], List[List[Any]]]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.reader(fh)]
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return [], []
    return [str(h or "") for h in rows[0]], rows[1:]


def load_controlled_document(path: Path, document_type: DocumentType, document_number: str, revision: str,
                             release_state: ReleaseState, owner: str, case_id: Optional[str] = None,
                             effective_date=None) -> ControlledDocument:
    headers, data = load_rows(path)
    mapping = {DocumentType.PFMEA: PFMEA_COLUMNS, DocumentType.CONTROL_PLAN: CP_COLUMNS,
               DocumentType.WORK_INSTRUCTION: WI_COLUMNS}[document_type]
    cols = _map_columns(headers, mapping)
    if not cols:
        raise ValueError(f"{path}: no recognisable {document_type.value} columns in header {headers}")
    rows: List[Dict[str, Any]] = []
    for i, r in enumerate(data, start=1):
        row = {canonical: (r[idx] if idx < len(r) else None) for canonical, idx in cols.items()}
        row = {k: ("" if v is None else str(v).strip()) for k, v in row.items()}
        row["row_id"] = f"{document_number}-r{i}"
        rows.append(row)
    return ControlledDocument(case_id=case_id, document_type=document_type, document_number=document_number,
                              revision=revision, release_state=release_state, owner=owner, source_path=str(path),
                              rows=rows, effective_date=effective_date)


# --------------------------------------------------------------------------- #
# Fuzzy matching
# --------------------------------------------------------------------------- #
STOPWORDS = {"the", "a", "an", "of", "at", "in", "on", "for", "to", "with", "and", "or", "so", "is", "are", "be",
             "by", "from", "as", "that", "this", "it", "its", "into", "per", "if", "not"}


def content_tokens(text: str) -> set:
    return {t for t in tokenize(text) if t not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Blend of token containment / Jaccard and sequence ratio - robust to wording changes."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    containment = len(ta & tb) / min(len(ta), len(tb))
    ratio = difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    return max(0.6 * containment + 0.4 * jaccard, ratio)


def best_match(needle: str, rows: Iterable[Dict[str, Any]], fields: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], float]:
    best, best_score = None, 0.0
    for row in rows:
        hay = " ".join(str(row.get(f, "")) for f in fields)
        s = similarity(needle, hay)
        if s > best_score:
            best, best_score = row, s
    return best, best_score


# --------------------------------------------------------------------------- #
# Revision diff
# --------------------------------------------------------------------------- #
def diff_revisions(old: ControlledDocument, new: ControlledDocument) -> Dict[str, Any]:
    """Row-level diff between two revisions of the same controlled document."""
    key_fields = {
        DocumentType.PFMEA: ("process_step", "failure_mode", "cause"),
        DocumentType.CONTROL_PLAN: ("process_step", "characteristic"),
        DocumentType.WORK_INSTRUCTION: ("step",),
    }[new.document_type]

    def key(row: Dict[str, Any]) -> str:
        return "|".join(_norm(row.get(f, "")) for f in key_fields)

    old_map = {key(r): r for r in old.rows}
    new_map = {key(r): r for r in new.rows}
    added = [new_map[k] for k in new_map if k not in old_map]
    removed = [old_map[k] for k in old_map if k not in new_map]
    changed = []
    for k in new_map:
        if k in old_map:
            deltas = {f: {"old": old_map[k].get(f, ""), "new": new_map[k].get(f, "")}
                      for f in new_map[k] if f != "row_id" and _norm(new_map[k].get(f)) != _norm(old_map[k].get(f))}
            if deltas:
                changed.append({"row_id": new_map[k]["row_id"], "changes": deltas})
    return {"document_type": new.document_type.value, "document_number": new.document_number,
            "old_revision": old.revision, "new_revision": new.revision,
            "added": added, "removed": removed, "changed": changed,
            "summary": f"{len(added)} added, {len(removed)} removed, {len(changed)} changed rows"}


# --------------------------------------------------------------------------- #
# Auditor
# --------------------------------------------------------------------------- #
class ReadAcrossAuditor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.threshold = float(settings.get("read_across.fuzzy_match_threshold", 0.55))
        self.checklist = list(settings.get("read_across.checklist", []))

    def _released(self, docs: Sequence[ControlledDocument], dtype: DocumentType) -> Optional[ControlledDocument]:
        candidates = [d for d in docs if d.document_type == dtype and d.release_state == ReleaseState.RELEASED]
        if not candidates:
            return None
        return sorted(candidates, key=lambda d: (d.effective_date or d.created_at.date(), d.revision))[-1]

    def _classify(self, score: float) -> ReadAcrossStatus:
        """PRESENT at/above threshold; AMBIGUOUS (manual review) in the band just below; MISSING otherwise."""
        if score >= self.threshold:
            return ReadAcrossStatus.PRESENT
        if score >= self.threshold * 0.8:
            return ReadAcrossStatus.AMBIGUOUS
        return ReadAcrossStatus.MISSING

    @staticmethod
    def _cause_and_control(cause: str, actions: Sequence[Action], rows: Sequence[Dict[str, Any]],
                           cause_fields: Sequence[str], control_fields: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], float, str]:
        """Both the cause AND its control must be represented: score = min(cause match, control match).
        The cause is matched row by row; the control is matched against the control columns of the
        best cause row, or of any row at the same process step (PFMEA splits escape rows across steps)."""
        row, cause_score = best_match(cause, rows, cause_fields)
        if row is None:
            return None, 0.0, "no rows"
        if not actions:
            return row, cause_score, f"cause match {cause_score:.2f}; no approved action to compare"
        step = _norm(row.get("process_step", ""))
        candidates = [r for r in rows if _norm(r.get("process_step", "")) == step] or [row]
        control_score = 0.0
        for a in actions:
            for r in rows:
                for f in control_fields:
                    if not _norm(r.get(f)):
                        continue
                    weight = 1.0 if r in candidates else 0.85
                    control_score = max(control_score, weight * similarity(a.description, str(r.get(f))))
        return row, min(cause_score, control_score), f"cause match {cause_score:.2f}, control match {control_score:.2f}"

    def audit(self, case_id: str, failure_mode: str, hypotheses: Sequence[Hypothesis],
              actions: Sequence[Action], documents: Sequence[ControlledDocument],
              implementation_evidence_present: bool = False,
              expected_min_revisions: Optional[Dict[str, str]] = None) -> ReadAcrossReport:
        """Run the six-point D7 checklist against the *released* documents."""
        validated = [h for h in hypotheses if h.status == HypothesisStatus.VALIDATED]
        occurrence = [h for h in validated if h.cause_type.value == "occurrence"]
        escape = [h for h in validated if h.cause_type.value == "escape"]
        prevention_actions = [a for a in actions if a.action_type == ActionType.OCCURRENCE]
        detection_actions = [a for a in actions if a.action_type == ActionType.DETECTION]
        pfmea = self._released(documents, DocumentType.PFMEA)
        cp = self._released(documents, DocumentType.CONTROL_PLAN)
        wi = self._released(documents, DocumentType.WORK_INSTRUCTION)
        expected_min_revisions = expected_min_revisions or {}
        findings: List[ReadAcrossFinding] = []

        def revision_mismatch(doc: Optional[ControlledDocument], dtype: DocumentType) -> Optional[str]:
            exp = expected_min_revisions.get(dtype.value)
            if doc is None:
                return f"No released {dtype.value} available"
            if exp and _norm(doc.revision) != _norm(exp):
                return f"Released revision {doc.revision} does not match expected post-action revision {exp}"
            return None

        for item in self.checklist:
            cid, dtype, req = item["id"], DocumentType(item["document_type"]), item["requirement"]
            doc = {DocumentType.PFMEA: pfmea, DocumentType.CONTROL_PLAN: cp, DocumentType.WORK_INSTRUCTION: wi}[dtype]
            mismatch = revision_mismatch(doc, dtype)
            if doc is None or (mismatch and cid != "RA-6"):
                status = ReadAcrossStatus.REVISION_MISMATCH if doc is not None else ReadAcrossStatus.MISSING
                findings.append(ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype, status=status,
                                                  detail=mismatch or "", blocks_closure=True,
                                                  proposed_change=f"Release {dtype.value} revision reflecting the approved action"))
                continue

            if cid == "RA-1":
                row, score = best_match(failure_mode, doc.rows, ("process_step", "failure_mode", "effect"))
                status = self._classify(score)
                findings.append(self._finding(cid, req, dtype, status, row, score,
                                              proposed=f"Add failure mode '{failure_mode}' at the affected process step with effect and severity"))
            elif cid == "RA-2":
                if not occurrence:
                    findings.append(ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype,
                                                      status=ReadAcrossStatus.MISSING, detail="No validated occurrence cause",
                                                      blocks_closure=True))
                    continue
                row, score, why = self._cause_and_control(occurrence[0].description, prevention_actions, doc.rows,
                                                          ("cause",), ("prevention_control",))
                status = self._classify(score)
                findings.append(self._finding(cid, req, dtype, status, row, score, extra=why,
                                              proposed=f"Document cause '{occurrence[0].description}' with prevention control '{prevention_actions[0].description if prevention_actions else 'TBD'}'"))
            elif cid == "RA-3":
                if not escape:
                    findings.append(ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype,
                                                      status=ReadAcrossStatus.MISSING, detail="No validated escape cause",
                                                      blocks_closure=True))
                    continue
                row, score, why = self._cause_and_control(escape[0].description, detection_actions, doc.rows,
                                                          ("cause", "failure_mode"), ("detection_control", "prevention_control"))
                status = self._classify(score)
                findings.append(self._finding(cid, req, dtype, status, row, score, extra=why,
                                              proposed=f"Document detection control '{detection_actions[0].description if detection_actions else 'TBD'}' for escape cause"))
            elif cid == "RA-4":
                if not detection_actions and not prevention_actions:
                    findings.append(ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype,
                                                      status=ReadAcrossStatus.MISSING, detail="No approved actions to compare",
                                                      blocks_closure=True))
                    continue
                best_row, best_score = None, 0.0
                for a in detection_actions + prevention_actions:
                    row, score = best_match(a.description, doc.rows,
                                            ("characteristic", "method", "frequency", "sample_size", "control_method", "reaction_plan"))
                    if score > best_score:
                        best_row, best_score = row, score
                status = self._classify(best_score)
                if status == ReadAcrossStatus.PRESENT and best_row and not _norm(best_row.get("reaction_plan")):
                    status = ReadAcrossStatus.AMBIGUOUS
                findings.append(self._finding(cid, req, dtype, status, best_row, best_score,
                                              proposed="Update characteristic / method / frequency / sample size / reaction plan to the approved action"))
            elif cid == "RA-5":
                operator_actions = [a for a in actions if a.action_type in (ActionType.OCCURRENCE, ActionType.DETECTION)]
                if not operator_actions:
                    findings.append(ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype,
                                                      status=ReadAcrossStatus.PRESENT, detail="No operator-facing change"))
                    continue
                best_row, best_score = None, 0.0
                for a in operator_actions:
                    row, score = best_match(a.description, doc.rows, ("instruction", "key_point"))
                    if score > best_score:
                        best_row, best_score = row, score
                status = self._classify(best_score)
                findings.append(self._finding(cid, req, dtype, status, best_row, best_score,
                                              proposed="Add the operator-facing change to the work instruction / standardised work"))
            elif cid == "RA-6":
                detail = mismatch or ""
                if mismatch:
                    status = ReadAcrossStatus.REVISION_MISMATCH
                elif not implementation_evidence_present:
                    status = ReadAcrossStatus.MISSING
                    detail = "Implementation evidence (training record, release note, effective date) not in evidence register"
                elif not (pfmea.effective_date and (cp is None or cp.effective_date)):
                    status = ReadAcrossStatus.AMBIGUOUS
                    detail = "Effective date not recorded on released revision"
                else:
                    status = ReadAcrossStatus.PRESENT
                    detail = f"PFMEA {pfmea.revision} effective {pfmea.effective_date}; CP {cp.revision if cp else 'n/a'}"
                findings.append(ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype, status=status,
                                                  detail=detail, blocks_closure=status in (ReadAcrossStatus.MISSING, ReadAcrossStatus.REVISION_MISMATCH),
                                                  proposed_change=None if status == ReadAcrossStatus.PRESENT else "Attach implementation evidence and effective date"))

        report = ReadAcrossReport(case_id=case_id, findings=findings)
        report.d7_complete = not any(f.blocks_closure for f in findings)
        report.ready_to_close_recommendation = False  # never automated; human approves closure
        return report

    def _finding(self, cid: str, req: str, dtype: DocumentType, status: ReadAcrossStatus,
                 row: Optional[Dict[str, Any]], score: float, proposed: str, extra: str = "") -> ReadAcrossFinding:
        blocks = status in (ReadAcrossStatus.MISSING, ReadAcrossStatus.REVISION_MISMATCH)
        detail = f"best match score {score:.2f}" + (f" on row {row['row_id']}" if row else "") + (f" ({extra})" if extra else "")
        return ReadAcrossFinding(check_id=cid, requirement=req, document_type=dtype, status=status,
                                 matched_rows=[row["row_id"]] if row and status != ReadAcrossStatus.MISSING else [],
                                 detail=detail, proposed_change=None if status == ReadAcrossStatus.PRESENT else proposed,
                                 blocks_closure=blocks)
