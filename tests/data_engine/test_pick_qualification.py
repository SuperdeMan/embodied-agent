"""Exercise the full-run equality gate for learned pick acceptance. 验证完整对照才可判定学习策略达标。"""

import runpy
from pathlib import Path

import pytest

from embodied.data_engine.evaluation import EvalTask

ROOT = Path(__file__).resolve().parents[2]
recipe = runpy.run_path(str(ROOT / "scripts/qualify_pick.py"))


@pytest.mark.parametrize("change", [
    {"schema": "eval.interrupted/v1", "complete": False, "total": 1, "wins": 1},
    {"total": 29, "wins": 29},
    {"wins": 29},
    {"exit_code": 1},
    {"commit": "another"},
])
def test_incomplete_or_mismatched_run_cannot_qualify(change):
    task = EvalTask.load(ROOT / "eval/tasks/tabletop_pick_place_v1.yaml")
    ok = dict(schema="eval.result/v1", complete=True, total=30, planned_total=30, wins=30,
              task=task.task_id, exit_code=0, commit="b44f00d")
    assert recipe["matched_reference"](task, ok, ok)
    assert not recipe["matched_reference"](task, ok, ok | change)
    assert not recipe["matched_reference"](task, ok | change, ok)

