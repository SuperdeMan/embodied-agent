"""DagExecutor contract tests: layering, refs, dedup, confirm gate (fail-closed), verify."""

from __future__ import annotations

import asyncio

from embodied.cognition.executor import DagExecutor
from embodied.cognition.plan import Plan, PlanContext, Step, StepResult, StepStatus
from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult


def step(id: str, skill: str, params=None, depends_on=None, refs=None, **kw) -> Step:
    return Step(
        id=id, skill=skill, params=dict(params or {}), depends_on=list(depends_on or []),
        param_refs=dict(refs or {}), **kw,
    )


async def collect(executor, plan) -> list[StepResult]:
    out = []
    async for r in executor.run(plan, PlanContext()):
        out.append(r)
    return out


def make_env(handlers: dict | None = None, confirm=None, world_fn=None):
    registry = SkillRegistry()
    calls: list[tuple[str, dict]] = []

    def register(name, fn=None, *, require_confirm=False, params=None, timeout_s=30.0):
        async def default_fn(**kw):
            return SkillResult(ok=True, detail=f"{name} done", data={"echo": kw})

        handler = fn or default_fn

        async def wrapped(**kw):
            calls.append((name, kw))
            return await handler(**kw)

        registry.register(
            SkillManifest(
                name=name, description=name, require_confirm=require_confirm,
                params=params if params is not None else {"x": ParamSpec(type="integer", required=False)},
                termination=TerminationSpec(timeout_s=timeout_s),
            ),
            wrapped,
        )

    for name, fn in (handlers or {"skill.test.a": None, "skill.test.b": None, "skill.test.c": None}).items():
        register(name, fn)
    return registry, register, calls, DagExecutor(registry, confirm=confirm, world_fn=world_fn)


async def test_layering_and_parallelism():
    order: list[str] = []

    async def slow_a(**kw):
        await asyncio.sleep(0.05)
        order.append("a")
        return SkillResult(ok=True)

    async def fast_b(**kw):
        order.append("b")
        return SkillResult(ok=True)

    async def then_c(**kw):
        order.append("c")
        return SkillResult(ok=True)

    _, _, _, ex = make_env({"skill.test.a": slow_a, "skill.test.b": fast_b, "skill.test.c": then_c})
    plan = Plan(steps=[
        step("s1", "skill.test.a"), step("s2", "skill.test.b"),
        step("s3", "skill.test.c", depends_on=["s1", "s2"]),
    ])
    results = await collect(ex, plan)
    assert [r.status for r in results] == [StepStatus.OK] * 3
    assert order.index("c") == 2  # c strictly after the parallel layer
    assert set(order[:2]) == {"a", "b"}


async def test_param_refs_all_three_forms():
    async def producer(**kw):
        return SkillResult(ok=True, data={"object": "obj_cube", "nested": {"k": [{"v": 7}]}})

    seen = {}

    async def consumer(**kw):
        seen.update(kw)
        return SkillResult(ok=True)

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.prod", description="p"), producer)
    registry.register(
        SkillManifest(
            name="skill.test.cons", description="c",
            params={
                "a": ParamSpec(type="string", required=False),
                "b": ParamSpec(type="integer", required=False),
                "c": ParamSpec(type="string", required=False),
            },
        ),
        consumer,
    )
    ex = DagExecutor(registry)
    plan = Plan(steps=[
        step("s1", "skill.test.prod"),
        step("s2", "skill.test.cons",
             params={"a": "${s1.data.object}", "c": "$ref.a"},
             refs={"b": "s1.data.nested.k.0.v"},
             depends_on=["s1"]),
    ])
    results = await collect(ex, plan)
    assert all(r.status == StepStatus.OK for r in results)
    assert seen == {"a": "obj_cube", "b": 7, "c": "obj_cube"}


async def test_dependency_failure_skips_dependents():
    async def boom(**kw):
        return SkillResult(ok=False, detail="nope")

    _, _, calls, ex = make_env({"skill.test.a": boom, "skill.test.b": None})
    plan = Plan(steps=[step("s1", "skill.test.a"), step("s2", "skill.test.b", depends_on=["s1"])])
    results = await collect(ex, plan)
    by_id = {r.step_id: r for r in results}
    assert by_id["s1"].status == StepStatus.FAILED
    assert by_id["s2"].status == StepStatus.SKIPPED
    assert [c[0] for c in calls] == ["skill.test.a"]  # b never invoked


