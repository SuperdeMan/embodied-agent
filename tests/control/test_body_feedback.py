"""Body-only motion must not consume semantic sampling time. 本体运动不消耗语义采样时间。"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("mujoco")

from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.scripted import motions  # noqa: E402
from embodied.skills.scripted.manip import _jaw_x_world  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


@pytest.mark.parametrize("expired", [False, True])
def test_body_feedback_skips_perception_but_expired_targets_still_block(expired):
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)
    registry.configure_execution_controller(sim.action_controller)
    reads = []

    class BodyOnly:
        def __getattr__(self, name):
            return getattr(sim, name)

        def read(self):
            raise AssertionError("body motion unexpectedly started perception")

        def read_control_feedback(self):
            reads.append(1)
            result = sim.read_control_feedback()
            assert not result.objects
            return result

        def validate_object_observation(self, observation):
            return not expired

    wrapped = BodyOnly()
    t = sim.data.time
    controls = sim.data.ctrl.copy()

    def handler():
        observation = SimpleNamespace(object_evidence={"object": "expired"}) if expired else None
        result = motions.move_to_q(wrapped, sim.home_qpos(), seconds=0.02, object_observation=observation)
        assert result == (not expired)
        if not expired:
            assert motions.set_gripper(wrapped, 0.5, seconds=0.02)
            assert np.isfinite(_jaw_x_world(wrapped)).all()
        return SkillResult(ok=result)

    try:
        manifest = SkillManifest(name="skill.test.body", description="body", resources=["arm", "gripper", "workspace"])
        result = registry.invoke_operator(manifest, handler)
        assert reads
        if expired:
            assert not result.ok and result.invocation_state == "OUTCOME_UNKNOWN"
            assert sim.data.time == t
            np.testing.assert_array_equal(sim.data.ctrl, controls)
        else:
            assert result.ok
    finally:
        registry.close()
        sim.close()
