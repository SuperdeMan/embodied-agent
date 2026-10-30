"""Crash and duplicate admission tests. 崩溃与重复准入回归。"""

import asyncio
import json
import sqlite3
import subprocess
import sys

import pytest

from embodied.contracts.operation import OperationIdentity
from embodied.runtime.operations import LedgerUnavailable, OperationConflict, OperationLedger
from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import ConfirmationRequired, SkillRegistry, SkillResult


def identity(operation="op", task="task"):
    return OperationIdentity(task_id=task, operation_id=operation)


def register(registry, handler, *, resources=None, dangerous=False, timeout=1):
    registry.register(SkillManifest(
        name="skill.test.move", description="test", params={"x": ParamSpec(type="integer", default=1)},
        resources=resources or [], require_confirm=dangerous, termination=TerminationSpec(timeout_s=timeout),
    ), handler)


async def test_success_is_durable_and_same_params_can_be_new_operation(tmp_path):
    calls = []

    async def action(**params):
        calls.append(params)
        return SkillResult(ok=True, data={"value": params["x"]})

    path = tmp_path / "ops.sqlite3"
    first = SkillRegistry(ledger=OperationLedger(path))
    register(first, action)
    result = await first.invoke("skill.test.move", operation=identity())
    first.close()
    second = SkillRegistry(ledger=OperationLedger(path))
    register(second, action)
    replay = await second.invoke("skill.test.move", operation=identity())
    assert replay.attempt_id == result.attempt_id and replay.data == {"value": 1}
    assert replay.invocation_state == "RETURNED"
    assert len(calls) == 1
    await second.invoke("skill.test.move", operation=identity("distinct"))
    assert len(calls) == 2
    second.close()


async def test_mismatched_identity_and_confirmation_never_dispatch(tmp_path):
    calls = []

    async def action(**params):
        calls.append(params)
        return SkillResult(ok=True)

    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"))
    register(registry, action, dangerous=True)
    await registry.invoke("skill.test.move", operation=identity(), confirmed=True)
    with pytest.raises(ConfirmationRequired):
        await registry.invoke("skill.test.move", operation=identity())
    for value in (1, "true", "false"):
        with pytest.raises(ConfirmationRequired):
            await registry.invoke("skill.test.move", operation=identity(), confirmed=value)
    with pytest.raises(OperationConflict):
        await registry.invoke("skill.test.move", {"x": 2}, operation=identity(), confirmed=True)
    with pytest.raises(OperationConflict):
        await registry.invoke("skill.test.move", operation=identity(task="other"), confirmed=True)
    assert len(calls) == 1
    registry.close()


async def test_same_operation_cannot_replay_on_a_different_robot(tmp_path):
    async def action(**params):
        return SkillResult(ok=True)

    path = tmp_path / "ops.sqlite3"
    first = SkillRegistry(ledger=OperationLedger(path), robot_id="first")
    register(first, action)
    await first.invoke("skill.test.move", operation=identity())
    first.close()
    second = SkillRegistry(ledger=OperationLedger(path), robot_id="second")
    register(second, action)
    with pytest.raises(OperationConflict):
        await second.invoke("skill.test.move", operation=identity())
    second.close()


async def test_parallel_duplicate_gets_state_without_second_attempt():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def action(**params):
        calls.append(params)
        entered.set()
        await release.wait()
        return SkillResult(ok=True)

    registry = SkillRegistry()
    register(registry, action)
    task = asyncio.create_task(registry.invoke("skill.test.move", operation=identity()))
    await entered.wait()
    duplicate = await registry.invoke("skill.test.move", operation=identity())
    assert not duplicate.ok and duplicate.invocation_state == "RUNNING"
    release.set()
    assert (await task).ok and len(calls) == 1
    registry.close()


