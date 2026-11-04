"""Semantic expiry blocks actual action admission. 语义过期阻断真实动作准入。"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("mujoco")

from embodied.cognition.perception import ObjectSpec, PerceivedSim  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.hal import Pose  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.policies.runner import PolicyRunner  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted.manip import _pick_sync  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


@pytest.mark.parametrize("fault", ["slow_inference", "chunk_age", "calibration_change", "scene_change", "slow_target_solve"])
def test_policy_keeps_semantic_input_identity_through_inference_and_chunk(fault, monkeypatch):
    sim = TabletopSim(seed=42, isolated_render=False)
    runtime = SkillRegistry(robot_id=sim.spec().embodiment_id)
    runtime.configure_execution_controller(sim.action_controller)
    pipeline = SimpleNamespace(objects={"cube": ObjectSpec("cube")},
                               locate=lambda *a, **kw: {"cube": Pose((0.1, -0.2, 0.015))})
    wrapped = PerceivedSim(sim, pipeline, object_ttl_wall_s=0.1, object_ttl_sim_s=0.03)
    wrapped._render_rgbd = lambda *a: (None, None)
    now = [wrapped._clock()]
    wrapped._clock = lambda: now[0]
    # 生产方与消费方共享受控测试时钟，不把机器调度耗时混入故障预算。
    # Share the controlled clock between producer and consumer; exclude host scheduling jitter.
    monkeypatch.setattr("embodied.contracts.object_state.time", SimpleNamespace(monotonic=lambda: now[0]))
    old_t, old_ctrl = sim.data.time, sim.data.ctrl.copy()

    class Policy:
        meta = SimpleNamespace(action_dim=len(sim.read().qpos) + 1, control_hz=50, env_dim=7)

        def reset(self):
            pass

        def act(self, state, env):
            if fault == "slow_inference":
                now[0] += 0.3
                wrapped.read()
            elif fault == "calibration_change":
                sim.model.cam_fovy[0] += 1
            elif fault == "scene_change":
                sim.action_controller.invalidate()
            return np.tile(state, (5, 1))

    def handler():
        if fault == "slow_target_solve":
            def solve(target):
                now[0] += 0.3
                return SimpleNamespace(pos_err=0.0, qpos=sim.home_qpos())
            sim.solve_reach = solve
            result = _pick_sync(wrapped, "cube")
            assert not result.ok and "expired during solve" in result.detail
            return SkillResult(ok=True)
        ok, reason = PolicyRunner(wrapped, Policy()).run_sync(done=lambda: False, max_seconds=1)
        assert not ok and "semantic_observation_stale" in reason
        return SkillResult(ok=True)

    try:
        manifest = SkillManifest(name="skill.test.semantic", description="semantic admission probe",
                                 resources=["arm", "gripper", "workspace"])
        result = runtime.invoke_operator(manifest, handler)
        assert result.invocation_state == "OUTCOME_UNKNOWN" and not result.ok
        assert not runtime.arbiter.idle(manifest.resources)
        if fault == "chunk_age":
            assert sim.data.time == pytest.approx(old_t + 0.04)
        else:
            assert sim.data.time == old_t
            np.testing.assert_array_equal(sim.data.ctrl, old_ctrl)
    finally:
        runtime.close()
