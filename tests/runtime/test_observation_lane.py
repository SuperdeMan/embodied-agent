"""Bounded observation waits track real workers. 有界观察等待持续追踪实际工作线程。"""

import asyncio
import threading
import time

import pytest

from embodied.runtime.invocation import CURRENT_INVOCATION, OBSERVATION_ONLY, InvocationPermit
from embodied.runtime.observation import ObservationLane, ObservationUnavailable


async def drain(lane):
    for _ in range(200):
        if not lane.busy:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("observation worker did not drain")


async def test_timeout_keeps_worker_tracked_without_queue_and_drops_late_result():
    lane = ObservationLane(0.05)
    release = threading.Event()
    seen = []

    def blocked():
        seen.append(1)
        assert release.wait(2)
        return "late"

    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    started = time.monotonic()
    try:
        with pytest.raises(ObservationUnavailable, match="timeout"):
            await lane.call(blocked)
        assert heartbeat.done() and time.monotonic() - started < 0.5
        assert lane.busy
        for _ in range(10):
            with pytest.raises(ObservationUnavailable, match="busy"):
                await lane.call(lambda: seen.append(2))
        assert lane.started == 1 and seen == [1]
    finally:
        release.set()
        await drain(lane)
    assert await lane.call(lambda: "new") == "new"


async def test_cancel_does_not_cancel_actual_worker_or_inherit_action_context():
    lane = ObservationLane()
    entered, release = threading.Event(), threading.Event()
    permit = InvocationPermit(5)

    def blocked():
        assert CURRENT_INVOCATION.get() is None and OBSERVATION_ONLY.get() is True
        entered.set()
        assert release.wait(2)
        raise ValueError("late failure")

    token = CURRENT_INVOCATION.set(permit)
    task = asyncio.create_task(lane.call(blocked))
    CURRENT_INVOCATION.reset(token)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lane.busy
    finally:
        release.set()
        await drain(lane)
    assert await lane.call(lambda: OBSERVATION_ONLY.get()) is True
    assert not OBSERVATION_ONLY.get()


def test_sync_wait_is_bounded_and_keeps_the_same_busy_worker():
    lane = ObservationLane(0.02)
    release = threading.Event()
    try:
        with pytest.raises(ObservationUnavailable, match="timeout"):
            lane.call_sync(lambda: release.wait(1))
        with pytest.raises(ObservationUnavailable, match="busy"):
            lane.call_sync(lambda: 1)
        assert lane.started == 1
    finally:
        release.set()
        lane._future.result(timeout=1)


async def test_completed_worker_remains_pending_until_waiter_consumes_completion():
    lane = ObservationLane()
    release = threading.Event()

    def work():
        assert release.wait(1)
        return 42

    task = asyncio.create_task(lane.call(work))
    await asyncio.sleep(0)
    release.set()
    assert lane._future.result(timeout=1) == 42
    assert lane.busy
    with pytest.raises(ObservationUnavailable, match="busy"):
        lane.call_sync(lambda: "must not start")
    assert await task == 42
    assert not lane.busy


@pytest.mark.parametrize("budget", [0, -1, 2.1, float("inf"), float("nan")])
def test_invalid_observation_budget_rejected(budget):
    with pytest.raises(ValueError):
        ObservationLane(budget)