async def test_resource_conflict_is_atomic_and_timeout_quarantine_survives_restart(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(**params):
        entered.set()
        await release.wait()
        return SkillResult(ok=True)

    path = tmp_path / "ops.sqlite3"
    registry = SkillRegistry(ledger=OperationLedger(path))
    register(registry, action, resources=["arm", "workspace"], timeout=0.05)
    task = asyncio.create_task(registry.invoke("skill.test.move", operation=identity()))
    await entered.wait()
    conflict = await registry.invoke("skill.test.move", operation=identity("parallel"))
    assert not conflict.ok and conflict.invocation_state == "REJECTED"
    assert registry.get_operation("parallel") is None
    assert (await task).invocation_state == "OUTCOME_UNKNOWN"
    registry.close()
    reopened = SkillRegistry(ledger=OperationLedger(path))
    register(reopened, action, resources=["arm", "workspace"])
    result = await reopened.invoke("skill.test.move", operation=identity("after-restart"))
    assert not result.ok and "resource_unavailable" in result.detail
    assert reopened.get_operation("op").stop_state == "STOP_UNCONFIRMED"
    reopened.close()


async def test_cancel_does_not_claim_stop_or_allow_new_attempt():
    entered = asyncio.Event()

    async def action(**params):
        entered.set()
        await asyncio.Event().wait()

    registry = SkillRegistry()
    register(registry, action, resources=["arm"])
    task = asyncio.create_task(registry.invoke("skill.test.move", operation=identity()))
    await entered.wait()
    record = registry.cancel_operation("op")
    assert record.state == "OUTCOME_UNKNOWN" and record.stop_state == "STOP_UNCONFIRMED"
    with pytest.raises(asyncio.CancelledError):
        await task
    replay = await registry.invoke("skill.test.move", operation=identity())
    assert not replay.ok
    registry.close()


def test_live_owner_blocks_second_open_without_recovery(tmp_path):
    path = tmp_path / "ops.sqlite3"
    ledger = OperationLedger(path)
    ledger.begin(identity(), "robot", "skill.test.move", "digest", ("arm",), 1)
    with pytest.raises(LedgerUnavailable, match="already owned"):
        OperationLedger(path)
    assert ledger.get("op").state == "RUNNING"
    ledger.close()


def test_unknown_schema_refused_without_migration(tmp_path):
    path = tmp_path / "ops.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER)")
        db.execute("INSERT INTO schema_version VALUES(99)")
    before = path.read_bytes()
    with pytest.raises(LedgerUnavailable, match="unsupported"):
        OperationLedger(path)
    assert path.read_bytes() == before


def test_process_crash_records_unknown_and_never_claims_stopped(tmp_path):
    path = tmp_path / "ops.sqlite3"
    code = """
import os, sys
from embodied.runtime.operations import OperationLedger
from embodied.contracts.operation import OperationIdentity
ledger = OperationLedger(sys.argv[1])
ledger.begin(OperationIdentity(task_id='task',operation_id='op'), 'robot', 'skill.test.move', 'digest', ('arm',), 1)
os._exit(17)
"""
    run = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, timeout=20)
    assert run.returncode == 17, run.stderr.decode(errors="replace")
    ledger = OperationLedger(path)
    record = ledger.get("op")
    assert record.state == "OUTCOME_UNKNOWN" and record.stop_state == "STOP_UNCONFIRMED"
    assert record.boot_id != ledger.boot_id
    assert ledger.quarantined() == {"op": ("arm",)}
    ledger.close()
    with sqlite3.connect(path) as db:
        events = db.execute("SELECT event_type,payload_json FROM operation_events ORDER BY created_at").fetchall()
    assert [event[0] for event in events] == ["admitted", "recovered_unknown"]
    assert json.loads(events[-1][1])["state"] == "OUTCOME_UNKNOWN"


async def test_record_failure_prevents_dispatch(tmp_path, monkeypatch):
    async def action(**params):
        raise AssertionError("must not dispatch")

    registry = SkillRegistry(ledger=OperationLedger(tmp_path / "ops.sqlite3"))
    register(registry, action, resources=["arm"])

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(registry.ledger, "begin", broken)
    with pytest.raises(sqlite3.OperationalError):
        await registry.invoke("skill.test.move")
    registry.close()


async def test_unserializable_result_is_unknown_not_success():
    async def action(**params):
        return SkillResult(ok=True, data={"bad": float("nan")})

    registry = SkillRegistry()
    register(registry, action, resources=["arm"])
    with pytest.raises(ValueError):
        await registry.invoke("skill.test.move", operation=identity())
    assert registry.get_operation("op").state == "OUTCOME_UNKNOWN"
    registry.close()
