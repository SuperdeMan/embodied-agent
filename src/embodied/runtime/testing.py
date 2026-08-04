# Ported from car-agent agents/_sdk/testing.py @ f0b08f8, changes: kept the generic duck-typed assert helpers (assert_manifest_consistent / assert_result_valid); dropped agent-server-bound make_context / run_handle / run_handle_stream (they require the gRPC agent SDK's Context/IntentView types, not ported).
"""契约测试夹具：不起服务进程，直接对 manifest / result 形状做黄金用例断言。

用法（在技能测试中）::

    from embodied.runtime.testing import assert_manifest_consistent, assert_result_valid

    def test_manifest():
        assert assert_manifest_consistent(MySkill()) is True

两个 helper 都是鸭子类型：只要求对象带同名属性，不绑定具体 manifest/result 类。
"""
from __future__ import annotations


def assert_manifest_consistent(agent) -> bool:
    """校验 manifest 一致性：agent_id 存在、有 capabilities、category 合法。"""
    m = agent.manifest
    assert m.agent_id, "manifest.agent_id is empty"
    assert m.version, f"{m.agent_id}: manifest.version is empty"
    assert m.category in ("core", "ecosystem"), f"{m.agent_id}: invalid category {m.category}"
    assert m.trust_level in ("system", "first_party", "third_party"), \
        f"{m.agent_id}: invalid trust_level {m.trust_level}"
    assert m.deployment in ("edge", "cloud"), f"{m.agent_id}: invalid deployment {m.deployment}"
    assert len(m.capabilities) > 0, f"{m.agent_id}: no capabilities declared"
    for cap in m.capabilities:
        assert cap.intent, f"{m.agent_id}: capability has empty intent"
        assert "." in cap.intent, f"{m.agent_id}: intent '{cap.intent}' not in domain.action format"
    return True


def assert_result_valid(res, expected_status: str | None = None):
    """校验 result 结构合法性（speech 非空、status 匹配、action 带 type）。"""
    assert res.speech, "speech is empty"
    if expected_status:
        assert res.status == expected_status, f"status={res.status}, expected={expected_status}"
    for a in res.actions:
        assert "type" in a, f"action missing 'type': {a}"
