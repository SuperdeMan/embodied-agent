"""PlanBuilder contract tests: atomic rejection, authority chain, ref-derived edges,
toolcall-with-salvage degradation."""

from __future__ import annotations

import json

from embodied.cognition.plan_builder import SUBMIT_PLAN, PlanBuilder
from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import SkillRegistry, SkillResult


async def _ok(**kw) -> SkillResult:
    return SkillResult(ok=True)


def make_registry() -> SkillRegistry:
    r = SkillRegistry()
    r.register(
        SkillManifest(
            name="skill.manip.pick", description="pick",
            params={"object": ParamSpec(type="string", required=False, default="obj_cube")},
            termination=TerminationSpec(timeout_s=42.0),
            verification={"mode": "schema", "expect": {"data_keys": ["object"]}},
        ),
        _ok,
    )
    r.register(
        SkillManifest(
            name="skill.manip.place", description="place",
            params={"region": ParamSpec(type="string", required=False, default="bin_region")},
        ),
        _ok,
    )
    r.register(
        SkillManifest(name="skill.system.power_off", description="off", require_confirm=True), _ok
    )
    r.register(
        SkillManifest(
            name="skill.test.strict", description="needs target",
            params={"target": ParamSpec(type="string")},  # required, no default
        ),
        _ok,
    )
    return r


class PlanOnce:
    """Provider double: returns the given submit_plan args (or raw content) once per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def complete_tools(self, messages, model, temperature, max_tokens, **kw):
        self.calls += 1
        item = self.responses.pop(0) if self.responses else ("", None)
        content, args = item
        calls = (
            [{"id": "1", "name": SUBMIT_PLAN, "arguments": args}] if isinstance(args, dict) else []
        )
        return content, "test", "tool_use" if calls else "stop", (0, 0), calls


def builder(responses, registry=None):
    return PlanBuilder(PlanOnce(responses), registry or make_registry())


async def test_valid_plan_carries_manifest_authority():
    plan = await builder(
        [("", {"goal": "g", "steps": [
            {"id": "s1", "skill": "skill.manip.pick", "params": {}},
            {"id": "s2", "skill": "skill.system.power_off", "params": {},
             "depends_on": ["s1"]},
        ]})]
    ).build("do it")
    assert [s.skill for s in plan.steps] == ["skill.manip.pick", "skill.system.power_off"]
    s1, s2 = plan.steps
    assert s1.timeout_s == 42.0 and s1.verification["mode"] == "schema"
    assert s1.params == {"object": "obj_cube"}  # manifest default filled in
    assert not s1.require_confirm and s2.require_confirm  # from manifest, not LLM
    assert plan.plan_mode == "toolcall"


async def test_llm_cannot_grant_or_revoke_confirm():
    """Authority chain: require_confirm in LLM output is an unknown param → atomic reject;
    it can never override the manifest."""
    plan = await builder(
        [("", {"steps": [{"id": "s1", "skill": "skill.system.power_off",
                          "params": {"require_confirm": False}}]}),
         ("", {"steps": []})]
    ).build("off")
    assert plan.steps == []  # rejected, degraded


async def test_atomic_rejection_unknown_skill():
    plan = await builder(
        [("", {"steps": [
            {"id": "s1", "skill": "skill.manip.pick", "params": {}},
            {"id": "s2", "skill": "skill.ghost.move", "params": {}},
        ]}),
         ("", {"steps": []})]
    ).build("x")
    assert plan.steps == []  # the valid remainder must NOT execute


async def test_atomic_rejection_unknown_and_missing_params():
    b = builder(
        [("", {"steps": [{"id": "s1", "skill": "skill.manip.pick", "params": {"bogus": 1}}]}),
         ("", {"steps": [{"id": "s1", "skill": "skill.test.strict", "params": {}}]})]
    )
    plan = await b.build("x")
    assert plan.steps == [] and plan.plan_mode == "degraded_chat"


async def test_required_param_satisfied_by_ref_passes():
    plan = await builder(
        [("", {"steps": [
            {"id": "s1", "skill": "skill.manip.pick", "params": {}},
            {"id": "s2", "skill": "skill.test.strict", "params": {},
             "param_refs": {"target": "s1.data.object"}},
        ]})]
    ).build("x")
    assert len(plan.steps) == 2
    assert plan.steps[1].depends_on == ["s1"]  # derived from the ref


async def test_depends_on_sanitized_and_ref_edges_derived():
    plan = await builder(
        [("", {"steps": [
            {"id": "s1", "skill": "skill.manip.pick", "params": {}, "depends_on": ["nope", 3]},
            {"id": "s2", "skill": "skill.manip.place",
             "params": {"region": "${s1.data.region}"}, "depends_on": []},
        ]})]
    ).build("x")
    assert plan.steps[0].depends_on == []
    assert plan.steps[1].depends_on == ["s1"]


async def test_duplicate_ids_rejected():
    plan = await builder(
        [("", {"steps": [
            {"id": "s1", "skill": "skill.manip.pick", "params": {}},
            {"id": "s1", "skill": "skill.manip.place", "params": {}},
        ]}),
         ("", {"steps": []})]
    ).build("x")
    assert plan.steps == []


async def test_salvage_from_text_content():
    raw = json.dumps({"steps": [{"id": "s1", "skill": "skill.manip.pick", "params": {}}]})
    plan = await builder([(f"noise {raw} noise", None)]).build("pick it")
    assert len(plan.steps) == 1 and plan.plan_mode == "toolcall_salvage"


async def test_no_action_twice_is_an_answer():
    plan = await builder([("", {"steps": []}), ("", {"steps": []})]).build("hello")
    assert plan.steps == [] and plan.plan_mode == "no_action_chat"


async def test_two_failures_degrade_to_chat():
    plan = await builder([("garbage", None), ("more garbage", None)]).build("hi")
    assert plan.steps == [] and plan.plan_mode == "degraded_chat"
