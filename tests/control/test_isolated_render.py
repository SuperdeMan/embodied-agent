"""Render a copy without changing the live body. 渲染副本，不改变活跃本体。"""

import os

import numpy as np
import pytest

pytest.importorskip("mujoco")

from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


def test_render_copy_matches_reference_and_preserves_live_state():
    # 原生对照在独立测试进程中运行，避免共享 GLFW 状态。
    # Run the native reference in a separate test process to avoid sharing GLFW state.
    import subprocess
    import sys

    code = '''
import numpy as np
from embodied.control.drivers.mujoco_sim import TabletopSim
a=TabletopSim(seed=42); b=TabletopSim(seed=42,isolated_render=False)
before=(a.data.qpos.copy(),a.data.qvel.copy(),a.data.ctrl.copy(),a.data.time)
try:
    rgb,depth=a.render_rgbd(width=320,height=240)
    ref,ref_depth=b.render_rgbd(width=320,height=240)
    np.testing.assert_array_equal(rgb,ref)
    np.testing.assert_array_equal(depth,ref_depth)
    jpeg=a.render_jpeg(width=320,height=240)
    assert jpeg.startswith(b'\\xff\\xd8') and jpeg.endswith(b'\\xff\\xd9')
    for actual,old in zip((a.data.qpos,a.data.qvel,a.data.ctrl),before[:3]): np.testing.assert_array_equal(actual,old)
    assert a.data.time==before[3]
    assert a._render_process.metadata['pid']==a._render_process.last_pid
    worker=a._render_process
finally:
    a.close();b.close()
assert worker.reaped
print('render_copy_passed')
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert "render_copy_passed" in result.stdout


def test_renderer_process_is_distinct_and_scene_change_discards_response(monkeypatch):
    sim = TabletopSim()
    sim.prepare_renderer()
    worker = sim._render_process
    assert worker.last_pid != os.getpid()
    original = worker.call

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        sim.state_epoch = "new-scene"
        return result

    monkeypatch.setattr(worker, "call", changed)
    try:
        with pytest.raises(RuntimeError, match="scene_changed"):
            sim.render(width=160, height=120)
    finally:
        sim.close()
    assert worker.reaped


def test_camera_and_shape_checks_happen_before_sending_invalid_data():
    sim = TabletopSim()
    try:
        with pytest.raises(ValueError):
            sim.render(width=99999)
        assert sim._render_process is None
        with pytest.raises(ValueError):
            sim.render("missing", width=160, height=120)
        assert sim._render_process.state == "READY"
        rgb, depth = sim.render_rgbd(width=160, height=120)
        assert rgb.shape == (120, 160, 3) and np.isfinite(depth).all()
    finally:
        sim.close()
