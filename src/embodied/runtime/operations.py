"""Durable call admission and recovery, not physical exactly-once execution.

持久调用准入与恢复，不承诺现实动作恰好执行一次。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from embodied.contracts.operation import OperationIdentity
from embodied.contracts.stopping import StopEvidence, StopProfile, StopRejected, StopRequest, validate_stop_evidence

SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 1_048_576
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version VALUES(1);
CREATE TABLE tasks(
    task_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, plan_revision INTEGER NOT NULL,
    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE operations(
    operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
    request_digest TEXT NOT NULL, skill_id TEXT NOT NULL, state TEXT NOT NULL,
    evidence_json TEXT
);
CREATE TABLE attempts(
    attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    boot_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, state TEXT NOT NULL,
    stop_state TEXT NOT NULL, resources_json TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL
);
CREATE TABLE operation_events(
    event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at REAL NOT NULL
);
COMMIT;
"""


class OperationConflict(RuntimeError):
    pass


class LedgerUnavailable(RuntimeError):
    pass


def request_digest(skill: str, params: dict, manifest: dict, robot_id: str) -> str:
    value = json.dumps({"robot_id": robot_id, "skill": skill, "params": params, "manifest": manifest},
                       sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    task_id: str
    request_digest: str
    skill_id: str
    state: str
    attempt_id: str
    boot_id: str
    lease_epoch: int
    stop_state: str
    resources: tuple[str, ...]
    result: dict[str, Any] | None
    robot_id: str = ""
    stop_context: dict[str, Any] = field(default_factory=dict)
    stop_evidence: dict[str, Any] | None = None


class OperationLedger:
    def __init__(self, path: str | Path = ":memory:"):
        self.boot_id = uuid4().hex
        self._lock_file = None
        self._db = None
        self.path = str(path)
        try:
            if self.path != ":memory:":
                target = Path(path).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                self.path = str(target)
                self._take_lock(Path(str(target) + ".lock"))
            self._db = sqlite3.connect(self.path, timeout=2)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                self._db.executescript(_SCHEMA)
            else:
                if "schema_version" not in tables:
                    raise LedgerUnavailable("unrecognized operation ledger schema")
                versions = [row[0] for row in self._db.execute("SELECT version FROM schema_version")]
                if versions != [SCHEMA_VERSION]:
                    raise LedgerUnavailable("unsupported operation ledger schema version")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._recover()
        except BaseException:
            self.close()
            raise

    def _take_lock(self, path: Path) -> None:
        stream = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, 2)
                if not stream.tell():
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise LedgerUnavailable("operation ledger already owned by another runtime") from exc
        self._lock_file = stream

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _recover(self) -> None:
        # 文件锁确保旧 owner 已退出，才允许恢复判断。 / The file lock proves the previous owner is gone before recovery.
        running = list(self._db.execute("SELECT operation_id FROM operations WHERE state='RUNNING'"))
        for row in running:
            self.finish(row[0], "OUTCOME_UNKNOWN", None, "STOP_UNCONFIRMED", event="recovered_unknown")

    def get(self, operation_id: str) -> OperationRecord | None:
        row = self._db.execute("""
            SELECT o.*, a.attempt_id, a.boot_id, a.lease_epoch, a.stop_state, a.resources_json, t.robot_id
            FROM operations o JOIN attempts a USING(operation_id) JOIN tasks t ON t.task_id=o.task_id
            WHERE o.operation_id=?
        """, (operation_id,)).fetchone()
        if row is None:
            return None
        admitted = self._event_payload(operation_id, row["attempt_id"], "admitted") or {}
        evidence = self._event_payload(operation_id, row["attempt_id"], "stop_observed")
        stop_context = admitted.get("stop_context", {})
        record = OperationRecord(
            operation_id=row["operation_id"], task_id=row["task_id"], request_digest=row["request_digest"],
            skill_id=row["skill_id"], state=row["state"], attempt_id=row["attempt_id"], boot_id=row["boot_id"],
            lease_epoch=row["lease_epoch"], stop_state=row["stop_state"],
            resources=tuple(json.loads(row["resources_json"])),
            result=json.loads(row["evidence_json"]) if row["evidence_json"] else None,
            robot_id=row["robot_id"], stop_context=stop_context if isinstance(stop_context, dict) else {},
            stop_evidence=evidence,
        )
        if record.stop_state == "MOTION_STOPPED_OBSERVED" and not self._valid_stored_stop(record):
            return replace(record, stop_state="STOP_UNCONFIRMED", stop_evidence=None)
        return record

    def _event_payload(self, operation_id: str, attempt_id: str, kind: str) -> dict | None:
        row = self._db.execute("""
            SELECT payload_json FROM operation_events WHERE operation_id=? AND attempt_id=? AND event_type=?
            ORDER BY rowid DESC LIMIT 1
        """, (operation_id, attempt_id, kind)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            return payload if isinstance(payload, dict) else None
        except ValueError:
            return None

    @staticmethod
    def _valid_stored_stop(record: OperationRecord, *, historical: bool = True) -> bool:
        try:
            if record.stop_context.get("tracked_workers") is not True or record.stop_evidence is None:
                return False
            profile = StopProfile.model_validate_json(json.dumps(record.stop_context["stop_profile"]))
            evidence = StopEvidence.model_validate_json(json.dumps(record.stop_evidence))
            request = StopRequest(
                operation_id=record.operation_id, attempt_id=record.attempt_id, attempt_boot_id=record.boot_id,
                observer_boot_id=evidence.observer_boot_id, robot_id=record.robot_id, lease_epoch=record.lease_epoch,
                resources=record.resources, nonce=evidence.nonce, deadline_monotonic=evidence.deadline_monotonic,
            )
            validate_stop_evidence(evidence, request, profile, historical=historical)
            return True
        except (ValueError, TypeError, KeyError, AttributeError, StopRejected):
            return False

    def lookup(self, identity: OperationIdentity, digest: str) -> OperationRecord | None:
        record = self.get(identity.operation_id)
        if record and (record.task_id != identity.task_id or record.request_digest != digest):
            raise OperationConflict("operation_id reused with a different task or request")
        return record

    def next_epoch(self) -> int:
        return self._db.execute("SELECT COALESCE(MAX(lease_epoch),0)+1 FROM attempts").fetchone()[0]

    def begin(self, identity: OperationIdentity, robot_id: str, skill: str, digest: str,
              resources: tuple[str, ...], epoch: int, *, stop_context: dict | None = None) -> OperationRecord:
        now = time.time()
        attempt = uuid4().hex
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self.lookup(identity, digest)
            if prior:
                return prior
            task = self._db.execute("SELECT robot_id FROM tasks WHERE task_id=?", (identity.task_id,)).fetchone()
            if task and task[0] != robot_id:
                raise OperationConflict("task_id belongs to a different robot")
            self._db.execute("""
                INSERT INTO tasks VALUES(?,?,?,'OPEN',?,?) ON CONFLICT(task_id) DO UPDATE SET
                plan_revision=MAX(plan_revision,excluded.plan_revision), updated_at=excluded.updated_at
            """, (identity.task_id, robot_id, identity.plan_revision, now, now))
            self._db.execute("INSERT INTO operations VALUES(?,?,?,?,'RUNNING',NULL)",
                             (identity.operation_id, identity.task_id, digest, skill))
            self._db.execute("INSERT INTO attempts VALUES(?,?,?,?, 'RUNNING','NOT_REQUESTED',?,?,NULL)",
                             (attempt, identity.operation_id, self.boot_id, epoch,
                              json.dumps(resources), now))
            self._event(identity.operation_id, attempt, "admitted",
                        {"request_digest": digest, "stop_context": stop_context or {}})
        return self.get(identity.operation_id)

    def finish(self, operation_id: str, state: str, result: dict | None, stop_state: str,
               *, event: str = "completed") -> OperationRecord:
        if state not in {"RETURNED", "FAILED", "OUTCOME_UNKNOWN"}:
            raise ValueError("invalid operation outcome")
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
        if payload is not None and len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("operation result exceeds storage limit")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record = self.get(operation_id)
            if record is None:
                raise KeyError(operation_id)
            if record.state != "RUNNING":
                return record
            self._db.execute("UPDATE operations SET state=?,evidence_json=? WHERE operation_id=?",
                             (state, payload, operation_id))
            self._db.execute("UPDATE attempts SET state=?,stop_state=?,ended_at=? WHERE operation_id=?",
                             (state, stop_state, time.time(), operation_id))
            self._event(operation_id, record.attempt_id, event, {"state": state, "stop_state": stop_state})
            if state == "OUTCOME_UNKNOWN":
                self._db.execute("UPDATE tasks SET state='OUTCOME_UNKNOWN',updated_at=? WHERE task_id=?",
                                 (time.time(), record.task_id))
        return self.get(operation_id)

    def _event(self, operation_id: str, attempt_id: str, kind: str, payload: dict) -> None:
        self._db.execute("INSERT INTO operation_events VALUES(?,?,?,?,?,?)",
                         (uuid4().hex, operation_id, attempt_id, kind, json.dumps(payload), time.time()))

    def quarantined(self) -> dict[str, tuple[str, ...]]:
        quarantined = {}
        for row in self._db.execute("SELECT operation_id FROM attempts WHERE state='OUTCOME_UNKNOWN'"):
            record = self.get(row["operation_id"])
            if record.stop_state != "MOTION_STOPPED_OBSERVED":
                quarantined[record.operation_id] = record.resources
        return quarantined

    def record_stop(self, evidence: StopEvidence) -> OperationRecord:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record = self.get(evidence.operation_id)
            if record is None or record.state != "OUTCOME_UNKNOWN":
                raise StopRejected("operation_not_unknown")
            if record.stop_state == "MOTION_STOPPED_OBSERVED":
                return record
            candidate = replace(record, stop_evidence=evidence.model_dump(mode="json"))
            if evidence.observer_boot_id != self.boot_id or not self._valid_stored_stop(candidate, historical=False):
                raise StopRejected("invalid_stop_evidence")
            # 调用结果仍未知；只更新停止子状态并追加证据。 / Preserve unknown outcome; update only stop state and append evidence.
            self._db.execute("UPDATE attempts SET stop_state='MOTION_STOPPED_OBSERVED' WHERE attempt_id=?",
                             (record.attempt_id,))
            self._event(record.operation_id, record.attempt_id, "stop_observed", evidence.model_dump(mode="json"))
        return self.get(evidence.operation_id)

    def note_stop_request(self, request: StopRequest, profile: StopProfile) -> None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record = self.get(request.operation_id)
            if (record is None or record.state != "OUTCOME_UNKNOWN" or record.attempt_id != request.attempt_id
                    or request.observer_boot_id != self.boot_id):
                raise StopRejected("stop_attempt_mismatch")
            self._event(record.operation_id, record.attempt_id, "stop_reconciliation_requested", {
                "nonce": request.nonce, "profile_digest": profile.digest,
                "observer_boot_id": request.observer_boot_id, "confirmation": "explicit_true",
            })
