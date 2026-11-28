"""Eval task loading/judging/persistence — hermetic (fake sim & engine, tmp dirs).

隔离验证任务载入、裁判和结果持久化。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from embodied.cognition.plan import StepResult, StepStatus
from embodied.control.hal import Observation, Pose
from embodied.data_engine.evaluation import RESULT_SCHEMA, EvalTask, run_task
from embodied.data_engine.failures import failure_index

TASK_YAML = """\
schema: eval.task/v1
name: fake_task
version: v9
command: "do the thing"
embodiment: sim.fake
episodes: {seeds: [7, 8], per_seed: 2}
randomization: {object: obj_cube}
judge: {predicate: object_in_region, object: obj_cube, region: bin_region, margin_m: 0.005}
pass_threshold: 0.75
"""


@pytest.fixture
def task(tmp_path) -> EvalTask:
    p = tmp_path / "fake_task_v9.yaml"
    p.write_text(TASK_YAML, encoding="utf-8")
    return EvalTask.load(p)


def obs(cube=(0.16, -0.18, 0.02)) -> Observation:
    return Observation(
        t=0.0, qpos=(0.0,), qvel=(0.0,), gripper_opening=0.5, ee_pose=Pose(pos=(0, 0, 0.2)),
        objects={"obj_cube": Pose(pos=cube)},
        extras={"regions": {"bin_region": {"center": (0.16, -0.18, 0.045), "half": (0.045, 0.045, 0.04)}}},
    )


class FakeSim:
    def __init__(self, seed, success_pattern):
        self.seed = seed
        self.pattern = list(success_pattern)
        self.n = -1

    def spec(self):
        class S:
            embodiment_id = "sim.fake"

        return S()

    def reset(self, randomize=False):
        self.n += 1
        return obs(cube=(-0.1, -0.2, 0.015))

    def read(self):
        ok = self.pattern[self.n % len(self.pattern)]
        return obs() if ok else obs(cube=(-0.1, -0.2, 0.015))

    def set_hooks(self, a, b):
        pass


class FakeEngine:
    async def turn(self, text):
        class T:
            text = "报告"
            results = [StepResult(step_id="s1", status=StepStatus.OK, detail="ok")]
            plan = None
            replans = 0

        return T()


def test_task_load_and_ids(task):
    assert task.task_id == "fake_task@v9"
    assert task.seeds == [7, 8] and task.per_seed == 2
    assert task.pass_threshold == 0.75


def test_bad_schema_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("schema: something/else\nname: x\nversion: v1\ncommand: c\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected schema"):
        EvalTask.load(p)


async def test_run_judges_from_ground_truth_and_persists(task, tmp_path, monkeypatch):
    sims: dict[int, FakeSim] = {}

    def make_sim(seed):
        # 每局交替模拟成功。
        sims[seed] = FakeSim(seed, [True, False])  # alternate success per episode
        return sims[seed]

    report = await run_task(
        task, make_sim=make_sim, make_engine=lambda sim: FakeEngine(),
        out_dir=tmp_path / "eval_out",
    )
    assert len(report.episodes) == 4
    assert report.wins == 2 and report.rate == 0.5
    assert not report.passed  # 0.5 < 0.75

    data = json.loads(Path(report.out_path).read_text(encoding="utf-8"))
    assert data["schema"] == RESULT_SCHEMA
    assert data["task"]["id"] == "fake_task@v9"
    assert data["total"] == 4 and data["wins"] == 2 and data["passed"] is False
    assert len(data["failures"]) == 2
    assert {e["failure_category"] for e in data["failures"]} == {"judge_unsatisfied"}
    assert all("failure_category" not in e for e in data["episodes"] if e["success"])
    assert all(e["steps"][0]["id"] == "s1" for e in data["episodes"])


async def test_all_pass(task, tmp_path):
    report = await run_task(
        task, make_sim=lambda seed: FakeSim(seed, [True]),
        make_engine=lambda sim: FakeEngine(), out_dir=tmp_path / "o",
    )
    assert report.passed and report.rate == 1.0


@pytest.mark.parametrize("remains_busy", [False, True])
async def test_seed_reset_waits_for_existing_observation_and_preserves_failures(task, tmp_path, monkeypatch, remains_busy):
    import embodied.data_engine.evaluation as evaluation

    class Registry:
        observation_pending = False

        async def drain_compute(self):
            pass

        def close(self):
            pass

    class Sim(FakeSim):
        def __init__(self, seed):
            super().__init__(seed, [False, True])
            self._eval_registry = Registry()

        def reset(self, randomize=False):
            assert not self._eval_registry.observation_pending
            return super().reset(randomize=randomize)

    def engine(sim):
        class Delayed(FakeEngine):
            async def turn(self, text):
                if sim.n == 0:
                    sim._eval_registry.observation_pending = True
                    if not remains_busy:
                        asyncio.get_running_loop().call_later(0.01, setattr, sim._eval_registry, "observation_pending", False)
                return await super().turn(text)

        return Delayed()

    out = tmp_path / "eval_results"
    if not remains_busy:
        report = await run_task(task, make_sim=Sim, make_engine=engine, out_dir=out)
        assert [r["success"] for r in report.episodes] == [False, True, False, True]
        assert not report.passed
        return

    monkeypatch.setattr(evaluation, "OBSERVATION_TIMEOUT_S", 0.01)
    with pytest.raises(evaluation.ObservationUnavailable, match="previous_episode_observation_still_running"):
        await run_task(task, make_sim=Sim, make_engine=engine, out_dir=out)
    saved = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert saved["schema"] == evaluation.INTERRUPTED_SCHEMA and not saved["complete"] and not saved["passed"]
    assert saved["total"] == 1 and saved["planned_total"] == 4 and saved["rate"] is None
    assert saved["interruption"]["stage"] == "reset" and saved["interruption"]["index"] == 2
    indexed = failure_index([out])
    assert indexed["total"] == 1 and indexed["issues"][0]["reason"] == "evaluation_interrupted"


def test_archived_eval_requires_matching_manifest_and_build_identity(tmp_path, monkeypatch):
    import embodied.data_engine.evaluation as evaluation

    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(evaluation.subprocess, "run", missing_git)
    monkeypatch.setenv("EMBODIED_RELEASE", "a" * 40)
    manifest = tmp_path / "bundle-manifest.json"
    manifest.write_text(json.dumps({"commit": "b" * 40}), encoding="utf-8")
    assert evaluation._git_commit(tmp_path) == ""
    manifest.write_text(json.dumps({"commit": "a" * 40}), encoding="utf-8")
    assert evaluation._git_commit(tmp_path) == "aaaaaaa"


def test_shipped_task_file_is_valid():
    root = Path(__file__).resolve().parents[2]
    task = EvalTask.load(root / "eval" / "tasks" / "tabletop_pick_place_v1.yaml")
    assert task.task_id == "tabletop_pick_place@v1"
    assert task.seeds == [0, 1, 2] and task.per_seed == 10
    assert task.judge["predicate"] == "object_in_region"
