"""A late worker must never commit a write after its invocation has ended.

调用结束后迟到工作线程不得再提交写入。
"""

import asyncio
import threading

import pytest

from embodied.control.hal import ActionCommand
from embodied.runtime.invocation import invocation_access
from embodied.skills.manifest import SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult


@pytest.mark.parametrize("cancel", [False, True])
async def test_thread_loses_authority_on_timeout_or_cancel(cancel):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    writes = []

    def worker():
        started.set()
        try:
            assert release.wait(5)
            with invocation_access() as allowed:
                if allowed:
                    writes.append("late write")
            return SkillResult(ok=True)
        finally:
            finished.set()

    async def handler():
        return await asyncio.to_thread(worker)

    registry = SkillRegistry()
    registry.register(SkillManifest(
        name="skill.test.slow", description="slow",
        termination=TerminationSpec(timeout_s=5 if cancel else 0.05),
    ), handler)
    task = asyncio.create_task(registry.invoke("skill.test.slow"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert not result.ok
            assert result.data["_execution"]["stop_state"] == "STOP_UNCONFIRMED"
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert not writes
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)


async def test_mujoco_driver_denies_late_worker_write_and_step():
    pytest.importorskip("mujoco")
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.control.simassets import find_menagerie_model

    try:
        find_menagerie_model()
    except FileNotFoundError:
        pytest.skip("menagerie assets not fetched")
    sim = TabletopSim(seed=0)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcomes = []

    def worker():
        command = ActionCommand(joint_targets=sim.read().qpos)
        started.set()
        try:
            assert release.wait(5)
            outcomes.append(sim.write(command))
            sim.step(10)
            return SkillResult(ok=True)
        finally:
            finished.set()

    async def handler():
        return await asyncio.to_thread(worker)

    registry = SkillRegistry()
    registry.register(SkillManifest(
        name="skill.test.slow", description="slow", termination=TerminationSpec(timeout_s=0.05),
    ), handler)
    task = asyncio.create_task(registry.invoke("skill.test.slow"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        result = await task
        assert not result.ok
        before = sim.read().t
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert len(outcomes) == 1 and not outcomes[0].applied
        assert sim.read().t == before
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)
