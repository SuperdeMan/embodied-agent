"""Three-state verifier contract tests. UNKNOWN-never-convicts and confirm-never-retries
are load-bearing safety semantics — fix code, not tests."""

from __future__ import annotations

from embodied.cognition import verify
from embodied.cognition.world_state import Region, WorldSnapshot
from embodied.control.hal import Pose


def snap(cube=(0.16, -0.18, 0.02), ee=(0.0, 0.0, 0.2)) -> WorldSnapshot:
    return WorldSnapshot(
        t=1.0, ee_pos=ee, gripper_opening=0.5,
        objects={"obj_cube": Pose(pos=cube)},
        regions={"bin_region": Region(center=(0.16, -0.18, 0.045), half=(0.045, 0.045, 0.04))},
    )


def test_resolve_args_param_refs():
    args = verify.resolve_args({"object": "$param:object", "margin": 0.01}, {"object": "obj_cube"})
    assert args == {"object": "obj_cube", "margin": 0.01}
    unresolved = verify.resolve_args({"object": "$param:missing"}, {})
    assert unresolved["object"] is verify.UNRESOLVED


def test_object_in_region_three_states():
    expect = {"predicate": "object_in_region", "args": {"object": "obj_cube", "region": "bin_region"}}
    assert verify.eval_state_predicate(expect, snap()) == verify.SAT
    assert verify.eval_state_predicate(expect, snap(cube=(0.4, 0.4, 0.02))) == verify.UNSAT
    missing = {"predicate": "object_in_region", "args": {"object": "obj_ghost", "region": "bin_region"}}
    assert verify.eval_state_predicate(missing, snap()) == verify.UNKNOWN


def test_gripper_holding():
    expect = {"predicate": "gripper_holding", "args": {"object": "obj_cube"}}
    held = snap(cube=(0.0, 0.0, 0.18), ee=(0.0, 0.0, 0.2))
    on_table = snap(cube=(0.16, -0.18, 0.02), ee=(0.0, 0.0, 0.2))
    assert verify.eval_state_predicate(expect, held) == verify.SAT
    assert verify.eval_state_predicate(expect, on_table) == verify.UNSAT


def test_unknown_predicate_and_no_snapshot_never_convict():
    assert verify.eval_state_predicate({"predicate": "future_pred", "args": {}}, snap()) == verify.UNKNOWN
    assert verify.eval_state_predicate({"predicate": "object_in_region", "args": {}}, None) == verify.UNKNOWN


def test_unresolved_param_ref_is_unknown():
    expect = {"predicate": "gripper_holding", "args": {"object": "$param:object"}}
    assert verify.eval_state_predicate(expect, snap(), params={}) == verify.UNKNOWN


def test_schema_mode():
    assert verify.eval_schema({"data_keys": ["object"]}, {"object": "obj_cube"}) == verify.SAT
    assert verify.eval_schema({"data_keys": ["object"]}, {}) == verify.UNSAT
    assert verify.eval_schema({"data_keys": ["items"]}, {"items": []}) == verify.UNSAT
    assert verify.eval_schema({"data_keys": ["n"]}, {"n": 0}) == verify.SAT  # 0/False are values
    assert verify.eval_schema({}, {"x": 1}) == verify.UNKNOWN


async def test_evaluate_dispatch_and_timeout():
    v = {"mode": "state_predicate", "timeout_ms": 200,
         "expect": {"predicate": "object_in_region", "args": {"object": "obj_cube", "region": "bin_region"}}}
    assert await verify.evaluate(v, {}, world_fn=lambda: snap()) == verify.SAT
    assert await verify.evaluate(v, {}, world_fn=lambda: snap(cube=(0.4, 0.4, 0.0))) == verify.UNSAT
    assert await verify.evaluate(v, {}, world_fn=None) == verify.UNKNOWN
    assert await verify.evaluate({"mode": "unheard_of"}, {}) == verify.UNKNOWN


def test_retry_never_for_confirmed_side_effects():
    v = {"on_fail": "retry", "max_attempts": 3}
    assert verify.retry_allowed(v, require_confirm=False, attempts=0)
    assert not verify.retry_allowed(v, require_confirm=True, attempts=0)  # hard constraint
    assert not verify.retry_allowed({"on_fail": "report"}, require_confirm=False, attempts=0)
    assert not verify.retry_allowed(v, require_confirm=False, attempts=3)
