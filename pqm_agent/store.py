"""SQLite-backed document store with version history and a hash-chained audit log.

Why SQLite + JSON for the pilot: the solution reference asks for a relational case
store plus immutable audit history, without prescribing a vendor. A single-file
database keeps the pilot portable (laptop, plant server, container) and every
entity is stored as validated JSON so the canonical schema stays the contract.
Swap `Store` for a PostgreSQL / SQL Server implementation at G4 without touching
agents - they only use `put / get / list / audit`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Type, TypeVar

from pydantic import BaseModel

from .models import ENTITY_TYPES, AuditEvent, content_hash, now_utc

T = TypeVar("T", bound=BaseModel)

_TYPE_BY_CLASS = {cls: name for name, cls in ENTITY_TYPES.items()}


def _id_field(model: BaseModel | Type[BaseModel]) -> str:
    cls = model if isinstance(model, type) else type(model)
    for candidate in ("case_id", "population_id", "evidence_id", "d2_id", "hypothesis_id",
                      "action_id", "document_id", "report_id", "approval_id", "escalation_id",
                      "run_id", "draft_id"):
        if candidate in cls.model_fields:
            # `case_id` is a foreign key on most entities; only the Case owns it as PK
            if candidate == "case_id" and cls.__name__ != "Case":
                continue
            return candidate
    raise TypeError(f"No id field on {cls.__name__}")


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS entities (
                   entity_type TEXT NOT NULL,
                   entity_id   TEXT NOT NULL,
                   case_id     TEXT,
                   customer    TEXT,
                   version     INTEGER NOT NULL,
                   payload     TEXT NOT NULL,
                   updated_at  TEXT NOT NULL,
                   PRIMARY KEY (entity_type, entity_id))"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS entity_versions (
                   entity_type TEXT NOT NULL,
                   entity_id   TEXT NOT NULL,
                   version     INTEGER NOT NULL,
                   payload     TEXT NOT NULL,
                   stored_at   TEXT NOT NULL,
                   PRIMARY KEY (entity_type, entity_id, version))"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS audit_log (
                   seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                   event_id   TEXT NOT NULL,
                   timestamp  TEXT NOT NULL,
                   case_id    TEXT,
                   actor      TEXT NOT NULL,
                   action     TEXT NOT NULL,
                   object_type TEXT NOT NULL,
                   object_id  TEXT NOT NULL,
                   object_version INTEGER NOT NULL,
                   detail     TEXT NOT NULL,
                   prev_hash  TEXT NOT NULL,
                   hash       TEXT NOT NULL)"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS ix_entities_case ON entities(case_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_audit_case ON audit_log(case_id)")
        self._conn.commit()

    # ----------------------------------------------------------------- entities
    def put(self, entity: BaseModel, actor: str = "system", action: str = "upsert",
            detail: Optional[Dict[str, Any]] = None) -> BaseModel:
        etype = _TYPE_BY_CLASS[type(entity)]
        eid = getattr(entity, _id_field(entity))
        existing = self._conn.execute(
            "SELECT version FROM entities WHERE entity_type=? AND entity_id=?", (etype, eid)
        ).fetchone()
        if existing:
            entity.version = int(existing["version"]) + 1
        if "updated_at" in type(entity).model_fields:
            entity.updated_at = now_utc()
        payload = entity.model_dump_json(by_alias=True)
        case_id = getattr(entity, "case_id", None)
        customer = getattr(entity, "customer", None)
        stamp = now_utc().isoformat()
        self._conn.execute(
            """INSERT INTO entities(entity_type, entity_id, case_id, customer, version, payload, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                   version=excluded.version, payload=excluded.payload,
                   updated_at=excluded.updated_at, case_id=excluded.case_id, customer=excluded.customer""",
            (etype, eid, case_id, customer, entity.version, payload, stamp),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO entity_versions(entity_type, entity_id, version, payload, stored_at) VALUES (?,?,?,?,?)",
            (etype, eid, entity.version, payload, stamp),
        )
        self._conn.commit()
        self.audit(actor=actor, action=action, object_type=etype, object_id=eid,
                   object_version=entity.version, case_id=case_id, detail=detail or {})
        return entity

    def get(self, cls: Type[T], entity_id: str) -> Optional[T]:
        etype = _TYPE_BY_CLASS[cls]
        row = self._conn.execute(
            "SELECT payload FROM entities WHERE entity_type=? AND entity_id=?", (etype, entity_id)
        ).fetchone()
        return cls.model_validate_json(row["payload"]) if row else None

    def require(self, cls: Type[T], entity_id: str) -> T:
        obj = self.get(cls, entity_id)
        if obj is None:
            raise KeyError(f"{cls.__name__} {entity_id} not found")
        return obj

    def get_version(self, cls: Type[T], entity_id: str, version: int) -> Optional[T]:
        etype = _TYPE_BY_CLASS[cls]
        row = self._conn.execute(
            "SELECT payload FROM entity_versions WHERE entity_type=? AND entity_id=? AND version=?",
            (etype, entity_id, version),
        ).fetchone()
        return cls.model_validate_json(row["payload"]) if row else None

    def list(self, cls: Type[T], case_id: Optional[str] = None, customer: Optional[str] = None) -> List[T]:
        etype = _TYPE_BY_CLASS[cls]
        sql = "SELECT payload FROM entities WHERE entity_type=?"
        params: List[Any] = [etype]
        if case_id is not None:
            sql += " AND case_id=?"
            params.append(case_id)
        if customer is not None:
            sql += " AND customer=?"
            params.append(customer)
        sql += " ORDER BY updated_at"
        return [cls.model_validate_json(r["payload"]) for r in self._conn.execute(sql, params).fetchall()]

    # -------------------------------------------------------------------- audit
    def audit(self, actor: str, action: str, object_type: str, object_id: str,
              object_version: int = 0, case_id: Optional[str] = None,
              detail: Optional[Dict[str, Any]] = None) -> AuditEvent:
        last = self._conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else "GENESIS"
        event = AuditEvent(case_id=case_id, actor=actor, action=action, object_type=object_type,
                           object_id=object_id, object_version=object_version, detail=detail or {},
                           prev_hash=prev_hash)
        body = json.dumps({
            "event_id": event.event_id, "timestamp": event.timestamp.isoformat(), "case_id": case_id,
            "actor": actor, "action": action, "object_type": object_type, "object_id": object_id,
            "object_version": object_version, "detail": event.detail, "prev_hash": prev_hash,
        }, sort_keys=True, default=str)
        event.hash = content_hash(body)
        self._conn.execute(
            """INSERT INTO audit_log(event_id, timestamp, case_id, actor, action, object_type, object_id,
               object_version, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (event.event_id, event.timestamp.isoformat(), case_id, actor, action, object_type, object_id,
             object_version, json.dumps(event.detail, default=str), prev_hash, event.hash),
        )
        self._conn.commit()
        return event

    def audit_trail(self, case_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_log"
        params: List[Any] = []
        if case_id:
            sql += " WHERE case_id=?"
            params.append(case_id)
        sql += " ORDER BY seq"
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    def verify_audit_chain(self) -> bool:
        """Recompute the hash chain; any tampering breaks the chain."""
        prev = "GENESIS"
        for r in self._conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall():
            body = json.dumps({
                "event_id": r["event_id"], "timestamp": r["timestamp"], "case_id": r["case_id"],
                "actor": r["actor"], "action": r["action"], "object_type": r["object_type"],
                "object_id": r["object_id"], "object_version": r["object_version"],
                "detail": json.loads(r["detail"]), "prev_hash": r["prev_hash"],
            }, sort_keys=True, default=str)
            if r["prev_hash"] != prev or content_hash(body) != r["hash"]:
                return False
            prev = r["hash"]
        return True

    def close(self) -> None:
        self._conn.close()
