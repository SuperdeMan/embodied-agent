"""agent-core entry points.

`embodied chat` — text conversation with mock skills (M0 DoD; no hardware, no keys).
`embodied sim`  — tabletop MuJoCo sim: chat control, or `--eval N` headless episodes
                  with optional episode recording (M1).
Voice/console and the process split attach in later M1 work.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any


def main(argv: list[str] | None = None) -> int:
    try:  # Windows may default pipes/console to a legacy codepage; interactive console IO is unaffected
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    parser = argparse.ArgumentParser(prog="embodied", description="Embodied agent runtime CLI")
    sub = parser.add_subparsers(dest="cmd")

    chat = sub.add_parser("chat", help="text-mode conversation with the agent core (mock skills)")
    _add_common(chat)

    sim = sub.add_parser("sim", help="tabletop simulation: chat control, or headless eval")
    _add_common(sim)
    sim.add_argument("--eval", type=int, default=0, metavar="N", help="run N randomized pick&place episodes, report success rate")
    sim.add_argument("--seed", type=int, default=0)
    sim.add_argument("--record", action="store_true", help="record episodes to outputs/episodes (EPISODES_DIR)")
    sim.add_argument("--snapshot", default="", help="save a scene PNG to this path on exit")

    args = parser.parse_args(argv)
    if args.cmd in (None, "chat"):
        return asyncio.run(_chat(getattr(args, "provider", "auto"), bool(getattr(args, "yes", False))))
    if args.cmd == "sim":
        return asyncio.run(_sim(args))
    parser.error(f"unknown command {args.cmd!r}")
    return 2


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "offline", "llm"],
        help="auto: use the configured LLM when LLM_API_KEY is set, otherwise the offline scripted provider",
    )
    p.add_argument("--yes", action="store_true", help="auto-confirm dangerous skills (demos only)")


def _build_provider(kind: str, plan_rules: Any = None) -> tuple[Any, str]:
    if kind == "offline" or (kind == "auto" and not os.getenv("LLM_API_KEY")):
        from embodied.cognition.offline import ScriptedPlanProvider

        return ScriptedPlanProvider(plan_rules), "offline-scripted"
    from embodied.providers import build_provider
    from embodied.providers.guarded import GuardedProvider

    p = GuardedProvider(build_provider())
    return p, f"guarded({type(p.inner).__name__})"


async def _repl(engine: Any, registry: Any, banner: str) -> int:
    print(banner)
    print("输入自然语言指令；/skills 列出技能；/exit 退出。")
    while True:
        try:
            line = (await asyncio.to_thread(input, "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/skills":
            for m in registry.catalog():
                flag = " [confirm]" if m.require_confirm else ""
                print(f"  {m.name}{flag} — {m.description}")
            continue
        turn = await engine.turn(line)
        for r in turn.results:
            plan_step = next((s for s in (turn.plan.steps if turn.plan else []) if s.id == r.step_id), None)
            skill = plan_step.skill if plan_step else r.step_id
            print(f"  [step] {r.step_id} {skill} → {r.status.value}{': ' + (r.detail or r.error) if (r.detail or r.error) else ''}")
        print(f"agent> {turn.text}")
    return 0


def _make_confirm(auto_yes: bool):
    async def confirm(skill: str, params: dict[str, Any]) -> bool:
        if auto_yes:
            print(f"[confirm] {skill} auto-approved (--yes)")
            return True
        ans = await asyncio.to_thread(input, f"[confirm] 执行危险技能 {skill}? (y/N) ")
        return ans.strip().lower() in ("y", "yes")

    return confirm


async def _chat(kind: str, auto_yes: bool) -> int:
    from embodied.cognition.engine import PlannerEngine
    from embodied.cognition.offline import CHAT_PLAN_RULES
    from embodied.skills.builtin.mock import register_builtin
    from embodied.skills.registry import SkillRegistry

    registry = SkillRegistry()
    register_builtin(registry)
    provider, pname = _build_provider(kind, plan_rules=CHAT_PLAN_RULES)
    engine = PlannerEngine(provider, registry, confirm=_make_confirm(auto_yes))
    return await _repl(engine, registry, f"embodied agent-core · provider={pname} · skills={len(registry.catalog())}")


def _make_sim_engine(sim: Any, registry: Any, provider: Any, confirm: Any) -> Any:
    from embodied.cognition.engine import PlannerEngine
    from embodied.cognition.world_state import WorldSnapshot

    def world_fn() -> Any:
        return WorldSnapshot.from_observation(sim.read())

    def context_fn() -> str:
        snap = world_fn()
        objs = ", ".join(f"{k}@({v.pos[0]:.2f},{v.pos[1]:.2f},{v.pos[2]:.2f})" for k, v in snap.objects.items())
        regions = ", ".join(snap.regions)
        return f"objects: {objs or 'none'}\nregions: {regions or 'none'}\ngripper_opening: {snap.gripper_opening:.2f}"

    return PlannerEngine(provider, registry, confirm=confirm, world_fn=world_fn, context_fn=context_fn)


async def _sim(args: argparse.Namespace) -> int:
    from embodied.cognition.offline import SIM_PLAN_RULES
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.skills.registry import SkillRegistry
    from embodied.skills.scripted.manip import register_sim_skills

    sim = TabletopSim(seed=int(args.seed))
    registry = SkillRegistry()
    register_sim_skills(registry, sim)

    code: int
    if args.eval > 0:
        code = await _sim_eval(sim, registry, n=int(args.eval), record=bool(args.record))
    else:
        provider, pname = _build_provider(args.provider, plan_rules=SIM_PLAN_RULES)
        engine = _make_sim_engine(sim, registry, provider, _make_confirm(bool(args.yes)))
        code = await _repl(
            engine, registry, f"embodied sim · provider={pname} · {sim.spec().embodiment_id} · skills={len(registry.catalog())}"
        )
    if args.snapshot:
        from PIL import Image

        Image.fromarray(sim.render("side")).save(args.snapshot)
        print(f"snapshot -> {args.snapshot}")
    return code


EVAL_COMMAND = "把红色方块放进盒子"


async def _sim_eval(sim: Any, registry: Any, *, n: int, record: bool) -> int:
    """Headless DoD harness: N randomized episodes driven end-to-end through the
    PlannerEngine (text command → plan → DAG → skills → verified report). Success is
    judged INDEPENDENTLY against sim ground truth, never by the agent's self-report."""
    from embodied.cognition.offline import SIM_PLAN_RULES, ScriptedPlanProvider
    from embodied.cognition.world_state import WorldSnapshot, object_in_region

    recorder = None
    if record:
        from embodied.data_engine import EpisodeRecorder

        recorder = EpisodeRecorder()

    wins = 0
    for i in range(n):
        obs = sim.reset(randomize=True)
        cube = tuple(round(v, 3) for v in obs.objects["obj_cube"].pos)
        # Fresh engine per episode: no cross-episode history leakage in eval
        engine = _make_sim_engine(sim, registry, ScriptedPlanProvider(SIM_PLAN_RULES), confirm=None)
        writer = None
        if recorder is not None:
            writer = recorder.start(
                task=EVAL_COMMAND, embodiment_id=sim.spec().embodiment_id, seed=i,
            )
            sim.set_hooks(
                on_step=writer.on_step,
                on_guard_event=lambda e, w=writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
            )
        try:
            turn = await engine.turn(EVAL_COMMAND)
            snap = WorldSnapshot.from_observation(sim.read())
            success = object_in_region(snap, "obj_cube", "bin_region", margin=0.005)
            if writer:
                for r in turn.results:
                    writer.on_event(
                        "skill_end",
                        {"step": r.step_id, "status": r.status.value, "detail": r.detail or r.error},
                    )
                writer.on_event("note", {"report": turn.text[:400]})
                writer.finish(bool(success), detail=turn.text[:200])
        except Exception as e:
            if writer:
                writer.abort(repr(e))
            raise
        finally:
            if recorder is not None:
                sim.set_hooks(None, None)
        wins += int(success)
        steps = " ".join(f"{r.step_id}={r.status.value}" for r in turn.results) or "no-steps"
        print(f"episode {i + 1:>2}/{n}: cube={cube} {steps} -> {'SUCCESS' if success else 'FAIL'}")
    rate = wins / n if n else 0.0
    print(f"\nresult: {wins}/{n} success ({rate:.0%})")
    return 0 if rate >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
