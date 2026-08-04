# Ported from car-agent security/tests/test_permission.py @ f0b08f8, changes: scope fixtures rewritten to the robot catalog (arm/gripper/camera/mic/speaker/memory/net); SlotValidator tests dropped (injection module not ported); check_action coverage added for the new map.
"""安全模块单元测试：scope 覆盖、trust 上限、权限引擎、运行时唯一决策。"""
import pytest

from embodied.safety.permission import (
    AuthContext,
    PermissionEngine,
    check_permission,
)
from embodied.safety.scopes import (
    ALL_SCOPES,
    ARM_HOME,
    ARM_MOVE,
    CAMERA_READ,
    GRIPPER_CLOSE,
    GRIPPER_OPEN,
    MEMORY_READ,
    MEMORY_WRITE,
    MIC_LISTEN,
    NET_FETCH,
    SPEAKER_SAY,
    TRUST_LEVEL_CAPS,
    deny_third_party,
    is_scope_covered,
)

# ─── Scope 覆盖测试 ───


def test_parent_covers_child():
    assert is_scope_covered("arm.move", {"arm"}) is True


def test_exact_match():
    assert is_scope_covered("arm.move", {"arm.move"}) is True


def test_sibling_not_cover():
    assert is_scope_covered("gripper.open", {"gripper.close"}) is False


def test_child_not_cover_parent():
    assert is_scope_covered("gripper", {"gripper.open"}) is False


def test_empty_effective():
    assert is_scope_covered("arm.move", set()) is False


# ─── trust_level 上限测试 ───


def test_system_has_all():
    assert TRUST_LEVEL_CAPS["system"] == set(ALL_SCOPES)


def test_third_party_no_actuation():
    for scope in (ARM_MOVE, ARM_HOME, GRIPPER_OPEN, GRIPPER_CLOSE):
        assert scope not in TRUST_LEVEL_CAPS["third_party"]


def test_third_party_no_raw_sensors_or_memory_write():
    assert CAMERA_READ not in TRUST_LEVEL_CAPS["third_party"]
    assert MIC_LISTEN not in TRUST_LEVEL_CAPS["third_party"]
    assert MEMORY_WRITE not in TRUST_LEVEL_CAPS["third_party"]


def test_third_party_can_speak_read_and_fetch():
    assert SPEAKER_SAY in TRUST_LEVEL_CAPS["third_party"]
    assert MEMORY_READ in TRUST_LEVEL_CAPS["third_party"]
    assert NET_FETCH in TRUST_LEVEL_CAPS["third_party"]


def test_first_party_has_actuation_and_camera_but_not_mic():
    for scope in (ARM_MOVE, GRIPPER_CLOSE, CAMERA_READ, MEMORY_WRITE):
        assert scope in TRUST_LEVEL_CAPS["first_party"]
    assert MIC_LISTEN not in TRUST_LEVEL_CAPS["first_party"]


def test_deny_third_party_strips_actuation_and_sensors():
    assert deny_third_party({ARM_MOVE, GRIPPER_OPEN, CAMERA_READ, MIC_LISTEN,
                             SPEAKER_SAY, NET_FETCH}) == {SPEAKER_SAY, NET_FETCH}


# ─── PermissionEngine 测试 ───


class MockManifest:
    def __init__(self, agent_id="test", trust_level="first_party"):
        self.agent_id = agent_id
        self.trust_level = trust_level


def test_check_allowed():
    engine = PermissionEngine()
    auth = AuthContext(token_scopes=[CAMERA_READ, SPEAKER_SAY])
    d = engine.check(MockManifest(), [CAMERA_READ], auth)
    assert d.allowed is True


def test_check_denied():
    engine = PermissionEngine()
    auth = AuthContext(token_scopes=[CAMERA_READ])
    d = engine.check(MockManifest(), [NET_FETCH], auth)
    assert d.allowed is False
    assert NET_FETCH in d.missing


def test_third_party_denied_actuation_in_effective_scopes():
    engine = PermissionEngine()
    auth = AuthContext(token_scopes=[ARM_MOVE, CAMERA_READ, SPEAKER_SAY])
    m = MockManifest(trust_level="third_party")
    # third_party 即使 token 授予了执行器/相机也被剔除
    eff = engine.effective_scopes(m, auth)
    assert ARM_MOVE not in eff
    assert CAMERA_READ not in eff
    assert SPEAKER_SAY in eff


