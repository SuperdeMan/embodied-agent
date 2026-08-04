"""Contract tests pinning the two registry invariants (see registry.py docstring).

If one of these fails, the architecture regressed — fix the code, never the test.
"""

from __future__ import annotations

import asyncio

import pytest

from embodied.skills.manifest import ParamSpec, SkillManifest, TerminationSpec
from embodied.skills.registry import (
    ConfirmationRequired,
    DuplicateSkill,
    InvalidParams,
    SkillNotFound,
    SkillRegistry,
    SkillResult,
)


def _manifest(name="skill.test.noop", **kw) -> SkillManifest:
    base = {"name": name, "description": "test skill"}
    base.update(kw)
    return SkillManifest.model_validate(base)


async def _ok(**kw) -> SkillResult:
    return SkillResult(ok=True, detail="done", data=kw)


@pytest.fixture
def registry() -> SkillRegistry:
    r = SkillRegistry()
    r.register(_manifest(), _ok)
    return r


async def test_register_and_invoke(registry):
    result = await registry.invoke("skill.test.noop")
    assert result.ok and result.detail == "done"


async def test_duplicate_rejected(registry):
    with pytest.raises(DuplicateSkill):
        registry.register(_manifest(), _ok)


async def test_sync_handler_rejected(registry):
    def sync_handler(**kw):
        return SkillResult(ok=True)

    with pytest.raises(Exception):
        registry.register(_manifest(name="skill.test.sync"), sync_handler)


async def test_unknown_skill(registry):
    with pytest.raises(SkillNotFound):
        await registry.invoke("skill.test.missing")


async def test_confirm_gate_cannot_be_bypassed(registry):
    """SG-3: the gate lives in the registry, below any planner bug."""
    executed: list[str] = []

    async def dangerous() -> SkillResult:
        executed.append("ran")
        return SkillResult(ok=True)

    registry.register(_manifest(name="skill.test.danger", require_confirm=True), dangerous)
    with pytest.raises(ConfirmationRequired):
        await registry.invoke("skill.test.danger")
    assert executed == []
    result = await registry.invoke("skill.test.danger", confirmed=True)
    assert result.ok and executed == ["ran"]


async def test_param_validation(registry):
    registry.register(
        _manifest(
            name="skill.test.params",
            params={
                "target": ParamSpec(type="string"),
                "times": ParamSpec(type="integer", required=False, default=2),
            },
        ),
        _ok,
    )
    result = await registry.invoke("skill.test.params", {"target": "cube"})
    assert result.data == {"target": "cube", "times": 2}
    with pytest.raises(InvalidParams):
        await registry.invoke("skill.test.params", {})
    with pytest.raises(InvalidParams):
        await registry.invoke("skill.test.params", {"target": "cube", "bogus": 1})


async def test_timeout_is_operational_failure(registry):
    async def slow() -> SkillResult:
        await asyncio.sleep(0.5)
        return SkillResult(ok=True)

    registry.register(
        _manifest(name="skill.test.slow", termination=TerminationSpec(timeout_s=0.05)),
        slow,
    )
    result = await registry.invoke("skill.test.slow")
    assert not result.ok
    assert "timeout" in result.detail


async def test_handler_fault_is_operational_failure(registry):
    async def boom() -> SkillResult:
        raise ValueError("kaboom")

    registry.register(_manifest(name="skill.test.boom"), boom)
    result = await registry.invoke("skill.test.boom")
    assert not result.ok
    assert "ValueError" in result.detail


async def test_extension_seam_no_core_changes(registry):
    """加技能不动内核: a fresh manifest+handler shows up in the LLM-facing catalog at once."""
    registry.register(_manifest(name="skill.test.extra", description="added at runtime"), _ok)
    names = [m.name for m in registry.catalog()]
    assert "skill.test.extra" in names
    tools = {t["function"]["name"] for t in registry.tool_schemas()}
    assert "skill-test-extra" in tools
    assert registry.resolve_tool("skill-test-extra") == "skill.test.extra"
