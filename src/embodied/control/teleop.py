"""Teleoperation channel v0: incremental commands -> IK -> guarded writes -> episodes.

The session is INPUT-AGNOSTIC: jog/gripper/episode-mark commands in, guarded motion
plus recording out. The terminal keyboard frontend lives in the CLI; a gamepad or
the real leader arm (M3) will drive this same path — that seam is the point of the
roadmap's teleop task. Recording is default ON (architecture §4.6: every run is
data); episodes carry ``extra_meta.collector = "teleop/v1"`` so datasets can keep
human demos separate from scripted-expert ones (D014 conversion works unchanged).

Safety: every motion goes through the same guarded write path as skills — the
fence pre-check refuses out-of-bounds jog targets BEFORE any motion, and a guard
veto mid-motion aborts the jog (never fought, same rule as motions.py).


输入无关遥操作将增量命令经逆解和 Guard 执行并录制；键盘、手柄或未来主臂共用链路。默认录制并标明人工来源；越界预检拒绝，运动中 Guard 否决即中止。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from embodied.contracts.operation import OperationIdentity
from embodied.data_engine.recorder import EpisodeRecorder, EpisodeWriter
from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult
from embodied.skills.scripted import motions

TELEOP_COLLECTOR_ID = "teleop/v1"

# 开度与脚本抓取保持一致，保证示教数据一致。
GRIP_OPEN = 0.6  # matches the scripted skills' grasp opening: consistent demo data
GRIP_CLOSED = 0.0

_RESOURCES = ["arm", "gripper", "workspace"]
_JOG = SkillManifest(name="skill.teleop.jog", description="Operator Cartesian jog", resources=_RESOURCES,
                     params={axis: ParamSpec(type="number") for axis in ("dx", "dy", "dz")},
                     termination=TerminationSpec(timeout_s=5.0))
_GRIP = SkillManifest(name="skill.teleop.gripper", description="Operator gripper target", resources=_RESOURCES,
                      params={"opening": ParamSpec(type="number")}, termination=TerminationSpec(timeout_s=5.0))
_TOGGLE = SkillManifest(name="skill.teleop.toggle", description="Operator gripper toggle", resources=_RESOURCES,
                        termination=TerminationSpec(timeout_s=5.0))
_HOME = SkillManifest(name="skill.teleop.home", description="Operator home", resources=_RESOURCES,
                      termination=TerminationSpec(timeout_s=5.0))


@dataclass
class JogResult:
    ok: bool
    # 原因区分越界、不可达、拒绝或无错误。
    reason: str = ""  # "fence" | "unreachable" | "denied" | ""
    operation_id: str = ""
    attempt_id: str = ""


class TeleopSession:
    """One operator session over a sim embodiment; episodes open/close explicitly.

    一个仿真操作者会话，显式开启与结束 episode。
    """

    def __init__(
        self,
        sim: Any,
        *,
        task: str,
        root: Path | str | None = None,
        jog_seconds: float = 0.12,
        max_pos_err: float = 0.008,
        record: bool = True,
        registry: SkillRegistry | None = None,
    ) -> None:
        self.sim = sim
        controller = sim.action_controller
        self.registry = registry or controller.runtime or SkillRegistry(robot_id=sim.spec().embodiment_id)
        self.registry.configure_execution_controller(controller)
        self.registry.configure_stop_controller(sim.stop_controller)
        self.last_result: SkillResult | None = None
        self._task_id = uuid4().hex
        self.task = task
        self.jog_seconds = jog_seconds
        self.max_pos_err = max_pos_err
        self.recorder = EpisodeRecorder(root) if record else None
        self.writer: EpisodeWriter | None = None
        self.episodes = 0
        spec = sim.spec()
        self._state_names = [j.name for j in spec.joints]
        if self._state_names and spec.gripper_joint is not None:
            self._state_names.append(spec.gripper_joint.name)

    # 数据局生命周期。
    # -- episode lifecycle --------------------------------------------------------

    def start_episode(self, *, randomize: bool = True) -> None:
        """Reset the scene and open a fresh episode (closing any dangling one as failed).

        重置场景并开始新局，未结束旧局按失败收尾。
        """
        self.registry.check_thread()
        self.sim.action_controller.require_idle()
        owner = getattr(self.sim.action_controller, "recording_owner", None)
        if owner is not None and owner is not self:
            raise RuntimeError("recording_session_busy")
        if self.writer is not None:
            self.abort_episode("superseded by new episode")
        self.sim.reset(randomize=randomize)
        if self.recorder is not None:
            extra: dict[str, Any] = {"collector": TELEOP_COLLECTOR_ID, "episode_index": self.episodes,
                                     "task_id": self._task_id}
            if self._state_names:
                extra["state_names"] = self._state_names
            self.writer = self.recorder.start(
                task=self.task, embodiment_id=self.sim.spec().embodiment_id, extra_meta=extra
            )
            self.sim.set_hooks(
                on_step=self.writer.on_step,
                on_guard_event=lambda e, w=self.writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
            )
            self.sim.action_controller.recording_owner = self
        self.episodes += 1

    def finish_episode(self, success: bool) -> Path | None:
        """Human marks the outcome — the operator IS the judge in teleop.

        遥操作由操作者判定结果。
        """
        if self.writer is None:
            return None
        self.registry.check_thread()
        self.sim.action_controller.require_idle()
        self.sim.set_hooks(None, None)
        path = self.writer.finish(success, detail="operator-marked")
        self.writer = None
        self.sim.action_controller.recording_owner = None
        return path

    def abort_episode(self, reason: str = "operator abort") -> None:
        if self.writer is None:
            return
        self.registry.check_thread()
        self.sim.set_hooks(None, None)
        self.writer.abort(reason)
        self.writer = None
        self.sim.action_controller.recording_owner = None

    def close(self) -> None:
        """End of session: a dangling episode is kept, labeled failed (data, not garbage).

        会话结束时保留未完成局并标失败。
        """
        self.abort_episode("session closed mid-episode")

    # 运动命令。
    # -- motion commands ----------------------------------------------------------

    def jog(self, dx: float, dy: float, dz: float) -> JogResult:
        """Cartesian nudge of the grasp point, top-down orientation held.

        Fails closed without any motion when the target leaves the safety fence or
        IK cannot reach it within tolerance.


        保持俯向抓取姿态微调末端；越界或逆解误差超限时不产生运动。
        """
        result = self._invoke(_JOG, self._jog, {"dx": dx, "dy": dy, "dz": dz})
        return JogResult(result.ok, "" if result.ok else result.detail, result.operation_id, result.attempt_id)

    def _jog(self, dx: float, dy: float, dz: float) -> SkillResult:
        obs = self.sim.read_control_feedback()
        target = np.asarray(obs.ee_pose.pos, dtype=float) + [dx, dy, dz]
        if hasattr(self.sim, "guard") and not self.sim.guard.check_target(tuple(target)):
            return SkillResult(ok=False, detail="fence")
        res = self.sim.solve_reach(target)
        if res.pos_err > self.max_pos_err:
            return SkillResult(ok=False, detail="unreachable")
        if not motions.move_to_q(self.sim, res.qpos, seconds=self.jog_seconds, settle_steps=10):
            return SkillResult(ok=False, detail="denied")
        return SkillResult(ok=True)

    def set_gripper(self, opening: float) -> bool:
        return self._invoke(_GRIP, self._gripper, {"opening": opening}).ok

    def _gripper(self, opening: float) -> SkillResult:
        ok = motions.set_gripper(self.sim, float(np.clip(opening, 0.0, 1.0)), seconds=0.3, settle_steps=30)
        return SkillResult(ok=ok, detail="" if ok else "denied")

    def toggle_gripper(self) -> bool:
        def toggle():
            current = self.sim.read_control_feedback().gripper_opening
            return self._gripper(GRIP_CLOSED if current > 0.3 else GRIP_OPEN)
        return self._invoke(_TOGGLE, toggle, {}).ok

    def home(self) -> bool:
        def move():
            ok = motions.move_to_q(self.sim, self.sim.home_qpos(), seconds=1.0)
            return SkillResult(ok=ok, detail="" if ok else "denied")
        return self._invoke(_HOME, move, {}).ok

    def _invoke(self, manifest, handler, params) -> SkillResult:
        identity = OperationIdentity(task_id=self._task_id, operation_id=uuid4().hex)
        try:
            result = self.registry.invoke_operator(manifest, handler, params, operation=identity)
        except Exception as exc:
            record = self.registry.get_operation(identity.operation_id)
            result = self.registry._from_record(record) if record else SkillResult(ok=False, detail=str(exc))
        self.last_result = result
        return result

    # 状态查询。
    # -- introspection ------------------------------------------------------------

    def status(self) -> str:
        obs = self.sim.read()
        x, y, z = obs.ee_pose.pos
        rec = "REC" if self.writer is not None else "---"
        return f"[{rec}] ep{self.episodes:03d} ee=({x:+.3f},{y:+.3f},{z:+.3f}) grip={obs.gripper_opening:.2f}"
