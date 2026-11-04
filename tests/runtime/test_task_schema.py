"""Schema upgrade preserves history and fails atomically. 升级保留历史并原子失败。"""

import sqlite3

import pytest

from embodied.runtime.operations import _SCHEMA, LedgerUnavailable, OperationLedger
from embodied.runtime.task_schema import history_digest, upgrade_ledger


def legacy(path):
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
        db.execute("INSERT INTO tasks VALUES('task','robot',1,'OPEN',1,1)")
        db.execute("INSERT INTO operations VALUES('op','task','digest','skill.test.old','RETURNED','{}')")
        db.execute("INSERT INTO attempts VALUES('attempt','op','boot',1,'RETURNED','NOT_REQUESTED','[]',1,2)")
        db.execute("INSERT INTO operation_events VALUES('event','op','attempt','completed','{}',2)")


def test_dry_run_does_not_mutate_original_and_apply_preserves_all_history(tmp_path):
    path = tmp_path / "old.sqlite3"
    legacy(path)
    before = path.read_bytes()
    preview = upgrade_ledger(path)
    assert not preview["applied"] and preview["after"]["version"] == 2
    assert path.read_bytes() == before
    result = upgrade_ledger(path, apply=True)
    assert result["applied"] and result["before"]["history_sha256"] == result["after"]["history_sha256"]
    with sqlite3.connect(result["backup"]) as backup, sqlite3.connect(path) as current:
        assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert current.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert history_digest(backup) == history_digest(current)
    assert upgrade_ledger(path, apply=True)["already_current"]


def test_live_owner_blocks_migration_and_v1_open_never_auto_migrates(tmp_path):
    path = tmp_path / "old.sqlite3"
    legacy(path)
    owner = OperationLedger(path)
    try:
        assert owner.schema_version == 1
        with pytest.raises(LedgerUnavailable, match="already owned"):
            upgrade_ledger(path, apply=True)
    finally:
        owner.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 1


def test_running_operations_and_unknown_versions_rejected(tmp_path):
    path = tmp_path / "old.sqlite3"
    legacy(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE operations SET state='RUNNING'")
    with pytest.raises(ValueError, match="running"):
        upgrade_ledger(path, apply=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        db.execute("UPDATE schema_version SET version=99")
    with pytest.raises(ValueError, match="unsupported"):
        upgrade_ledger(path)


def test_ddl_failure_rolls_back_new_tables_and_version(tmp_path, monkeypatch):
    import embodied.runtime.task_schema as schema

    path = tmp_path / "old.sqlite3"
    legacy(path)
    monkeypatch.setattr(schema, "TASK_DDL", (*schema.TASK_DDL[:1], "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        upgrade_ledger(path, apply=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='task_plans'").fetchone() is None
    ledger = OperationLedger(path)
    try:
        assert ledger.schema_version == 1
    finally:
        ledger.close()
