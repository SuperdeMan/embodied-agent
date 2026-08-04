# Ported from car-agent orchestrator/cloud/verify.py @ f0b08f8, changes: state source is a
# WorldSnapshot callable instead of the NATS state mirror; expect keys become named spatial
# predicates over the snapshot (declarative registry, no skill-name branches — the origin's
# "no agent_id/intent literals" iron rule maps to "no skill-name literals" here); $slot:
# dynamic expectations become $param:; schema mode kept for data-producing skills.
"""Outcome verifier: post-execution reconciliation (step OK ≠ goal achieved).

Three-state semantics, inherited verbatim in spirit:
- SAT     expectation confirmed → pass through
- UNSAT   HARD evidence of failure → on_fail (report honestly / retry once)
- UNKNOWN observation missing (object/region not in snapshot) → never convict;
          "can't see it" is not "didn't happen".
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any, Callable

logger = logging.getLogger("cognition.verify")

SAT, UNSAT, UNKNOWN = "sat", "unsat", "unknown"

MODE_SCHEMA, MODE_STATE = "schema", "state_predicate"
ON_FAIL_REPORT, ON_FAIL_RETRY = "report", "retry"

DEFAULT_TIMEOUT_MS = 2000
_POLL_INTERVAL_S = 0.1

_PARAM_REF_PREFIX = "$param:"


class _Unresolved:
    """The declaration referenced a param this step doesn't have: can't evaluate ≠ failed."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<unresolved param ref>"


UNRESOLVED = _Unresolved()


def resolve_args(args: dict, params: dict) -> dict:
    """Replace `$param:<name>` values with this step's ACTUAL params (post ref-resolution).
    Whole-value references only — no string interpolation (same rationale as origin: never
    grow an LLM-influenceable syntax surface inside the reconciliation layer)."""
    if not isinstance(args, dict):
        return {}
    params = params if isinstance(params, dict) else {}
    out: dict = {}
    for k, want in args.items():
        if isinstance(want, str) and want.startswith(_PARAM_REF_PREFIX):
            name = want[len(_PARAM_REF_PREFIX):].strip()
            value = params.get(name)
            out[k] = UNRESOLVED if value is None or str(value).strip() == "" else value
        else:
            out[k] = want
    return out


# ── evaluator one: schema (query-ish skills — "we got real data back") ──────────

def eval_schema(expect: dict, data: dict) -> str:
    keys = expect.get("data_keys")
    if not isinstance(keys, (list, tuple)) or not keys:
        return UNKNOWN
    if not isinstance(data, dict):
        return UNSAT
    for k in keys:
        if str(k) not in data:
            return UNSAT
        v = data[str(k)]
        if v is None:
            return UNSAT
        if isinstance(v, (str, list, tuple, dict, set)) and len(v) == 0:
            return UNSAT
    return SAT


# ── evaluator two: state predicates ("the world actually changed") ─────────────
#
# Predicates read a WorldSnapshot (embodied.cognition.world_state). Registry is the
# extension seam: adding a predicate never touches the engine. Each returns SAT/UNSAT/
# UNKNOWN; missing objects/regions are UNKNOWN by contract.

def _pred_object_in_region(snap: Any, args: dict) -> str:
    obj, region = str(args.get("object", "")), str(args.get("region", ""))
    if obj not in snap.objects or region not in snap.regions:
        return UNKNOWN
    margin = float(args.get("margin", 0.005))
    return SAT if snap.regions[region].contains(snap.objects[obj].pos, margin=margin) else UNSAT


def _pred_object_near(snap: Any, args: dict) -> str:
    obj = str(args.get("object", ""))
    if obj not in snap.objects:
        return UNKNOWN
    try:
        pos = tuple(float(v) for v in args.get("pos", ()))
        radius = float(args.get("radius", 0.05))
    except (TypeError, ValueError):
        return UNKNOWN
    if len(pos) != 3:
        return UNKNOWN
    return SAT if math.dist(snap.objects[obj].pos, pos) <= radius else UNSAT


def _pred_gripper_holding(snap: Any, args: dict) -> str:
    obj = str(args.get("object", ""))
    if obj not in snap.objects:
        return UNKNOWN
    min_z = float(args.get("min_z", 0.055))
    radius = float(args.get("radius", 0.06))
    p = snap.objects[obj].pos
    held = p[2] > min_z and math.dist(p, snap.ee_pos) < radius
    return SAT if held else UNSAT


PREDICATES: dict[str, Callable[[Any, dict], str]] = {
    "object_in_region": _pred_object_in_region,
    "object_near": _pred_object_near,
    "gripper_holding": _pred_gripper_holding,
}


def eval_state_predicate(expect: dict, snapshot: Any | None, params: dict | None = None) -> str:
    """Evaluate the declared predicate against a world snapshot. No snapshot → UNKNOWN
    ("I can't see" is not "it didn't happen"). Unresolved $param refs → UNKNOWN."""
    name = str(expect.get("predicate", "") or "")
    fn = PREDICATES.get(name)
    if fn is None:
        return UNKNOWN  # forward-compatible: new predicate on an old engine never convicts
    if snapshot is None:
        return UNKNOWN
    args = resolve_args(expect.get("args") or {}, params or {})
    if any(v is UNRESOLVED for v in args.values()):
        return UNKNOWN
    try:
        return fn(snapshot, args)
    except Exception as e:  # fail-open: verification must never take down the main chain
        logger.warning("predicate %s errored (ignored): %s", name, e)
        return UNKNOWN


# ── dispatch ───────────────────────────────────────────────────────────────────

async def evaluate(
    verification: dict,
    data: dict,
    world_fn: Callable[[], Any] | None = None,
    params: dict | None = None,
) -> str:
    """Evaluate per declared mode. Unknown mode → UNKNOWN (forward compatible).

    `state_predicate` polls until `timeout_ms` — physical effects settle over sim/real
    time (a released object needs a moment to land), asserting instantly would misfire.
    """
    mode = str(verification.get("mode") or "")
    expect = verification.get("expect") or {}
    if mode == MODE_SCHEMA:
        return eval_schema(expect, data)
    if mode == MODE_STATE:
        return await _eval_state_with_wait(expect, verification, world_fn, params)
    return UNKNOWN


async def _eval_state_with_wait(
    expect: dict, verification: dict, world_fn: Callable[[], Any] | None, params: dict | None
) -> str:
    if world_fn is None:
        return UNKNOWN
    timeout_ms = int(verification.get("timeout_ms") or 0) or DEFAULT_TIMEOUT_MS
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    while True:
        try:
            snapshot = world_fn()
        except Exception as e:
            logger.warning("world_fn errored (ignored): %s", e)
            return UNKNOWN
        verdict = eval_state_predicate(expect, snapshot, params)
        if verdict == SAT:
            return SAT
        if asyncio.get_event_loop().time() >= deadline:
            return verdict  # still unmet at deadline: UNSAT reports, UNKNOWN never convicts
        await asyncio.sleep(_POLL_INTERVAL_S)


def enabled() -> bool:
    return os.getenv("VERIFY_OUTCOME", "on").strip().lower() != "off"


def retry_allowed(verification: dict, require_confirm: bool, attempts: int) -> bool:
    """Side-effect-confirmed steps NEVER retry: replaying a confirmed action means executing
    a side effect twice on a single user confirmation. Hard constraint, not configuration."""
    if str(verification.get("on_fail") or ON_FAIL_REPORT) != ON_FAIL_RETRY:
        return False
    if require_confirm:
        return False
    return attempts < (int(verification.get("max_attempts") or 0) or 1)
