"""v3 migration and history-preserving rollback. v3 迁移与保留历史的回退。"""

import sqlite3
from contextlib import closing

import pytest

from embodied.contracts.operation import OperationIdentity
from embodied.runtime.operations import OperationLedger
from embodied.runtime.release import backup_ledger, inspect_ledger_state, restore_ledger_backup
from embodied.runtime.task_schema import upgrade_ledger


def make_v2(path):
    ledger = OperationLedger(path)
    op = OperationIdentity.fresh()
    record = ledger.begin(op, "sim.test", "skill.test.move", "original", ("arm",), 1)
    ledger.finish(op.operation_id, "RETURNED", {"ok": True}, "NOT_REQUESTED", attempt_id=record.attempt_id)
    ledger.close()
    return op, record.attempt_id


def test_migration_and_clean_rollback_preserve_original_v2(tmp_path):
    path = tmp_path / "v2.sqlite3"
    op, attempt = make_v2(path)
    before = inspect_ledger_state(path)
    preview = upgrade_ledger(path, target_version=3)
    assert preview["after"]["version"] == 3 and inspect_ledger_state(path) == before
    applied = upgrade_ledger(path, target_version=3, apply=True)
    ledger = OperationLedger(path)
    try:
        row = ledger.get(op.operation_id, attempt)
        assert row.result == {"ok": True} and row.state == "RETURNED"
        assert ledger._db.execute("SELECT v3_dirty FROM schema_version").fetchone()[0] == 0
    finally:
        ledger.close()
    restored = restore_ledger_backup(path, applied["backup"], expected_sha256=applied["backup_sha256"])
    assert restored["version"] == 2 and restored["all_tables_sha256"] == before["all_tables_sha256"]


def test_written_or_corrupted_v3_refuses_v2_recovery_and_restores_authenticated_v3(tmp_path):
    path = tmp_path / "live.sqlite3"
    make_v2(path)
    applied = upgrade_ledger(path, target_version=3, apply=True)
    make_v2(path)
    current = inspect_ledger_state(path)
    backup, manifest = backup_ledger(path, tmp_path / "v3-backups", source_release="v3")
    with pytest.raises(ValueError, match="discard_history"):
        restore_ledger_backup(path, applied["backup"], expected_sha256=applied["backup_sha256"])
    assert inspect_ledger_state(path) == current
    path.write_bytes(b"truncated")
    with pytest.raises(ValueError, match="unproven"):
        restore_ledger_backup(path, applied["backup"], expected_sha256=applied["backup_sha256"])
    result = restore_ledger_backup(path, backup, expected_sha256=manifest.backup_sha256)
    assert result["version"] == 3 and result["all_tables_sha256"] == current["all_tables_sha256"]


def test_failed_migration_keeps_all_v2_rows(tmp_path, monkeypatch):
    from embodied.runtime import task_schema

    path = tmp_path / "original.sqlite3"
    make_v2(path)
    before = inspect_ledger_state(path)
    migrate = task_schema.migrate_attempts

    def interrupted(db, digest):
        migrate(db, digest)
        raise sqlite3.OperationalError("injected interruption")

    monkeypatch.setattr(task_schema, "migrate_attempts", interrupted)
    with pytest.raises(sqlite3.OperationalError, match="injected interruption"):
        upgrade_ledger(path, target_version=3, apply=True)
    assert inspect_ledger_state(path) == before
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='recovery_grants'").fetchall()
