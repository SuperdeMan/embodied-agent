"""Compute death cannot produce actions or replay an operation. 计算进程死亡不能产生动作或重放操作。"""

import pytest

pytest.importorskip("mujoco")

from embodied.contracts.operation import OperationIdentity  # noqa: E402
from embodied.control.drivers.mujoco_sim import TabletopSim  # noqa: E402
from embodied.control.simassets import find_menagerie_model  # noqa: E402
from embodied.providers.policy import BasePolicyProvider, PolicyMeta  # noqa: E402
from embodied.runtime.compute import ComputeProcess  # noqa: E402
from embodied.skills.policies.runner import register_policy_sim_skills  # noqa: E402
from embodied.skills.registry import SkillRegistry  # noqa: E402
from embodied.skills.scripted.manip import register_sim_skills  # noqa: E402

try:
    find_menagerie_model()
except FileNotFoundError:
    pytest.skip("menagerie assets not fetched", allow_module_level=True)


async def test_process_death_is_unknown_and_requires_stop_before_explicit_recovery():
    sim = TabletopSim()
    registry = SkillRegistry(robot_id=sim.spec().embodiment_id)

    class DyingPolicy(BasePolicyProvider):
        meta = PolicyMeta(chunk_size=1, control_hz=50.0, state_dim=6, env_dim=7, action_dim=6)

        def __init__(self):
            self.worker = ComputeProcess({"backend": "color"})
            self.worker.start()
            self.calls = 0

        def act(self, state, env):
            self.calls += 1
            self.worker._process.kill()
            return self.worker.call("detect", {})

        def close(self):
            self.worker.close()

        def restart(self):
            self.worker.restart()

    policy = DyingPolicy()
    register_sim_skills(registry, sim, skip=("skill.manip.pick",))
    register_policy_sim_skills(registry, sim, pick=policy)
    identity = OperationIdentity(task_id="task", operation_id="crashed-pick")
    ctrl, t, boot = sim.data.ctrl.copy(), sim.data.time, policy.worker.boot_id
    try:
        result = await registry.invoke("skill.manip.pick", operation=identity)
        assert not result.ok and result.invocation_state == "OUTCOME_UNKNOWN"
        assert policy.worker.reaped and policy.worker.state == "FAILED"
        assert sim.data.time == t and (sim.data.ctrl == ctrl).all()
        with pytest.raises(RuntimeError, match="not_idle"):
            registry.restart_compute()
        await registry.reconcile_stop(result.operation_id, result.attempt_id, confirmed=True)
        registry.restart_compute()
        assert policy.worker.boot_id != boot and policy.calls == 1
        replay = await registry.invoke("skill.manip.pick", operation=identity)
        assert not replay.ok and policy.calls == 1
        assert (await registry.invoke("skill.arm.home")).ok
    finally:
        registry.close()
        sim.close()
