"""Hardware abstraction layer: the seam that keeps everything above L1 embodiment-agnostic.

Sim (MuJoCo) and real (Feetech serial, M3) drivers implement the same `Embodiment`
protocol; skills and cognition never know which one they are driving
(docs/architecture.md §4.4). Safety Guard runs INSIDE the driver write path —
below skills, below the planner — so nothing upstream can skip it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class JointSpec:
    name: str
    lower: float  # rad
    upper: float  # rad


@dataclass(frozen=True)
class EmbodimentSpec:
    embodiment_id: str  # e.g. "sim.mujoco.so_arm100" / "real.so_arm101"
    joints: tuple[JointSpec, ...]  # arm joints, actuator order (gripper excluded)
    gripper_joint: JointSpec | None = None


@dataclass(frozen=True)
class Pose:
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)  # (w, x, y, z)


@dataclass
class Observation:
    t: float  # embodiment clock, seconds
    qpos: tuple[float, ...]  # arm joints, spec order
    qvel: tuple[float, ...]
    gripper_opening: float  # normalized 0 (closed) .. 1 (open)
    ee_pose: Pose  # grasp-point pose in base frame
    objects: dict[str, Pose] = field(default_factory=dict)  # sim ground truth; perception fills this from M2
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionCommand:
    joint_targets: tuple[float, ...]  # absolute position targets, spec order
    gripper: float | None = None  # normalized target opening; None = hold
    source: str = "skill"  # provenance, checked against the safety whitelist


@dataclass
class WriteResult:
    applied: bool
    reason: str = ""  # populated when denied/halted by the guard
    clamped: bool = False  # true when the guard limited range or rate


@runtime_checkable
class Embodiment(Protocol):
    def spec(self) -> EmbodimentSpec: ...

    def read(self) -> Observation: ...

    def write(self, cmd: ActionCommand) -> WriteResult: ...

    def step(self, n: int = 1) -> None:
        """Advance sim time; real drivers no-op (wall clock advances itself)."""
        ...

    def reset(self) -> Observation: ...
