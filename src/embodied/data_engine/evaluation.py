"""Versioned eval runner: load a task file, run episodes through the FULL engine path,
judge against sim ground truth, persist a machine-readable result.

Task files are immutable per version (eval/tasks/); results land in outputs/eval/
(gitignored); the append-only human ledger is eval/BASELINES.md.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from embodied.cognition.world_state import WorldSnapshot, object_in_region

TASK_SCHEMA = "eval.task/v1"
RESULT_SCHEMA = "eval.result/v1"


@dataclass
class EvalTask:
    name: str
    version: str
    command: str
    embodiment: str
    seeds: list[int]
    per_seed: int
    judge: dict[str, Any]
    pass_threshold: float
    randomization: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    @property
    def task_id(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def load(cls, path: Path | str) -> "EvalTask":
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if data.get("schema") != TASK_SCHEMA:
            raise ValueError(f"{path}: expected schema {TASK_SCHEMA!r}, got {data.get('schema')!r}")
        episodes = data.get("episodes") or {}
        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            command=str(data["command"]),
            embodiment=str(data.get("embodiment", "")),
            seeds=[int(s) for s in episodes.get("seeds", [0])],
            per_seed=int(episodes.get("per_seed", 10)),
            judge=dict(data.get("judge") or {}),
            pass_threshold=float(data.get("pass_threshold", 0.8)),
            randomization=dict(data.get("randomization") or {}),
            path=str(path),
        )

    def judge_success(self, snap: WorldSnapshot) -> bool:
        j = self.judge
        if j.get("predicate") != "object_in_region":
            raise ValueError(f"unsupported judge predicate {j.get('predicate')!r}")
        return object_in_region(
            snap, str(j.get("object", "")), str(j.get("region", "")),
            margin=float(j.get("margin_m", 0.0)),
        )


def _truth_obs(sim):
    """Judges read ground truth even when the agent runs on perception (D015):
    a PerceivedSim exposes read_truth(); a bare sim IS the truth."""
    return sim.read_truth() if hasattr(sim, "read_truth") else sim.read()


def _close_sim(sim) -> None:
    """Per-seed sims that own resources (perception render threads) must release
    them deterministically; bare sims have no close() and skip as a no-op."""
    fn = getattr(sim, "close", None)
    if callable(fn):
        fn()


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[3],
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


@dataclass
class EvalReport:
    task_id: str
    episodes: list[dict[str, Any]]
    passed: bool
    out_path: Path | None

    @property
    def wins(self) -> int:
        return sum(1 for e in self.episodes if e["success"])

    @property
    def rate(self) -> float:
        return self.wins / len(self.episodes) if self.episodes else 0.0


async def run_task(
    task: EvalTask,
    *,
    make_sim,
    make_engine,
    record: bool = False,
    out_dir: Path | str | None = None,
    progress=print,
) -> EvalReport:
    """Run all task episodes. make_sim(seed) -> TabletopSim; make_engine(sim) -> engine.

    Fresh sim AND fresh engine per seed group / episode keep episodes independent;
    success is judged from ground truth, never from the agent's report.
    """
    recorder = None
    if record:
        from embodied.data_engine import EpisodeRecorder

        recorder = EpisodeRecorder()

    episodes: list[dict[str, Any]] = []
    idx = 0
    for seed in task.seeds:
        sim = make_sim(seed)
        for k in range(task.per_seed):
            idx += 1
            sim.reset(randomize=True)
            truth0 = _truth_obs(sim)
            start_pos = tuple(round(v, 4) for v in truth0.objects[task.judge.get("object", "obj_cube")].pos)
            engine = make_engine(sim)
            writer = None
            if recorder is not None:
                writer = recorder.start(
                    task=task.command, embodiment_id=sim.spec().embodiment_id,
                    seed=seed, extra_meta={"eval_task": task.task_id, "episode_index": idx},
                )
                sim.set_hooks(
                    on_step=writer.on_step,
                    on_guard_event=lambda e, w=writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
                )
            episode_dir = ""
            try:
                turn = await engine.turn(task.command)
                snap = WorldSnapshot.from_observation(_truth_obs(sim))
                success = task.judge_success(snap)
                if writer:
                    for r in turn.results:
                        writer.on_event("skill_end", {"step": r.step_id, "status": r.status.value,
                                                      "detail": (r.detail or r.error)[:200]})
                    episode_dir = str(writer.finish(bool(success), detail=turn.text[:200]))
            except Exception as e:
                if writer:
                    episode_dir = str(writer.abort(repr(e)))
                raise
            finally:
                if recorder is not None:
                    sim.set_hooks(None, None)
            steps = [
                {"id": r.step_id, "status": r.status.value, "detail": (r.detail or r.error)[:160]}
                for r in turn.results
            ]
            episodes.append({
                "index": idx, "seed": seed, "start_pos": start_pos, "success": bool(success),
                "steps": steps, "report": turn.text[:300], "episode_dir": episode_dir,
            })
            summary = " ".join(f"{s['id']}={s['status']}" for s in steps) or "no-steps"
            progress(f"[{task.task_id}] episode {idx:>3}: seed={seed} pos={start_pos[:2]} "
                     f"{summary} -> {'SUCCESS' if success else 'FAIL'}")
        _close_sim(sim)

    report = EvalReport(task_id=task.task_id, episodes=episodes, passed=False, out_path=None)
    report.passed = report.rate >= task.pass_threshold
    report.out_path = _persist(task, report, out_dir)
    return report


def _persist(task: EvalTask, report: EvalReport, out_dir: Path | str | None) -> Path:
    root = Path(out_dir or os.getenv("EVAL_OUT_DIR", "outputs/eval"))
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = root / f"{task.name}@{task.version}-{stamp}.json"
    payload = {
        "schema": RESULT_SCHEMA,
        "task": {"id": task.task_id, "file": task.path, "command": task.command,
                 "judge": task.judge, "randomization": task.randomization,
                 "pass_threshold": task.pass_threshold},
        "commit": _git_commit(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "total": len(report.episodes),
        "wins": report.wins,
        "rate": round(report.rate, 4),
        "passed": report.passed,
        "failures": [e for e in report.episodes if not e["success"]],
        "episodes": report.episodes,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
