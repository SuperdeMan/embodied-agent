# Ported from car-agent orchestrator/cloud/tests/test_context_scopes.py @ f0b08f8, changes: dispatcher passthrough test dropped (dispatcher not ported); filter tests retargeted to standalone strip_sensitive; origin embodiment-state scope key renamed robot_state (battery context key now robot_battery); vision scope coverage added.
"""敏感上下文按 context_scopes 声明最小化下发（strip 机制）。"""
from embodied.runtime.privacy import CONTEXT_SCOPES, SENSITIVE_SCOPE, strip_sensitive


def test_no_filter_when_scopes_none():
    """context_scopes=None（legacy/本地路径）→ 不过滤，保持既有行为。"""
    prefs = {"current_lat": "39.9", "robot_battery": "80", "answer_length": "short"}
    out = strip_sensitive(prefs, None)
    assert out["current_lat"] == "39.9"
    assert out["robot_battery"] == "80"
    assert out["answer_length"] == "short"


def test_drops_sensitive_when_no_scope_declared():
    """声明为空（未声明任何敏感 scope）→ 精确位置/电量/视觉引用全部剔除；非敏感偏好保留。"""
    prefs = {"current_lat": "39.9", "current_lng": "116.4",
             "current_accuracy_m": "10", "robot_battery": "80",
             "vision_frame_id": "f-1",
             "answer_length": "short", "model_pref": "fast"}
    out = strip_sensitive(prefs, [])
    assert "current_lat" not in out
    assert "current_lng" not in out
    assert "current_accuracy_m" not in out
    assert "robot_battery" not in out
    assert "vision_frame_id" not in out
    assert out["answer_length"] == "short"   # 非敏感保留
    assert out["model_pref"] == "fast"


def test_keeps_location_when_declared():
    prefs = {"current_lat": "39.9", "current_lng": "116.4", "robot_battery": "80"}
    out = strip_sensitive(prefs, ["location"])
    assert out["current_lat"] == "39.9"
    assert out["current_lng"] == "116.4"
    assert "robot_battery" not in out        # 未声明 robot_state


def test_keeps_battery_when_robot_state_declared():
    prefs = {"current_lat": "39.9", "robot_battery": "80"}
    out = strip_sensitive(prefs, ["robot_state"])
    assert "current_lat" not in out          # 未声明 location
    assert out["robot_battery"] == "80"


def test_keeps_vision_frame_when_declared():
    prefs = {"vision_frame_id": "f-1", "robot_battery": "80"}
    out = strip_sensitive(prefs, ["vision"])
    assert out["vision_frame_id"] == "f-1"
    assert "robot_battery" not in out


def test_input_mapping_not_mutated_and_none_tolerated():
    prefs = {"robot_battery": "80", "answer_length": "short"}
    strip_sensitive(prefs, [])
    assert prefs == {"robot_battery": "80", "answer_length": "short"}
    assert strip_sensitive(None, ["location"]) == {}


def test_scope_vocabulary_is_closed():
    assert CONTEXT_SCOPES == {"location", "robot_state", "vision"}
    assert set(SENSITIVE_SCOPE.values()) == CONTEXT_SCOPES
