# Fresh tests for embodied.runtime.testing (the origin agents/_sdk/testing.py shipped without direct tests; ported helpers get coverage here).
"""testing helpers：manifest / result 形状断言（鸭子类型）。"""
from types import SimpleNamespace

import pytest

from embodied.runtime.testing import assert_manifest_consistent, assert_result_valid


def _manifest(**over):
    m = SimpleNamespace(
        agent_id="skill.manip.pick",
        version="0.1.0",
        category="core",
        trust_level="first_party",
        deployment="edge",
        capabilities=[SimpleNamespace(intent="manip.pick")],
    )
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _holder(manifest):
    return SimpleNamespace(manifest=manifest)


def test_manifest_consistent_ok():
    assert assert_manifest_consistent(_holder(_manifest())) is True


@pytest.mark.parametrize("field,value", [
    ("agent_id", ""),
    ("version", ""),
    ("category", "misc"),
    ("trust_level", "root"),
    ("deployment", "orbit"),
    ("capabilities", []),
])
def test_manifest_inconsistent_fields_raise(field, value):
    with pytest.raises(AssertionError):
        assert_manifest_consistent(_holder(_manifest(**{field: value})))


def test_manifest_intent_must_be_domain_action():
    bad = _manifest(capabilities=[SimpleNamespace(intent="pick")])
    with pytest.raises(AssertionError):
        assert_manifest_consistent(_holder(bad))


def _result(**over):
    r = SimpleNamespace(
        speech="好的，已完成",
        status="ok",
        actions=[{"type": "arm.move", "payload": {"dx": 0.1}}],
    )
    for k, v in over.items():
        setattr(r, k, v)
    return r


def test_result_valid_ok():
    assert_result_valid(_result())
    assert_result_valid(_result(), expected_status="ok")


def test_result_empty_speech_raises():
    with pytest.raises(AssertionError):
        assert_result_valid(_result(speech=""))


def test_result_status_mismatch_raises():
    with pytest.raises(AssertionError):
        assert_result_valid(_result(status="failed"), expected_status="ok")


def test_result_action_without_type_raises():
    with pytest.raises(AssertionError):
        assert_result_valid(_result(actions=[{"payload": {}}]))
