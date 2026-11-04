"""Cancellation survives unavailable persistence. 持久化不可用时仍完成撤权和取消。"""

import asyncio
import sqlite3

import pytest

from embodied.contracts.operation import OperationIdentity
from embodied.runtime.invocation import CURRENT_INVOCATION
from embodied.runtime.operations import OperationLedger
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult


async def active_pair(path):
    registry = SkillRegistry(ledger=OperationLedger(path))
    permits = []

    async def action():
        permits.append(CURRENT_INVOCATION.get())
        await asyncio.Event().wait()

    tasks = []
    for index in range(2):
        name = f"skill.test.wait{index}"
        registry.register(SkillManifest(name=name, description="wait", resources=[f"arm{index}"]), action)
        tasks.append(asyncio.create_task(registry.invoke(name, operation=OperationIdentity(task_id="task", operation_id=f"op{index}"))))
    async with asyncio.timeout(2):
        while len(permits) < 2:
            await asyncio.sleep(0)
    return registry, permits, tasks


def revoked(permit):
    with permit.access() as allowed:
        return not allowed


@pytest.mark.parametrize("fault", ["read", "write"])
async def test_one_failed_record_does_not_skip_other_cancellations(tmp_path, monkeypatch, fault):
    registry, permits, tasks = await active_pair(tmp_path / "ops.sqlite3")
    point = "get" if fault == "read" else "finish"
    original = getattr(registry.ledger, point)

    def fail_first(operation_id, *args, **kwargs):
        assert all(revoked(p) for p in permits)
        assert all(t.cancelling() for t in tasks)
        if operation_id == "op0":
            raise sqlite3.OperationalError("injected storage failure")
        return original(operation_id, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(registry.ledger, point, fail_first)
            errors = registry.cancel_all()
            assert errors == {"op0": "OperationalError"}
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(r, BaseException) for r in results)
        assert not registry._active and all(revoked(p) for p in permits)
        assert not registry.arbiter.idle(("arm0", "arm1"))
        assert registry.ledger.get("op1").state == "OUTCOME_UNKNOWN"
    finally:
        registry.close()


async def test_single_cancel_still_cancels_task_when_query_fails(tmp_path, monkeypatch):
    registry, permits, tasks = await active_pair(tmp_path / "ops.sqlite3")
    original = registry.ledger.get

    def fail(operation_id):
        if operation_id == "op0":
            raise sqlite3.OperationalError("read unavailable")
        return original(operation_id)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(registry.ledger, "get", fail)
            with pytest.raises(sqlite3.OperationalError):
                registry.cancel_operation("op0")
            assert revoked(permits[0]) and tasks[0].cancelling()
            assert not registry.arbiter.idle(("arm0",))
            registry.cancel_operation("op1")
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        registry.close()


async def test_actual_sqlite_full_preserves_running_rows_for_restart(tmp_path, monkeypatch):
    path = tmp_path / "full.sqlite3"
    registry, permits, tasks = await active_pair(path)
    original = registry.ledger._event
    codes = []
    db = registry.ledger._db
    count = db.execute("PRAGMA page_count").fetchone()[0]
    db.execute(f"PRAGMA max_page_count={count}")

    def oversized(operation_id, attempt_id, kind, payload):
        # 限制本测试数据库页数并扩大事件，触发真实 SQLITE_FULL，不填满宿主磁盘。
        # Cap only this test database and enlarge the event to trigger real SQLITE_FULL, not host disk exhaustion.
        try:
            original(operation_id, attempt_id, kind, {**payload, "fault_padding": "x" * 262144})
        except sqlite3.DatabaseError as exc:
            codes.append(exc.sqlite_errorcode)
            raise

    with monkeypatch.context() as patch:
        patch.setattr(registry.ledger, "_event", oversized)
        errors = registry.cancel_all()
        assert set(errors) == {"op0", "op1"} and all(revoked(p) for p in permits)
        await asyncio.gather(*tasks, return_exceptions=True)
    assert codes and set(codes) == {sqlite3.SQLITE_FULL}
    assert [registry.ledger.get(f"op{i}").state for i in range(2)] == ["RUNNING", "RUNNING"]
    assert not registry.arbiter.idle(("arm0", "arm1"))
    db.execute("PRAGMA max_page_count=1073741823")
    registry.close()
    reopened = OperationLedger(path)
    try:
        assert reopened.quarantined() == {"op0": ("arm0",), "op1": ("arm1",)}
        assert all(reopened.get(f"op{i}").stop_state == "STOP_UNCONFIRMED" for i in range(2))
    finally:
        reopened.close()


async def test_lost_completion_write_never_reexecutes_after_restart(tmp_path):
    path = tmp_path / "completion.sqlite3"
    registry = SkillRegistry(ledger=OperationLedger(path))
    calls = []

    async def action():
        calls.append(1)
        registry.ledger._db.execute("PRAGMA query_only=ON")
        return SkillResult(ok=True)

    manifest = SkillManifest(name="skill.test.action", description="write", resources=["arm"])
    registry.register(manifest, action)
    identity = OperationIdentity(task_id="task", operation_id="op")
    with pytest.raises(sqlite3.OperationalError):
        await registry.invoke(manifest.name, operation=identity)
    assert calls == [1] and registry.ledger.get("op").state == "RUNNING"
    assert not registry.arbiter.idle(("arm",))
    registry.close()
    registry = SkillRegistry(ledger=OperationLedger(path))
    registry.register(manifest, action)
    try:
        result = await registry.invoke(manifest.name, operation=identity)
        assert not result.ok and result.invocation_state == "OUTCOME_UNKNOWN" and calls == [1]
        assert result.stop_state == "STOP_UNCONFIRMED"
    finally:
        registry.close()
