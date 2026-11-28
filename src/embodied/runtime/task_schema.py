"""Explicit, additive task-journal schema upgrade. 显式且仅追加的任务日志升级。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from embodied.runtime.attempt_schema import ATTEMPT_COLUMNS, mark_v3_path, migrate_attempts

TASK_DDL = (
    """CREATE TABLE task_plans(
        task_id TEXT NOT NULL REFERENCES tasks(task_id), revision INTEGER NOT NULL,
        digest TEXT NOT NULL, plan_json TEXT NOT NULL, recorder_boot_id TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(task_id,revision))""",
    """CREATE TABLE task_events(
        event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
        step_id TEXT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL,
        FOREIGN KEY(task_id,revision) REFERENCES task_plans(task_id,revision))""",
    "CREATE INDEX task_events_lookup ON task_events(task_id,revision,created_at)",
)
LEGACY_TABLES = ("tasks", "operations", "attempts", "operation_events")


def history_digest(db):
    content = {}
    for table in LEGACY_TABLES:
        columns = ",".join(ATTEMPT_COLUMNS) if table == "attempts" else "*"
        rows = db.execute(f"SELECT {columns} FROM {table}").fetchall()
        content[table] = sorted([list(row) for row in rows], key=lambda row: str(row[0]))
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest(), {table: len(rows) for table, rows in content.items()}


def inspect_schema(db):
    versions = [tuple(row) for row in db.execute("SELECT version FROM schema_version")]
    if versions not in ([(1,)], [(2,)], [(3,)]):
        raise ValueError("unsupported schema for task upgrade")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    expected = {"schema_version", *LEGACY_TABLES}
    if versions[0][0] >= 2:
        expected |= {"task_plans", "task_events"}
    if versions == [(3,)]:
        expected.add("recovery_grants")
    if tables != expected or db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("unrecognized or inconsistent task ledger")
    from embodied.runtime.operations import _SCHEMA
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(_SCHEMA)
        if versions[0][0] >= 2:
            for statement in TASK_DDL:
                reference.execute(statement)
        if versions == [(3,)]:
            migrate_attempts(reference, "0" * 64)
            sql = """SELECT type,name,sql FROM sqlite_master
                     WHERE type='trigger' OR name IN ('attempts','operation_events','recovery_grants','one_running_attempt')
                     ORDER BY type,name"""
            if [tuple(row) for row in db.execute(sql)] != reference.execute(sql).fetchall():
                raise ValueError("ledger v3 constraints differ from declared schema")
            if db.execute("""SELECT 1 FROM operations o LEFT JOIN attempts a
                    ON a.attempt_id=(SELECT attempt_id FROM attempts WHERE operation_id=o.operation_id ORDER BY attempt_no DESC LIMIT 1)
                    WHERE a.attempt_id IS NULL OR a.state IS NOT o.state OR a.result_json IS NOT o.evidence_json
                    UNION ALL SELECT 1 FROM attempts a WHERE a.attempt_no>1 AND NOT EXISTS(
                        SELECT 1 FROM recovery_grants g JOIN attempts p ON p.attempt_id=g.previous_attempt_id
                        WHERE g.state='CONSUMED' AND g.new_attempt_id=a.attempt_id AND g.operation_id=a.operation_id
                        AND p.operation_id=a.operation_id AND p.attempt_no=a.attempt_no-1) LIMIT 1""").fetchone():
                raise ValueError("v3 attempt history is inconsistent")
        for table in expected:
            for pragma in ("table_info", "foreign_key_list"):
                actual = [tuple(row) for row in db.execute(f"PRAGMA {pragma}({table})")]
                if actual != reference.execute(f"PRAGMA {pragma}({table})").fetchall():
                    raise ValueError("ledger schema shape differs from declared version")
    digest, counts = history_digest(db)
    return {"version": versions[0][0], "history_sha256": digest, "row_counts": counts,
            "running": db.execute("SELECT count(*) FROM operations WHERE state='RUNNING'").fetchone()[0]}


def upgrade_ledger(path: str | Path, *, apply: bool = False, backup_dir: str | Path | None = None,
                   target_version: int = 2):
    """Dry-run on a memory copy; apply requires exclusive ownership and a backup.

    干跑仅改内存副本；真正升级必须独占账本并先备份。
    """
    from embodied.runtime.operations import take_ledger_lock
    from embodied.runtime.release import full_ledger_digest

    if target_version not in (2, 3):
        raise ValueError("unsupported migration target")
    path = Path(path).resolve(strict=True)
    lock = take_ledger_lock(Path(str(path) + ".lock")) if apply else None
    uri = path.as_uri() + ("?mode=rw" if apply else "?mode=ro")
    source = target = None
    backup = None
    try:
        source = sqlite3.connect(uri, uri=True, timeout=2)
        target = source if apply else sqlite3.connect(":memory:")
        if not apply:
            source.backup(target)
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("BEGIN IMMEDIATE")
        before = inspect_schema(target)
        if before["version"] == target_version:
            target.rollback()
            return {"applied": False, "already_current": True, "before": before, "after": before}
        if before["running"]:
            raise ValueError("cannot upgrade with running operations")
        if target_version != before["version"] + 1:
            raise ValueError("explicit adjacent-version upgrade required")
        old_digest, old_counts = full_ledger_digest(target, before["version"])
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("source ledger integrity check failed")
        if apply:
            folder = Path(backup_dir).resolve() if backup_dir else path.parent / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder / (path.name + f".v{before['version']}-" + uuid4().hex + ".sqlite3")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader, closing(sqlite3.connect(backup)) as copy:
                reader.backup(copy)
                if full_ledger_digest(copy, before["version"]) != (old_digest, old_counts):
                    raise ValueError("backup history differs from source")
        if target_version == 3:
            migrate_attempts(target, old_digest)
            if full_ledger_digest(target, 3, v2_projection=True) != (old_digest, old_counts):
                raise ValueError("v2 history changed during attempt migration")
        else:
            for statement in TASK_DDL:
                target.execute(statement)
            target.execute("UPDATE schema_version SET version=2")
        after = inspect_schema(target)
        if before["history_sha256"] != after["history_sha256"]:
            raise ValueError("history changed during task upgrade")
        if apply and target_version == 3:
            mark_v3_path(path)
        target.commit()
        return {"applied": apply, "before": before, "after": after,
                "source_all_tables_sha256": old_digest, "source_row_counts": old_counts,
                "backup": str(backup) if backup else None,
                "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest() if backup else None}
    except BaseException:
        if target is not None:
            target.rollback()
        raise
    finally:
        if target is not None and target is not source:
            target.close()
        if source is not None:
            source.close()
        if lock is not None:
            lock.close()
