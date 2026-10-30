"""Queued and detached workers remain accountable. 排队与游离线程仍受退出跟踪约束。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from embodied.contracts.operation import OperationIdentity
from embodied.runtime.invocation import run_blocking
from embodied.skills.manifest import SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult


async def test_cancelled_queued_worker_does_not_run_and_eventually_drains():
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    entered = asyncio.Event()
    release = threading.Event()
    called = []

    def occupy():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    occupied = loop.run_in_executor(None, occupy)
    await entered.wait()

    async def action():
        return await run_blocking(lambda: called.append(True) or SkillResult(ok=True))

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.queue", description="queue",
                                    termination=TerminationSpec(timeout_s=0.03)), action, tracked_workers=True)
    try:
        result = await registry.invoke("skill.test.queue", operation=OperationIdentity(task_id="task", operation_id="op"))
        assert not result.ok and result.invocation_state == "OUTCOME_UNKNOWN"
        assert not registry._stop_contexts["op"].drained
        release.set()
        await occupied
        for _ in range(100):
            if registry._stop_contexts["op"].drained:
                break
            await asyncio.sleep(0.01)
        assert registry._stop_contexts["op"].drained and not called
        registry.close()
    finally:
        release.set()
        executor.shutdown(wait=True)


async def test_returning_before_detached_tracked_work_is_unknown():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    workers = []

    def worker():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return None

    async def action():
        workers.append(asyncio.create_task(run_blocking(worker)))
        await entered.wait()
        return SkillResult(ok=True)

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.detach", description="detach"), action, tracked_workers=True)
    try:
        result = await registry.invoke("skill.test.detach", operation=OperationIdentity(task_id="task", operation_id="op"))
        assert not result.ok and result.invocation_state == "OUTCOME_UNKNOWN"
        release.set()
        await asyncio.gather(*workers)
        assert registry._stop_contexts["op"].drained
        registry.close()
    finally:
        release.set()
