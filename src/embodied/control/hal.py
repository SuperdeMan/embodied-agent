"""Hardware abstraction layer: the seam that keeps everything above L1 embodiment-agnostic.

The MuJoCo driver implements this `Embodiment` protocol; a future real driver
must preserve its execution boundary while removing sim-specific skill coupling
(docs/architecture.md §4.4). Safety Guard runs INSIDE the driver write path —
below skills, below the planner — so nothing upstream can skip it.


硬件抽象隔离上层与本体；仿真和未来串口驱动实现相同协议，Guard 在驱动写路径内强制执行，上层不能绕过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class JointSpec:
    name: str
    # 弧度。
    lower: float  # rad
    # 弧度。
    upper: float  # rad


@dataclass(frozen=True)
class EmbodimentSpec:
    # 仿真或真实本体的标识示例。
    embodiment_id: str  # e.g. "sim.mujoco.so_arm100" / "real.so_arm101"
    # 按执行器顺序排列的机械臂关节，不含夹爪。
    joints: tuple[JointSpec, ...]  # arm joints, actuator order (gripper excluded)
    gripper_joint: JointSpec | None = None


@dataclass(frozen=True)
class Pose:
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)  # (w, x, y, z)


@dataclass
class Observation:
    # 本体时钟，单位秒。
    t: float  # embodiment clock, seconds
    # 按规格顺序排列的机械臂关节。
    qpos: tuple[float, ...]  # arm joints, spec order
    qvel: tuple[float, ...]
    # 归一化开度：零关闭、一张开。
    gripper_opening: float  # normalized 0 (closed) .. 1 (open)
    # 基座坐标系下的抓取点位姿。
    ee_pose: Pose  # grasp-point pose in base frame
    # 物体位姿由仿真真值或 M2 感知填充。
    objects: dict[str, Pose] = field(default_factory=dict)  # sim ground truth; perception fills this from M2
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionCommand:
    # 按规格顺序的绝对位置目标。
    joint_targets: tuple[float, ...]  # absolute position targets, spec order
    # 归一化目标开度；空值表示保持。
    gripper: float | None = None  # normalized target opening; None = hold
    # 来源标签由安全白名单检查。
    source: str = "skill"  # provenance, checked against the safety whitelist


@dataclass
class WriteResult:
    applied: bool
    # Guard 拒绝或闩锁时填写原因。
    reason: str = ""  # populated when denied/halted by the guard
    # 范围或增量被夹限时为真。
    clamped: bool = False  # true when the guard limited range or rate


@runtime_checkable
class Embodiment(Protocol):
    def spec(self) -> EmbodimentSpec: ...

    def read(self) -> Observation: ...

    def write(self, cmd: ActionCommand) -> WriteResult: ...

    def step(self, n: int = 1) -> None:
        """Advance sim time; real drivers no-op (wall clock advances itself).

        推进仿真时间；真机由墙钟推进，此操作为空。
        """
        ...

    def reset(self) -> Observation: ...
