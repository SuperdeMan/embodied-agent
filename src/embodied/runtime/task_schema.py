"""Explicit, additive task-journal schema upgrade. 显式且仅追加的任务日志升级。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

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
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        content[table] = sorted([list(row) for row in rows], key=lambda row: str(row[0]))
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest(), {table: len(rows) for table, rows in content.items()}


def inspect_schema(db):
    versions = db.execute("SELECT version FROM schema_version").fetchall()
    if versions not in ([(1,)], [(2,)]):
        raise ValueError("unsupported schema for task upgrade")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    expected = {"schema_version", *LEGACY_TABLES}
    if versions == [(2,)]:
        expected |= {"task_plans", "task_events"}
    if tables != expected or db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("unrecognized or inconsistent task ledger")
    from embodied.runtime.operations import _SCHEMA
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(_SCHEMA)
        if versions == [(2,)]:
            for statement in TASK_DDL:
                reference.execute(statement)
        for table in expected:
            for pragma in ("table_info", "foreign_key_list"):
                actual = [tuple(row) for row in db.execute(f"PRAGMA {pragma}({table})")]
                if actual != reference.execute(f"PRAGMA {pragma}({table})").fetchall():
                    raise ValueError("ledger schema shape differs from declared version")
    digest, counts = history_digest(db)
    return {"version": versions[0][0], "history_sha256": digest, "row_counts": counts,
            "running": db.execute("SELECT count(*) FROM operations WHERE state='RUNNING'").fetchone()[0]}


def upgrade_ledger(path: str | Path, *, apply: bool = False, backup_dir: str | Path | None = None):
    """Dry-run on a memory copy; apply requires exclusive ownership and a backup.

    干跑仅改内存副本；真正升级必须独占账本并先备份。
    """
    from embodied.runtime.operations import take_ledger_lock

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
        if before["version"] == 2:
            target.rollback()
            return {"applied": False, "already_current": True, "before": before, "after": before}
        if before["running"]:
            raise ValueError("cannot upgrade with running operations")
        if apply:
            folder = Path(backup_dir).resolve() if backup_dir else path.parent / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder / (path.name + ".v1-" + uuid4().hex + ".sqlite3")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader, closing(sqlite3.connect(backup)) as copy:
                reader.backup(copy)
        for statement in TASK_DDL:
            target.execute(statement)
        target.execute("UPDATE schema_version SET version=2")
        after = inspect_schema(target)
        if before["history_sha256"] != after["history_sha256"]:
            raise ValueError("history changed during task upgrade")
        target.commit()
        return {"applied": apply, "before": before, "after": after,
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
