from __future__ import annotations

import pytest
from pydantic import ValidationError

from embodied.skills.manifest import ParamSpec, SkillManifest


def _m(**kw) -> SkillManifest:
    base = {"name": "skill.arm.wave", "description": "wave"}
    base.update(kw)
    return SkillManifest.model_validate(base)


def test_valid_names():
    for name in ("skill.arm.home", "skill.manip.pick", "skill.data_engine.flush", "skill.a1.b2"):
        assert _m(name=name).name == name


@pytest.mark.parametrize(
    "bad",
    ["arm.home", "skill.arm", "skill.Arm.home", "skill.arm.home.fast", "skill..home", "skill.arm.回零"],
)
def test_invalid_names_rejected(bad):
    with pytest.raises(ValidationError):
        _m(name=bad)


def test_tool_name_mapping_reversible():
    m = _m(name="skill.system.power_off")
    assert m.tool_name == "skill-system-power_off"
    assert m.tool_name.replace("-", ".") == m.name


def test_tool_schema_shape():
    m = _m(
        params={
            "times": ParamSpec(type="integer", description="how many", required=False, default=2),
            "target": ParamSpec(type="string", description="object id"),
        }
    )
    schema = m.to_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "skill-arm-wave"
    assert fn["parameters"]["properties"]["times"]["type"] == "integer"
    assert fn["parameters"]["required"] == ["target"]


def test_dangerous_skill_flagged_in_description():
    m = _m(require_confirm=True)
    assert "confirmation" in m.to_tool_schema()["function"]["description"]


def test_from_yaml_roundtrip(tmp_path):
    p = tmp_path / "wave.yaml"
    p.write_text(
        "name: skill.arm.wave\n"
        "description: wave the gripper\n"
        "require_confirm: false\n"
        "params:\n"
        "  times: {type: integer, required: false, default: 3}\n"
        "termination: {timeout_s: 5.0}\n",
        encoding="utf-8",
    )
    m = SkillManifest.from_yaml(p)
    assert m.name == "skill.arm.wave"
    assert m.params["times"].default == 3
    assert m.termination.timeout_s == 5.0