def test_user_grants_merged():
    engine = PermissionEngine()
    auth = AuthContext(
        token_scopes=[CAMERA_READ],
        user_grants={"test": [SPEAKER_SAY]},
    )
    eff = engine.effective_scopes(MockManifest(), auth)
    assert CAMERA_READ in eff
    assert SPEAKER_SAY in eff


def test_empty_required_always_allowed():
    engine = PermissionEngine()
    d = engine.check(MockManifest(), [], AuthContext())
    assert d.allowed is True


# ─── check_permission：运行时唯一权限决策（规划期过滤 + 执行期同源）───


@pytest.mark.parametrize("trust,required,granted,kind,allowed", [
    ("first_party", [], [], "agent", True),                                   # 无 required 放行
    ("first_party", ["arm.move"], ["arm"], "agent", True),                    # 父覆盖子
    ("first_party", ["gripper.open"], ["gripper.close"], "agent", False),     # 兄弟不覆盖
    ("third_party", ["arm.move"], ["arm"], "agent", False),                   # 第三方执行器硬禁（虽授权）
    ("first_party", ["arm.move"], ["arm.move"], "tool", False),               # 工具执行器硬禁（虽授权）
    ("first_party", ["arm.move"], ["arm.move"], "agent", True),               # first_party 授权可执行
    ("third_party", ["gripper.close"], ["gripper.close"], "agent", False),    # 第三方夹爪同样硬禁
    ("first_party", ["net.fetch"], ["camera.read"], "agent", False),          # 缺权
    ("first_party", ["net.fetch", "camera.read"],
     ["net.fetch", "camera.read"], "agent", True),                            # 全覆盖
])
def test_check_permission_contract(trust, required, granted, kind, allowed):
    d = check_permission(agent_id="x", trust_level=trust, required=required,
                         granted=granted, kind=kind)
    assert d.allowed is allowed


def test_check_permission_missing_lists_scopes():
    d = check_permission(agent_id="x", trust_level="first_party",
                         required=[NET_FETCH], granted=[CAMERA_READ])
    assert d.allowed is False
    assert NET_FETCH in d.missing
    assert "missing permissions" in d.reason


def test_check_permission_third_party_actuation_reason():
    d = check_permission(agent_id="x", trust_level="third_party",
                         required=[ARM_MOVE], granted=[ARM_MOVE])
    assert d.allowed is False
    assert ARM_MOVE in d.missing
    assert "third_party" in d.reason


def test_check_permission_tool_actuation_reason():
    d = check_permission(agent_id="x", trust_level="first_party",
                         required=[GRIPPER_CLOSE], granted=[GRIPPER_CLOSE],
                         kind="tool")
    assert d.allowed is False
    assert "tools cannot" in d.reason


def test_permission_engine_check_delegates_to_check_permission():
    """PermissionEngine.check 委托 check_permission：token∪user_grants 作 granted。"""
    engine = PermissionEngine()
    m = MockManifest(trust_level="first_party")
    auth = AuthContext(token_scopes=[NET_FETCH],
                       user_grants={"test": [CAMERA_READ]})
    d = engine.check(m, [NET_FETCH, CAMERA_READ], auth)
    assert d.allowed is True


# ─── check_action：执行层动作级校验 ───


def test_check_action_requires_matching_scope():
    engine = PermissionEngine()
    auth = AuthContext(token_scopes=[ARM_MOVE])
    assert engine.check_action("arm.move", MockManifest(), auth).allowed is True
    d = engine.check_action("gripper.close", MockManifest(), auth)
    assert d.allowed is False and GRIPPER_CLOSE in d.missing


def test_check_action_parent_grant_covers():
    engine = PermissionEngine()
    auth = AuthContext(token_scopes=["arm"])
    assert engine.check_action("arm.home", MockManifest(), auth).allowed is True


def test_check_action_third_party_actuation_hard_denied():
    engine = PermissionEngine()
    auth = AuthContext(token_scopes=[ARM_MOVE])
    m = MockManifest(trust_level="third_party")
    assert engine.check_action("arm.move", m, auth).allowed is False


def test_check_action_unmapped_type_allowed():
    engine = PermissionEngine()
    assert engine.check_action("ui.card", MockManifest(), AuthContext()).allowed is True
