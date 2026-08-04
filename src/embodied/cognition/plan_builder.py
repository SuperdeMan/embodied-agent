# Ported from car-agent orchestrator/cloud/planning.py @ f0b08f8, changes: capabilities are
# SkillManifests from SkillRegistry (agent_id/intent → skill; re-homing dropped — skill names
# are globally unique by construction); prompt rewritten for a desktop robot planner (Chinese
# domain prompt kept, same rationale as origin: planning happens in the user's language);
# addressed/clarify/emotion/route-hints/skills-KB/exemplars channels not carried (voice UX
# arrives with the console; knowledge stores are M2+); fallback is a direct chat reply instead
# of a chitchat agent; atomic plan rejection, depends_on sanitation, ref-derived dependencies,
# two-attempt toolcall-with-salvage loop and the manifest authority chain ported faithfully.
"""PlanBuilder: LLM → submit_plan toolcall → validated DAG plan.

Iron rules pinned by contract tests:
- Plans are ATOMIC: one invalid step rejects the whole plan (silently executing a valid
  remainder drops user intents and falsely reports completion).
- `require_confirm` / `verification` / `timeout_s` are populated from the SkillManifest;
  same-named fields in LLM output are never read (authority chain, D009).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from embodied.cognition.plan import Plan, Step
from embodied.skills.manifest import SkillManifest
from embodied.skills.registry import SkillRegistry

logger = logging.getLogger("cognition.plan_builder")

SUBMIT_PLAN = "submit_plan"

_PLANNER_SYSTEM = (
    "你是一台桌面机器人的任务规划器。根据用户话术、可用技能清单和当前世界状态，"
    "调用 submit_plan 工具提交 JSON 执行计划；这是唯一合法的动作输出通道。\n"
    "- 每个 step: {\"id\":\"s1\",\"skill\":\"skill.manip.pick\",\"params\":{},"
    "\"depends_on\":[],\"param_refs\":{}}\n"
    "- 无数据依赖的步骤各自 depends_on=[]（会并行执行）；有依赖时用 depends_on + "
    "param_refs 引用前序结果（如 {\"object\":\"s1.data.object\"}）\n"
    "- complexity: simple=一次可确定全部步骤；adaptive=必须根据运行结果决定下一步\n"
    "- goal: 一句话目标（adaptive 时是再规划的锚点）\n"
    "- params 只填用户话术或世界状态里真实存在的值；没有就省略该键让默认值生效，"
    "绝不编造占位值\n"
    "- 世界状态里不存在的物体不要规划操作；纯聊天/问答不需要动作时提交空 steps，"
    "随后用一句话直接回答\n"
    "- 只通过工具提交，不要文本输出 JSON，不要解释"
)


def _skill_catalog(manifests: list[SkillManifest]) -> str:
    lines = []
    for m in manifests:
        params = ", ".join(
            f"{name}:{spec.type}{'' if spec.required else '?'}" for name, spec in m.params.items()
        )
        confirm = " [需确认]" if m.require_confirm else ""
        lines.append(f"- {m.name}({params}){confirm} — {m.description}")
    return "\n".join(lines)


def submit_plan_tools() -> list[dict]:
    """OpenAI-style tools list for BaseProvider.complete_tools. Schema top level = the plan
    dict consumed by validation — zero semantic drift between wire and validation. No
    require_confirm in schema: confirmation authority is never the LLM's (origin M0a)."""
    step_props = {
        "id": {"type": "string"},
        "skill": {"type": "string"},
        "params": {
            "type": "object",
            "description": "该步骤的全部参数键值；省略式追问必须把继承参数与变化参数一起写全",
        },
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "param_refs": {"type": "object", "description": "参数对前序结果的引用，如 {\"object\":\"s1.data.object\"}"},
    }
    props = {
        "complexity": {"type": "string", "enum": ["simple", "adaptive"]},
        "goal": {"type": "string", "description": "一句话目标"},
        "steps": {
            "type": "array",
            "items": {"type": "object", "properties": step_props, "required": ["id", "skill"]},
        },
    }
    return [{
        "type": "function",
        "function": {
            "name": SUBMIT_PLAN,
            "description": "提交本轮规划结果。这是唯一合法的动作输出通道。",
            "parameters": {"type": "object", "properties": props, "required": ["steps"]},
        },
    }]


