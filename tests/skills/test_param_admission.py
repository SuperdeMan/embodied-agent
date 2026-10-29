"""Untrusted inputs must be rejected before a handler can produce side effects."""

import pytest

from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import InvalidParams, SkillRegistry, SkillResult


@pytest.mark.parametrize(("spec", "value"), [
    (ParamSpec(type="integer"), True), (ParamSpec(type="integer"), "2"),
    (ParamSpec(type="number"), float("nan")), (ParamSpec(type="number"), float("inf")),
    (ParamSpec(type="boolean"), 1), (ParamSpec(type="string", enum=["cube"]), "arm"),
    (ParamSpec(type="number", minimum=0, maximum=1), 2),
    (ParamSpec(type="array"), [0, {"bad": float("nan")}]),
    (ParamSpec(type="object"), {1: "non-string-key"}),
    (ParamSpec(type="string"), None), (ParamSpec(type="array"), (1, 2)),
])
async def test_invalid_value_never_dispatches(spec, value):
    calls = []

    async def action(**params):
        calls.append(params)
        return SkillResult(ok=True)

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.input", description="input", params={"x": spec}), action)
    with pytest.raises(InvalidParams):
        await registry.invoke("skill.test.input", {"x": value})
    assert not calls


@pytest.mark.parametrize("params", [[], "", 0, False, {1: 2}])
async def test_non_object_params_are_not_coerced_to_empty(params):
    async def action():
        raise AssertionError("must not dispatch")

    registry = SkillRegistry()
    registry.register(SkillManifest(name="skill.test.input", description="input"), action)
    with pytest.raises(InvalidParams):
        await registry.invoke("skill.test.input", params)


def test_defaults_limits_and_tool_schema_agree():
    with pytest.raises(ValueError):
        ParamSpec(type="integer", default="2")
    with pytest.raises(ValueError):
        ParamSpec(type="number", minimum=2, maximum=1)
    for timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            TerminationSpec(timeout_s=timeout)
    manifest = SkillManifest(
        name="skill.test.input", description="input",
        params={"x": ParamSpec(type="number", minimum=0, maximum=1, default=0.5)},
    )
    schema = manifest.to_tool_schema()["function"]["parameters"]
    assert schema["properties"]["x"]["maximum"] == 1
    assert schema["additionalProperties"] is False
    assert SkillRegistry._validate_params(manifest, {}) == {"x": 0.5}
