"""Render requests are bounded and never queue behind a stuck frame. 渲染请求有界且不在卡住的帧后排队。"""

import asyncio
import threading

import numpy as np
import pytest

from embodied.hri.server import AppContext
from embodied.runtime.invocation import CURRENT_INVOCATION, OBSERVATION_ONLY
from embodied.runtime.observation import ObservationUnavailable
from embodied.skills.registry import SkillRegistry


async def test_frame_timeout_busy_rejection_and_fresh_retry():
    release, entered = threading.Event(), threading.Event()
    calls = []

    class Sim:
        def render(self, camera):
            assert OBSERVATION_ONLY.get() and CURRENT_INVOCATION.get() is None
            calls.append(camera)
            entered.set()
            assert release.wait(2)
            return np.full((8, 8, 3), len(calls) * 80, dtype=np.uint8)

    registry = SkillRegistry()
    ctx = AppContext(Sim(), registry, None, None, None)
    ctx._render_timeout_s = 0.05
    first = asyncio.create_task(ctx.render_jpeg())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(ObservationUnavailable, match="busy"):
            await ctx.render_jpeg()
        with pytest.raises(ObservationUnavailable, match="timeout"):
            await first
        for _ in range(3):
            with pytest.raises(ObservationUnavailable, match="busy"):
                await ctx.render_jpeg()
        assert calls == ["side"] and not ctx._frame_future.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(ctx._frame_future), 1)
    try:
        image = await ctx.render_jpeg()
        assert image[:2] == b"\xff\xd8" and calls == ["side", "side"]
    finally:
        await ctx.close()
        registry.close()


async def test_frame_cancellation_does_not_allow_another_queued_render():
    release, entered = threading.Event(), threading.Event()
    registry = SkillRegistry()

    class Sim:
        def render(self, camera):
            entered.set()
            assert release.wait(2)
            return np.zeros((8, 8, 3), dtype=np.uint8)

    ctx = AppContext(Sim(), registry, None, None, None)
    pending = asyncio.create_task(ctx.render_jpeg())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(ObservationUnavailable, match="busy"):
            await ctx.render_jpeg()
        with pytest.raises(ObservationUnavailable, match="still_running"):
            await ctx.close()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(ctx._frame_future), 1)
        await ctx.close()
        registry.close()
