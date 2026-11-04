"""Console shutdown aborts owned compute without waiting for native sleep. 控制台退出中止自有计算，不等待原生睡眠结束。"""

import asyncio
import sys
import sysconfig
import time
from pathlib import Path

import pytest

from embodied.hri.server import AppContext
from embodied.runtime.compute import ComputeProcess
from embodied.runtime.observation import ObservationUnavailable
from embodied.skills.registry import SkillRegistry


async def test_close_reaps_a_blocked_render_process(tmp_path):
    fixture = Path(__file__).parents[1] / "runtime" / "compute_fixture.py"
    bootstrap = "import sys,site,runpy;p=sys.argv.pop(1);sys.path.insert(0,p);site.addsitedir(p);runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    worker = ComputeProcess({}, timeout_s=10, _command=[getattr(sys, "_base_executable", sys.executable), "-I", "-c",
                            bootstrap, sysconfig.get_path("purelib"), str(fixture)])
    worker.start()

    class Sim:
        def render_jpeg(self, camera):
            return worker.call("gil_hang", {})

        def cancel_render(self):
            worker.request_stop()

        def close_renderers(self):
            worker.close()

    registry = SkillRegistry()
    ctx = AppContext(Sim(), registry, None, None, None)
    ctx._render_timeout_s = 0.05
    try:
        with pytest.raises(ObservationUnavailable, match="render_timeout"):
            await ctx.render_jpeg()
        started = time.monotonic()
        await ctx.close()
        assert time.monotonic() - started < 3 and worker.reaped and worker.state == "CLOSED"
    finally:
        worker.close()
        registry.close()
        await asyncio.sleep(0)
