"""Remote effect verification over loopback RPC. 本机 RPC 上的远端效果验证。"""

import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.cognition.executor import DagExecutor  # noqa: E402
from embodied.cognition.plan import Plan, PlanContext, Step, StepStatus  # noqa: E402
from embodied.control.hal import Observation, Pose  # noqa: E402
from embodied.control.service import ControlState, serve  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402


@pytest.mark.parametrize("available", [True, False])
async def test_remote_state_releases_dependencies_only_with_effect_evidence(available):
    registry = SkillRegistry(robot_id="sim")
    calls = []

    async def act():
        calls.append("act")
        return SkillResult(ok=True)

    verification = {"mode": "state_predicate", "timeout_ms": 100,
                    "expect": {"predicate": "gripper_holding", "args": {"object": "cube"}}}
    registry.register(SkillManifest(name="skill.test.act", description="act", verification=verification), act)
    registry.register(SkillManifest(name="skill.test.next", description="next"), act)

    def read():
        return "scene", Observation(t=1., qpos=(0.,), qvel=(0.,), gripper_opening=0.,
                                    ee_pose=Pose((0., 0., 0.1)), objects={"cube": Pose((0., 0., 0.1))})

    state = ControlState(registry, world_reader=read if available else None)
    server, port = await serve(state, port=0)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        remote = await RemoteSkillRegistry(channel).connect()
        executor = DagExecutor(remote, world_fn=remote.world_snapshot)
        plan = Plan(steps=[Step(id="pick", skill="skill.test.act", verification=verification),
                           Step(id="next", skill="skill.test.next", depends_on=["pick"])])
        results = [r async for r in executor.run(plan, PlanContext())]
        assert results[-1].status == (StepStatus.OK if available else StepStatus.SKIPPED)
        assert len(calls) == (2 if available else 1)
        if available:
            assert "simulated world" in await remote.world_context()
            state.world_reader = None
            assert await remote.world_snapshot() is None
        else:
            assert results[0].data["_verify"]["verdict"] == "unknown"
    finally:
        await channel.close()
        await server.stop(None)
        registry.close()
