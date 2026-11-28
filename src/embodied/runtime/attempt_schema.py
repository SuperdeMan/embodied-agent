"""Transactional v2 to v3 attempt history migration. 以事务将 v2 迁移为按尝试保存历史的 v3。"""

from __future__ import annotations

import os
from pathlib import Path

ATTEMPT_COLUMNS = ("attempt_id", "operation_id", "boot_id", "lease_epoch", "state", "stop_state",
                   "resources_json", "started_at", "ended_at")
BUSINESS_TABLES = ("tasks", "operations", "attempts", "operation_events", "task_plans", "task_events", "recovery_grants")


def migrate_attempts(db, origin_sha256: str) -> None:
    """Rebuild the two FK-linked tables inside the caller's transaction. 在调用者事务内重建两张有外键关联的表。"""
    db.execute("""CREATE TABLE attempts_v3(
        attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
        boot_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, state TEXT NOT NULL,
        stop_state TEXT NOT NULL, resources_json TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
        attempt_no INTEGER NOT NULL DEFAULT 1 CHECK(attempt_no >= 1), result_json TEXT,
        UNIQUE(operation_id, attempt_no))""")
    db.execute("""INSERT INTO attempts_v3 SELECT a.*, 1, o.evidence_json
                  FROM attempts a JOIN operations o USING(operation_id) ORDER BY a.rowid""")
    db.execute("""CREATE TABLE operation_events_v3(
        event_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
        attempt_id TEXT NOT NULL REFERENCES attempts_v3(attempt_id), event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at REAL NOT NULL)""")
    db.execute("INSERT INTO operation_events_v3 SELECT * FROM operation_events ORDER BY rowid")
    db.execute("DROP TABLE operation_events")
    db.execute("DROP TABLE attempts")
    db.execute("ALTER TABLE attempts_v3 RENAME TO attempts")
    db.execute("ALTER TABLE operation_events_v3 RENAME TO operation_events")
    db.execute("CREATE UNIQUE INDEX one_running_attempt ON attempts(operation_id) WHERE state='RUNNING'")
    db.execute("""CREATE TABLE recovery_grants(
        grant_digest TEXT PRIMARY KEY, subject_hash TEXT NOT NULL, issuer TEXT NOT NULL, policy_digest TEXT NOT NULL,
        task_id TEXT NOT NULL, plan_revision INTEGER NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
        previous_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), request_digest TEXT NOT NULL,
        review_digest TEXT NOT NULL, ledger_boot_id TEXT NOT NULL, controller_boot_id TEXT NOT NULL,
        scene_epoch TEXT NOT NULL, profile_digest TEXT NOT NULL, evidence_id TEXT NOT NULL,
        issued_at REAL NOT NULL, deadline_monotonic REAL NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('ISSUED','DECLINED','REFUSED','CONSUMED')),
        closed_at REAL, new_attempt_id TEXT UNIQUE REFERENCES attempts(attempt_id),
        CHECK((state='CONSUMED')=(new_attempt_id IS NOT NULL)), CHECK((state='ISSUED')=(closed_at IS NULL)),
        FOREIGN KEY(task_id,plan_revision) REFERENCES task_plans(task_id,revision))""")
    db.execute("ALTER TABLE schema_version ADD COLUMN v2_origin_sha256 TEXT")
    db.execute("ALTER TABLE schema_version ADD COLUMN v3_dirty INTEGER NOT NULL DEFAULT 0 CHECK(v3_dirty IN (0,1))")
    db.execute("UPDATE schema_version SET version=3,v2_origin_sha256=?,v3_dirty=0", (origin_sha256,))
    for table in BUSINESS_TABLES:
        for verb in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER v3_dirty_{table}_{verb.lower()} AFTER {verb} ON {table}
                WHEN (SELECT v3_dirty FROM schema_version)=0
                BEGIN UPDATE schema_version SET v3_dirty=1; END""")


def mark_v3_path(path: Path) -> None:
    """Protect a migrated path even if the SQLite header becomes unreadable. 主库头损坏后仍保护已迁移路径。"""
    marker = Path(str(path) + ".schema-v3")
    try:
        with marker.open("xb") as stream:
            stream.write(b"embodied ledger v3\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return
    if os.name != "nt":
        fd = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
