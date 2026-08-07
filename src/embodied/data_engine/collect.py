"""Scripted-expert data collection: the M2 learning loop's data source.

Drives scripted skills DIRECTLY through the SkillRegistry (no planner/LLM in the
loop): deterministic expert rollouts over randomized scenes, recorded by default.
Success is judged against sim ground truth, never the skill's self-report; failed
episodes are kept and labeled (architecture §4.6 — failure data is an asset).
skill_start/skill_end events carry ``sim_t`` at the TRUE skill boundaries so the
LeRobot converter can segment episodes per skill (docs/decisions.md D014).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from embodied.cognition.world_state import WorldSnapshot
from embodied.data_engine.recorder import EpisodeRecorder

COLLECTOR_ID = "scripted-expert/v1"

SkillSequence = tuple[tuple[str, dict[str, Any]], ...]


@dataclass
class CollectReport:
    episodes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for e in self.episodes if e["success"])

    @property
    def rate(self) -> float:
        return self.successes / len(self.episodes) if self.episodes else 0.0


async def collect_episodes(
    sim: Any,
    registry: Any,
    *,
    episodes: int,
    command: str,
    skill_sequence: SkillSequence,
    judge: Callable[[WorldSnapshot], bool],
    root: Path | str | None = None,
    seed: int | None = None,
    progress: Callable[[str], None] = print,
) -> CollectReport:
    """Run ``episodes`` expert rollouts of ``skill_sequence`` and record each one.

    A failed skill aborts the remaining sequence but the episode is still finalized
    (success=False) — partial trajectories train recovery behaviors later. ``judge``
    runs on a fresh ground-truth snapshot only when every skill reported ok.
    """
    recorder = EpisodeRecorder(root)
    report = CollectReport()
    spec = sim.spec()
    # Self-describing datasets: joint names ride along so the converter can label
    # observation.state columns (falls back to generic names when absent).
    state_names = [j.name for j in getattr(spec, "joints", ())]
    gripper = getattr(spec, "gripper_joint", None)
    if state_names and gripper is not None:
        state_names.append(gripper.name)
    for i in range(episodes):
        sim.reset(randomize=True)
        extra_meta: dict[str, Any] = {
            "collector": COLLECTOR_ID,
            "episode_index": i,
            "skill_sequence": [name for name, _ in skill_sequence],
        }
        if state_names:
            extra_meta["state_names"] = state_names
        writer = recorder.start(
            task=command,
            embodiment_id=spec.embodiment_id,
            seed=seed,
            extra_meta=extra_meta,
        )
        sim.set_hooks(
            on_step=writer.on_step,
            on_guard_event=lambda e, w=writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
        )
        try:
            ok, detail = True, ""
            for skill, params in skill_sequence:
                writer.on_event("skill_start", {"skill": skill, "params": dict(params), "sim_t": sim.read().t})
                result = await registry.invoke(skill, dict(params))
                writer.on_event(
                    "skill_end",
                    {"skill": skill, "ok": result.ok, "detail": result.detail[:200], "sim_t": sim.read().t},
                )
                if not result.ok:
                    ok, detail = False, f"{skill}: {result.detail[:160]}"
                    break
            success = ok and bool(judge(WorldSnapshot.from_observation(sim.read())))
            if ok and not success:
                detail = "judge predicate false after rollout"
            path = writer.finish(success, detail=detail or "expert rollout complete")
        except Exception as e:
            writer.abort(repr(e))
            raise
        finally:
            sim.set_hooks(None, None)
        report.episodes.append({"index": i, "success": success, "path": str(path), "detail": detail})
        progress(f"[collect] episode {i + 1:>3}/{episodes}: {'SUCCESS' if success else 'FAIL'}"
                 f"{' — ' + detail if detail else ''}")
    return report
