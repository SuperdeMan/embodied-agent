"""Eval task loading/judging/persistence — hermetic (fake sim & engine, tmp dirs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from embodied.cognition.plan import StepResult, StepStatus
from embodied.control.hal import Observation, Pose
from embodied.data_engine.evaluation import RESULT_SCHEMA, EvalTask, run_task

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
    assert all(e["steps"][0]["id"] == "s1" for e in data["episodes"])


async def test_all_pass(task, tmp_path):
    report = await run_task(
        task, make_sim=lambda seed: FakeSim(seed, [True]),
        make_engine=lambda sim: FakeEngine(), out_dir=tmp_path / "o",
    )
    assert report.passed and report.rate == 1.0


def test_shipped_task_file_is_valid():
    root = Path(__file__).resolve().parents[2]
    task = EvalTask.load(root / "eval" / "tasks" / "tabletop_pick_place_v1.yaml")
    assert task.task_id == "tabletop_pick_place@v1"
    assert task.seeds == [0, 1, 2] and task.per_seed == 10
    assert task.judge["predicate"] == "object_in_region"
