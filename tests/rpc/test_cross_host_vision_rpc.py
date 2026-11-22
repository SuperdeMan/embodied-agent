"""End-to-end mTLS RPC tests for cross-host vision clock & per-object freshness propagation (D036).

端到端 BoringSSL mTLS RPC 测试：验证 PerceivedSim 不泄漏仿真真值、跨机时钟偏斜拒止、
遮挡或过期对象导致远端效果验证器返回 UNKNOWN 并阻断依赖，以及隔离物理 + ColorBlob 感知真栈闭环（D036）。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

OPENSSL = shutil.which("openssl")
if OPENSSL is None:
    pytest.skip("openssl binary unavailable for mTLS fixtures", allow_module_level=True)

from embodied.cognition.executor import DagExecutor  # noqa: E402
from embodied.cognition.perception import ObjectSpec, PerceivedSim  # noqa: E402
from embodied.cognition.plan import Plan, PlanContext, Step, StepStatus  # noqa: E402
from embodied.cognition.verify import SAT, UNKNOWN, eval_state_predicate  # noqa: E402
from embodied.contracts.object_state import object_available  # noqa: E402
from embodied.contracts.observation import ClockAdmissionPolicy, ClockMetadata  # noqa: E402
from embodied.contracts.rpc_auth import RpcAccessPolicy, RpcTlsPaths  # noqa: E402
from embodied.control.camera import CameraModel  # noqa: E402
from embodied.control.hal import Observation, Pose  # noqa: E402
from embodied.control.service import ControlState, serve  # noqa: E402
from embodied.runtime.rpc_auth import build_client_channel, cert_fingerprint_sha256  # noqa: E402
from embodied.skills.manifest import SkillManifest  # noqa: E402
from embodied.skills.registry import SkillRegistry, SkillResult  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402


def _make_pki(root: Path) -> tuple[dict[str, RpcTlsPaths], RpcAccessPolicy]:
    """Generate ephemeral ECDSA CA and pinned control/agent certs for mTLS tests.

    生成测试专用临时 ECDSA CA 与固定指纹的 control/agent 双向 TLS 证书。
    """
    ca_key, ca_cert = root / "ca.key", root / "ca.crt"
    subprocess.run(
        [
            OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "1", "-subj", "/CN=embodied-d036-ca",
        ],
        check=True,
        capture_output=True,
    )

    def issue(name: str, san: str) -> RpcTlsPaths:
        key, csr, cert, ext = root / f"{name}.key", root / f"{name}.csr", root / f"{name}.crt", root / f"{name}.ext"
        ext.write_text(f"subjectAltName={san}\nbasicConstraints=CA:FALSE\n", encoding="utf-8")
        subprocess.run(
            [
                OPENSSL, "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                OPENSSL, "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
                "-CAcreateserial", "-out", str(cert), "-days", "1", "-extfile", str(ext),
            ],
            check=True,
            capture_output=True,
        )
        return RpcTlsPaths(ca_cert=str(ca_cert), cert=str(cert), key=str(key))

    tls = {
        "control": issue("control", "DNS:localhost,IP:127.0.0.1"),
        "agent": issue("agent", "URI:spiffe://embodied.local/role/agent"),
    }
    policy = RpcAccessPolicy(
        ca_cert_sha256=cert_fingerprint_sha256(ca_cert.read_bytes()),
        peers=(
            {
                "name": "agent-core",
                "role": "agent",
                "cert_sha256": cert_fingerprint_sha256(Path(tls["agent"].cert).read_bytes()),
                "spiffe_id": "spiffe://embodied.local/role/agent",
            },
        ),
    )
    return tls, policy


class _MockPerceivedBody:
    """Minimal deterministic body with distinct truth vs perceived poses for RPC freshness testing.

    最小确定性仿真夹具：真值位姿与感知位姿严格区分，用于验证 RPC 绝不泄漏底层真值。
    """

    def __init__(self) -> None:
        self.t = 1.0
        self.epoch = "scene-1"
        self.ee = (0.0, 0.0, 0.1)
        self.grip = 0.7
        self.cam = CameraModel(
            10, 10, 10.0, 10.0, 5.0, 5.0,
            (0.0, 0.0, 1.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        )

    def read(self) -> Observation:
        # 故意把真值方块放在盒子内，检验遮挡或感知过期时远端是否会误读真值。
        # Place ground-truth cube inside bin_region to prove occlusion/expiry never falls back to truth.
        return Observation(
            t=self.t,
            qpos=(0.0,),
            qvel=(0.0,),
            gripper_opening=self.grip,
            ee_pose=Pose(self.ee),
            objects={"obj_cube": Pose((0.0, 0.0, 0.1))},
            extras={"regions": {"bin_region": {"center": (0.0, 0.0, 0.1), "half": (0.1, 0.1, 0.1)}}},
            reference=SimpleNamespace(controller_boot_id="boot-1", control_epoch="ctrl-1", scene_epoch=self.epoch),
        )

    def read_reference_state(self) -> tuple[str, Observation]:
        return self.epoch, self.read()

    def camera_model(self, *args, **kwargs):
        return self.cam

    def render_rgbd(self, *args):
        return np.zeros((10, 10, 3)), np.ones((10, 10))


class _BlinkingPipeline:
    objects = {"obj_cube": ObjectSpec("cube")}

    def __init__(self) -> None:
        self.visible = True
        self.pose = Pose((0.0, 0.0, 0.1))

    def locate(self, *args, **kwargs):
        return {"obj_cube": self.pose} if self.visible else {}


async def test_mtls_rpc_perceived_object_expiry_blocks_dependent_steps_without_truth_leak(tmp_path):
    """PerceivedSim.read_reference_state propagates per-object freshness over mTLS RPC and blocks on expiry.

    PerceivedSim.read_reference_state 经 mTLS RPC 传递逐对象感知鲜度；遮挡并超过 TTL 后对象消失、
    不回退底层真值，远端验证器返回 UNKNOWN 并阻断依赖步骤。
    """
    tls, policy = _make_pki(tmp_path)
    body = _MockPerceivedBody()
    pipeline = _BlinkingPipeline()
    # 故意设置偏移的远端生产单调时钟，证明跨进程不比较两端原始 monotonic。
    # Offset producer monotonic by +500,000 s to prove cross-host admission never compares raw monotonic values.
    producer_clock = [500_000.0]
    wrapped = PerceivedSim(
        body,
        pipeline,
        cameras=("cam0",),
        min_interval_s=0.05,
        object_ttl_sim_s=1.0,
        object_ttl_wall_s=0.4,
    )
    wrapped._clock = lambda: producer_clock[0]
    wrapped._render_rgbd = body.render_rgbd

    registry = SkillRegistry(robot_id="sim.mujoco.so_arm100")
    calls: list[str] = []

    async def act():
        calls.append("act")
        return SkillResult(ok=True)

    verification = {
        "mode": "state_predicate",
        "timeout_ms": 120,
        "expect": {"predicate": "object_in_region", "args": {"object": "obj_cube", "region": "bin_region"}},
    }
    registry.register(SkillManifest(name="skill.test.place", description="place", verification=verification), act)
    registry.register(SkillManifest(name="skill.test.next", description="next"), act)

    cstate = ControlState(
        registry,
        world_reader=wrapped.read_reference_state,
        host_id="host-control-edge",
    )
    server, port = await serve(cstate, port=0, host="127.0.0.1", tls=tls["control"], policy=policy)
    channel = build_client_channel(f"127.0.0.1:{port}", tls["agent"], server_name="localhost")
    try:
        remote = await RemoteSkillRegistry(
            channel,
            clock_policy=ClockAdmissionPolicy(consumer_host_id="host-agent-cloud", max_age_s=1.0),
        ).connect()

        # 1. 目标可见且新鲜 -> 远端快照携带重基准对象证据，空间谓词 SAT。
        # Visible and fresh -> remote snapshot carries rebased evidence and evaluates SAT.
        snap_fresh = await remote.world_snapshot()
        assert snap_fresh is not None
        assert "obj_cube" in snap_fresh.objects
        assert snap_fresh.object_evidence is not None
        assert snap_fresh.object_evidence["obj_cube"].captured_monotonic < 400_000.0  # 已重基准到本地时钟 / rebased locally
        assert eval_state_predicate(verification["expect"], snap_fresh) == SAT

        # 2. 遮挡后推进生产端墙钟 0.25s（尚未超 0.40s TTL）-> 仍可见但剩余 TTL 收紧至 <= 0.15s。
        # Occluded + 0.25s producer wall age (< 0.40s TTL) -> still visible with tightened remaining TTL <= 0.15s.
        pipeline.visible = False
        producer_clock[0] += 0.25
        snap_aging = await remote.world_snapshot()
        assert snap_aging is not None
        assert "obj_cube" in snap_aging.objects
        assert snap_aging.object_evidence is not None
        assert snap_aging.object_evidence["obj_cube"].ttl_wall_s <= 0.155

        # 3. 推进至 0.45s（超过 0.40s TTL）-> 即使底层真值仍在盒子内，远端对象消失且 DAG 后继被阻断。
        # Advance past 0.40s TTL -> despite ground truth still in bin_region, object vanishes and DAG blocks dependent step.
        producer_clock[0] += 0.20
        snap_expired = await remote.world_snapshot()
        assert snap_expired is not None
        assert "obj_cube" not in snap_expired.objects
        assert not object_available(snap_expired, "obj_cube")
        assert eval_state_predicate(verification["expect"], snap_expired) == UNKNOWN
        assert "obj_cube" not in await remote.world_context()

        executor = DagExecutor(remote, world_fn=remote.world_snapshot)
        plan = Plan(
            steps=[
                Step(id="s1", skill="skill.test.place", verification=verification),
                Step(id="s2", skill="skill.test.next", depends_on=["s1"]),
            ]
        )
        results = [r async for r in executor.run(plan, PlanContext())]
        assert results[0].data["_verify"]["verdict"] == UNKNOWN
        assert results[1].status == StepStatus.SKIPPED
        assert len(calls) == 1
    finally:
        await channel.close()
        await server.stop(None)
        registry.close()


async def test_mtls_rpc_rejects_skewed_wall_ntp_and_cross_host_monotonic(tmp_path):
    """Real mTLS ControlService publishing skewed wall_ntp or cross-host local_monotonic is rejected by receiver.

    真实 mTLS ControlService 发布偏斜 wall_ntp 或跨机 local_monotonic 时，接收端立即拒收并返回 None。
    """
    tls, policy = _make_pki(tmp_path)
    body = _MockPerceivedBody()
    registry = SkillRegistry(robot_id="sim.mujoco.so_arm100")

    mode = ["skewed_ntp"]

    def clock_provider() -> ClockMetadata:
        now_unix = time.time()
        if mode[0] == "skewed_ntp":
            # 声称偏斜上限 20ms，但发布墙钟实际滞后 300ms -> Cristian 中点反查必拒。
            # Claims 20ms skew bound while publishing 300ms behind -> Cristian midpoint check rejects.
            return ClockMetadata(
                clock_domain="wall_ntp",
                clock_id="ntp-skewed",
                host_id="host-control-edge",
                sync_status="synchronized",
                max_clock_skew_s=0.02,
                source_unix_s=now_unix - 0.31,
                publish_unix_s=now_unix - 0.30,
                measurement_source="ntp_declared",
            )
        if mode[0] == "remote_mono":
            return ClockMetadata(
                clock_domain="local_monotonic",
                clock_id="mono-edge",
                host_id="host-control-edge",
                measurement_source="local_os_monotonic",
            )
        return ClockMetadata(
            clock_domain="wall_ntp",
            clock_id="ntp-healthy",
            host_id="host-control-edge",
            sync_status="synchronized",
            max_clock_skew_s=0.05,
            source_unix_s=now_unix - 0.005,
            publish_unix_s=now_unix,
            measurement_source="ntp_declared",
        )

    cstate = ControlState(
        registry,
        world_reader=body.read_reference_state,
        host_id="host-control-edge",
        clock_provider=clock_provider,
    )
    server, port = await serve(cstate, port=0, host="127.0.0.1", tls=tls["control"], policy=policy)
    channel = build_client_channel(f"127.0.0.1:{port}", tls["agent"], server_name="localhost")
    try:
        remote = await RemoteSkillRegistry(
            channel,
            clock_policy=ClockAdmissionPolicy(
                consumer_host_id="host-agent-cloud",
                max_age_s=1.0,
                max_allowed_skew_s=0.05,
            ),
        ).connect()

        assert await remote.world_snapshot() is None
        assert remote._snapshot_admission.last_rejection_reason == "clock_skew_violation_detected"

        mode[0] = "remote_mono"
        assert await remote.world_snapshot() is None
        assert remote._snapshot_admission.last_rejection_reason == "cross_host_monotonic_forbidden"

        mode[0] = "healthy_ntp"
        snap = await remote.world_snapshot()
        assert snap is not None
        assert "obj_cube" in snap.objects
    finally:
        await channel.close()
        await server.stop(None)
        registry.close()


async def test_mtls_rpc_isolated_sim_and_color_perception_world_state(tmp_path):
    """Real IsolatedTabletopSim + PerceivedSim(ColorBlobProvider) serves verified perceived world state over mTLS RPC.

    真实隔离 MuJoCo 物理子进程 + ColorBlob 感知管线经 BoringSSL mTLS RPC 发布带鲜度证据的世界状态。
    """
    pytest.importorskip("mujoco")
    from embodied.cognition.perception import TABLETOP_OBJECTS, PerceptionPipeline
    from embodied.control.isolated_sim import IsolatedTabletopSim
    from embodied.control.simassets import find_menagerie_model
    from embodied.providers.perception import ColorBlobProvider

    try:
        find_menagerie_model()
    except FileNotFoundError:
        pytest.skip("menagerie assets not fetched")

    tls, policy = _make_pki(tmp_path)
    iso = IsolatedTabletopSim(seed=0)
    perceived = PerceivedSim(
        iso,
        PerceptionPipeline(ColorBlobProvider(), dict(TABLETOP_OBJECTS)),
        min_interval_s=0.25,
    )
    registry = SkillRegistry(robot_id=iso.spec().embodiment_id)
    # 预先热身离屏渲染子进程，避免首帧 GL 初始化挤占 1s 观察窗。
    # Warm up offscreen render worker once before RPC so initial GL init does not consume the 1s observation lane budget.
    warm_obs = perceived.read()
    assert "obj_cube" in warm_obs.objects
    assert warm_obs.object_evidence is not None

    cstate = ControlState(
        registry,
        perceived,
        world_reader=perceived.read_reference_state,
        host_id="host-control-sim",
    )
    server, port = await serve(cstate, port=0, host="127.0.0.1", tls=tls["control"], policy=policy)
    channel = build_client_channel(f"127.0.0.1:{port}", tls["agent"], server_name="localhost")
    try:
        remote = await RemoteSkillRegistry(
            channel,
            clock_policy=ClockAdmissionPolicy(consumer_host_id="host-agent-remote", max_age_s=1.0),
        ).connect()
        snap = await remote.world_snapshot()
        assert snap is not None
        assert "obj_cube" in snap.objects
        assert "bin_region" in snap.regions
        assert snap.object_evidence is not None
        assert snap.object_context is not None
        assert object_available(snap, "obj_cube") is True
        # 验证感知位姿接近仿真真值，且使用的是感知源而非穿透真值。
        # Perceived remote pose matches ground truth within ~2 cm and carries semantic evidence.
        truth = perceived.read_truth()
        dist = float(np.linalg.norm(np.asarray(snap.objects["obj_cube"].pos) - np.asarray(truth.objects["obj_cube"].pos)))
        assert dist < 0.03
        assert snap.object_evidence["obj_cube"].source == "vision"
        assert "obj_cube@" in await remote.world_context()

        # 场景重置后旧 epoch 退役，远端 RPC 获取新场景感知位姿与新代次证据。
        # Scene reset retires the prior epoch and serves fresh perceived pose/evidence on the new epoch.
        old_scene_epoch = snap.object_context.scene_epoch
        perceived.reset(randomize=True)
        snap_after = await remote.world_snapshot()
        assert snap_after is not None
        assert "obj_cube" in snap_after.objects
        assert snap_after.object_context is not None
        assert snap_after.object_context.scene_epoch != old_scene_epoch
        truth_after = perceived.read_truth()
        dist_after = float(
            np.linalg.norm(np.asarray(snap_after.objects["obj_cube"].pos) - np.asarray(truth_after.objects["obj_cube"].pos))
        )
        assert dist_after < 0.03
    finally:
        await channel.close()
        await server.stop(None)
        await registry.drain_compute()
        registry.close()
        perceived.close()