_REPLAN_SYSTEM = (
    "你是桌面机器人有界任务循环的再规划器。根据用户目标、最近观察和可用技能，"
    "调用 submit_plan 提交下一批步骤；任务已完成或无法推进时提交空 steps。"
    "仅在必要时改变计划；不得输出解释。"
)

CompleteTools = Callable[..., Awaitable[tuple]]


class PlanBuilder:
    def __init__(self, provider: Any, registry: SkillRegistry, *, model: str = "",
                 temperature: float = 0.1, max_tokens: int = 2048) -> None:
        self._provider = provider
        self._registry = registry
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def build(self, text: str, context_block: str = "") -> Plan:
        """Two attempts; each: toolcall channel first, same-round JSON salvage from content.
        Still nothing valid → empty plan marked degraded_chat (engine answers conversationally)."""
        manifests = self._registry.catalog()
        by_name = {m.name: m for m in manifests}
        messages = [
            {"role": "system", "content": _PLANNER_SYSTEM},
            {"role": "user", "content": self._user_msg(text, manifests, context_block)},
        ]
        last_raw = ""
        no_action = 0
        for _attempt in range(2):
            raw, args = await self._call(messages)
            last_raw = raw or last_raw
            mode = "toolcall"
            if args is None:
                data = self._extract_data(raw)
                mode = "toolcall_salvage"
            else:
                data = args
            if not isinstance(data, dict):
                continue
            steps = self._validated_steps(data.get("steps", []) or [], by_name)
            if steps:
                plan = self._assemble(steps, data, text)
                plan.plan_mode = mode
                plan.raw_llm = last_raw
                return plan
            if not (data.get("steps") or []):
                no_action += 1

        if no_action >= 2:
            # The model said "no action needed" twice: that's its answer, not a failure —
            # second-guessing an explicit answer is what the origin learned NOT to do.
            return Plan(steps=[], raw_text=text, plan_mode="no_action_chat", raw_llm=last_raw)
        logger.warning("plan parse failed twice, degrading to chat")
        return Plan(steps=[], raw_text=text, plan_mode="degraded_chat", raw_llm=last_raw)

    async def replan(self, goal: str, observations: list[dict], context_block: str = "") -> Plan:
        manifests = self._registry.catalog()
        by_name = {m.name: m for m in manifests}
        user = (
            f"目标：{goal}\n最近观察：{json.dumps(observations, ensure_ascii=False, default=str)}\n"
            f"{context_block}可用技能:\n{_skill_catalog(manifests)}"
        )
        messages = [
            {"role": "system", "content": _REPLAN_SYSTEM},
            {"role": "user", "content": user},
        ]
        raw, args = await self._call(messages)
        data = args if isinstance(args, dict) else self._extract_data(raw)
        if not isinstance(data, dict):
            return Plan(steps=[], raw_text=goal, plan_mode="degraded_chat", raw_llm=raw)
        steps = self._validated_steps(data.get("steps", []) or [], by_name)
        plan = self._assemble(steps, data, goal)
        plan.complexity = "adaptive"
        plan.raw_llm = raw
        return plan

    # -- wire ------------------------------------------------------------------

    async def _call(self, messages: list[dict]) -> tuple[str, dict | None]:
        try:
            content, _used, _finish, _usage, calls = await self._provider.complete_tools(
                messages, self._model, self._temperature, self._max_tokens,
                tools=submit_plan_tools(),
                tool_choice={"type": "function", "function": {"name": SUBMIT_PLAN}},
            )
        except Exception as e:
            logger.warning("plan toolcall exception: %s", e)
            return "", None
        args = next(
            (c.get("arguments") for c in (calls or [])
             if isinstance(c, dict) and c.get("name") == SUBMIT_PLAN
             and isinstance(c.get("arguments"), dict)),
            None,
        )
        return (content or ""), args

    @staticmethod
    def _user_msg(text: str, manifests: list[SkillManifest], context_block: str) -> str:
        ctx = f"{context_block}\n\n" if context_block else ""
        return f"可用技能:\n{_skill_catalog(manifests)}\n\n{ctx}用户说: {text}"

    @staticmethod
    def _extract_data(raw: str) -> dict | None:
        if not raw:
            return None
        i, j = raw.find("{"), raw.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            data = json.loads(raw[i:j + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # -- validation (atomic) ---------------------------------------------------

    def _validated_steps(self, raw_steps: Any, by_name: dict[str, SkillManifest]) -> list[Step]:
        if not isinstance(raw_steps, list):
            return []
        steps: list[Step] = []
        invalid = False
        for s in raw_steps:
            if not isinstance(s, dict):
                logger.warning("plan step is %s (not object), rejecting plan", type(s).__name__)
                invalid = True
                continue
            skill = str(s.get("skill", "") or "")
            manifest = by_name.get(skill)
            if manifest is None:
                logger.warning("unknown skill in plan: %r, rejecting plan", skill)
                invalid = True
                continue
            raw_params = s.get("params") or {}
            if not isinstance(raw_params, dict):
                logger.warning("step params is %s (not dict), rejecting plan", type(raw_params).__name__)
                invalid = True
                continue
            raw_refs = s.get("param_refs") or {}
            refs = (
                {k: v for k, v in raw_refs.items() if isinstance(v, str)}
                if isinstance(raw_refs, dict) else {}
            )
            unknown = set(raw_params) | set(refs)
            unknown -= set(manifest.params)
            if unknown:
                logger.warning("step %s has unknown params %s, rejecting plan", skill, sorted(unknown))
                invalid = True
                continue
            missing = [
                name for name, spec in manifest.params.items()
                if spec.required and spec.default is None
                and name not in raw_params and name not in refs
            ]
            if missing:
                logger.warning("step %s missing required params %s, rejecting plan", skill, missing)
                invalid = True
                continue
            deps_raw = s.get("depends_on")
            deps = [d for d in deps_raw if isinstance(d, str)] if isinstance(deps_raw, list) else []
            params = dict(raw_params)
            # Fill manifest defaults for omitted params so the step carries its EFFECTIVE
            # values: $param: verification refs must see what the skill will actually use,
            # not just what the LLM bothered to write.
            for pname, spec in manifest.params.items():
                if pname not in params and pname not in refs and spec.default is not None:
                    params[pname] = spec.default
            steps.append(Step(
                id=str(s.get("id") or f"s{len(steps) + 1}"),
                skill=skill,
                params=params,
                depends_on=deps,
                param_refs=refs,
                # Authority chain: manifest only. LLM-supplied require_confirm/verification
                # keys in the step dict are unknown-params-rejected above anyway.
                require_confirm=manifest.require_confirm,
                timeout_s=manifest.termination.timeout_s,
                verification=dict(manifest.verification or {}),
            ))
        if invalid:
            return []

        valid_ids = {st.id for st in steps}
        if len(valid_ids) != len(steps):
            logger.warning("duplicate step ids, rejecting plan")
            return []
        for st in steps:
            st.depends_on = [d for d in st.depends_on if d in valid_ids and d != st.id]
        self._derive_depends_on_from_refs(steps, valid_ids)
        return steps

    # Referencing another step's output IS the definition of a dependency; models often
    # write the ref but leave depends_on empty — that's a self-contradictory plan, and the
    # topo sort would run both in one parallel layer. Repair the contradiction; never
    # invent routing (producer must exist in-plan; self-reference is a cycle, not an edge).
    _REF_HEAD_RE = re.compile(r"^\$?\{?\s*([A-Za-z_][A-Za-z0-9_]*)\.data\.")

    @classmethod
    def _derive_depends_on_from_refs(cls, steps: list[Step], valid_ids: set[str]) -> None:
        for step in steps:
            derived = []
            for raw in list(step.param_refs.values()) + list(step.params.values()):
                if not isinstance(raw, str):
                    continue
                m = cls._REF_HEAD_RE.match(raw.strip())
                if not m:
                    continue
                producer = m.group(1)
                if (
                    producer in valid_ids and producer != step.id
                    and producer not in step.depends_on and producer not in derived
                ):
                    derived.append(producer)
            if derived:
                logger.info("step %s references %s but declared depends_on=%s; deriving edges",
                            step.id, derived, step.depends_on)
                step.depends_on = list(step.depends_on) + derived

    @staticmethod
    def _assemble(steps: list[Step], data: dict, text: str) -> Plan:
        complexity = data.get("complexity", "simple")
        if complexity not in ("simple", "adaptive"):
            complexity = "simple"
        return Plan(
            steps=steps, raw_text=text, complexity=complexity,
            goal=str(data.get("goal", "") or ""),
        )
