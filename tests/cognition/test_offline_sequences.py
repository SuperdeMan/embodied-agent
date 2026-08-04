"""Sequence rules in the scripted provider: one call per round, abort on failure —
the same cadence the planner sees from a real LLM."""

from __future__ import annotations

from embodied.cognition.offline import ScriptedToolProvider

TOOLS = [
    {"type": "function", "function": {"name": "skill-manip-pick"}},
    {"type": "function", "function": {"name": "skill-manip-place"}},
]
RULES = [(r"收拾|tidy", [("skill-manip-pick", {}), ("skill-manip-place", {})])]


async def _round(provider, messages):
    return await provider.complete_tools(messages, "", 0.0, 128, tools=TOOLS)


async def test_sequence_drains_one_call_per_round():
    p = ScriptedToolProvider(RULES)
    messages = [{"role": "user", "content": "收拾桌面"}]
    *_, calls = await _round(p, messages)
    assert [c["name"] for c in calls] == ["skill-manip-pick"]
    messages += [
        {"role": "assistant", "content": "(calling skills)"},
        {"role": "user", "content": "<tool_results>\n- skill.manip.pick: ok=True detail=grasped\n</tool_results>"},
    ]
    *_, calls2 = await _round(p, messages)
    assert [c["name"] for c in calls2] == ["skill-manip-place"]
    messages += [
        {"role": "assistant", "content": "(calling skills)"},
        {"role": "user", "content": "<tool_results>\n- skill.manip.place: ok=True detail=placed\n</tool_results>"},
    ]
    text, _, finish, _, calls3 = await _round(p, messages)
    assert calls3 == [] and finish == "stop" and "placed" in text


async def test_sequence_aborts_after_failed_step():
    p = ScriptedToolProvider(RULES)
    messages = [{"role": "user", "content": "tidy up"}]
    await _round(p, messages)
    messages += [
        {"role": "assistant", "content": "(calling skills)"},
        {"role": "user", "content": "<tool_results>\n- skill.manip.pick: ok=False detail=miss\n</tool_results>"},
    ]
    text, _, _, _, calls = await _round(p, messages)
    assert calls == []  # place is NOT attempted after pick failed
    assert "miss" in text


async def test_single_call_rules_still_work():
    p = ScriptedToolProvider([(r"home", "skill-arm-home", {})])
    tools = [{"type": "function", "function": {"name": "skill-arm-home"}}]
    *_, calls = await p.complete_tools([{"role": "user", "content": "go home"}], "", 0.0, 128, tools=tools)
    assert [c["name"] for c in calls] == ["skill-arm-home"]