async def test_cycle_fails_plan():
    _, _, _, ex = make_env()
    plan = Plan(steps=[step("s1", "skill.test.a", depends_on=["s2"]),
                       step("s2", "skill.test.b", depends_on=["s1"])])
    results = await collect(ex, plan)
    assert results[0].step_id == "plan" and results[0].status == StepStatus.FAILED


async def test_unknown_dependency_fails_closed():
    _, _, calls, ex = make_env()
    plan = Plan(steps=[step("s1", "skill.test.a", depends_on=["phantom"])])
    results = await collect(ex, plan)
    assert results == [] or all(r.status != StepStatus.OK for r in results)
    assert calls == []


async def test_dedup_suppresses_identical_side_effect():
    _, _, calls, ex = make_env()
    plan = Plan(steps=[
        step("s1", "skill.test.a", params={"x": 1}),
        step("s2", "skill.test.a", params={"x": 1}, depends_on=["s1"]),
        step("s3", "skill.test.a", params={"x": 2}, depends_on=["s2"]),
    ])
    results = await collect(ex, plan)
    assert all(r.status == StepStatus.OK for r in results)
    assert len([c for c in calls if c[0] == "skill.test.a"]) == 2  # x=1 executed once, x=2 once


async def test_confirm_fail_closed_without_channel():
    registry = SkillRegistry()

    async def danger(**kw):
        raise AssertionError("must never run")

    registry.register(
        SkillManifest(name="skill.test.danger", description="d", require_confirm=True), danger
    )
    ex = DagExecutor(registry, confirm=None)
    plan = Plan(steps=[step("s1", "skill.test.danger", require_confirm=True)])
    results = await collect(ex, plan)
    assert results[0].status == StepStatus.NEED_CONFIRM
    assert results[0].error == "confirm_unavailable"


async def test_confirm_denied_and_approved():
    ran: list[str] = []
    registry = SkillRegistry()

    async def danger(**kw):
        ran.append("x")
        return SkillResult(ok=True, detail="did it")

    registry.register(
        SkillManifest(name="skill.test.danger", description="d", require_confirm=True), danger
    )

    async def deny(skill, params):
        return False

    async def approve(skill, params):
        return True

    plan = Plan(steps=[step("s1", "skill.test.danger", require_confirm=True)])
    denied = await collect(DagExecutor(registry, confirm=deny), plan)
    assert denied[0].status == StepStatus.NEED_CONFIRM and ran == []
    approved = await collect(DagExecutor(registry, confirm=approve), Plan(steps=[
        step("s1", "skill.test.danger", require_confirm=True)
    ]))
    assert approved[0].status == StepStatus.OK and ran == ["x"]


async def test_verify_unsat_report_annotates():
    class FarSnap:
        objects = {}
        regions = {}

    async def claims_ok(**kw):
        return SkillResult(ok=True, detail="claimed", data={})

    registry = SkillRegistry()
    registry.register(
        SkillManifest(
            name="skill.test.claim", description="c",
            verification={"mode": "schema", "expect": {"data_keys": ["object"]}},
        ),
        claims_ok,
    )
    ex = DagExecutor(registry)
    results = await collect(ex, Plan(steps=[step("s1", "skill.test.claim",
                                                 verification={"mode": "schema", "expect": {"data_keys": ["object"]}})]))
    assert results[0].status == StepStatus.OK  # honesty via annotation, not status flip
    assert results[0].data["_verify"]["verdict"] == "unsat"


async def test_verify_unsat_retry_once_then_report():
    attempts: list[int] = []

    async def flaky(**kw):
        attempts.append(1)
        return SkillResult(ok=True, data={} if len(attempts) == 1 else {"object": "x"})

    registry = SkillRegistry()
    v = {"mode": "schema", "expect": {"data_keys": ["object"]}, "on_fail": "retry", "max_attempts": 1}
    registry.register(SkillManifest(name="skill.test.flaky", description="f", verification=v), flaky)
    ex = DagExecutor(registry)
    results = await collect(ex, Plan(steps=[step("s1", "skill.test.flaky", verification=v)]))
    assert len(attempts) == 2  # one retry
    assert results[0].status == StepStatus.OK and "_verify" not in results[0].data
