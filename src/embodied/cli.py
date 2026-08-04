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


def _build_provider(kind: str, offline_rules: Any = None) -> tuple[Any, str]:
    if kind == "offline" or (kind == "auto" and not os.getenv("LLM_API_KEY")):
        from embodied.cognition.offline import ScriptedToolProvider

        return ScriptedToolProvider(offline_rules), "offline-scripted"
    from embodied.providers import build_provider

    p = build_provider()
    return p, type(p).__name__


async def _repl(planner: Any, registry: Any, banner: str) -> int:
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
        turn = await planner.turn(line)
        for rec in turn.calls:
            if rec.result is None:
                status = f"skipped ({rec.note})"
            else:
                status = "ok" if rec.result.ok else f"failed: {rec.result.detail}"
            print(f"  [skill] {rec.skill} → {status}")
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
    from embodied.cognition.planner import PlannerConfig, TextPlanner
    from embodied.skills.builtin.mock import register_builtin
    from embodied.skills.registry import SkillRegistry

    registry = SkillRegistry()
    register_builtin(registry)
    provider, pname = _build_provider(kind)
    planner = TextPlanner(provider, registry, PlannerConfig(), confirm=_make_confirm(auto_yes))
    return await _repl(planner, registry, f"embodied agent-core · provider={pname} · skills={len(registry.catalog())}")


async def _sim(args: argparse.Namespace) -> int:
    from embodied.cognition.offline import SIM_RULES
    from embodied.cognition.planner import PlannerConfig, TextPlanner
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
        provider, pname = _build_provider(args.provider, offline_rules=SIM_RULES)
        planner = TextPlanner(provider, registry, PlannerConfig(), confirm=_make_confirm(bool(args.yes)))
        code = await _repl(
            planner, registry, f"embodied sim · provider={pname} · {sim.spec().embodiment_id} · skills={len(registry.catalog())}"
        )
    if args.snapshot:
        from PIL import Image

        Image.fromarray(sim.render("side")).save(args.snapshot)
        print(f"snapshot -> {args.snapshot}")
    return code


async def _sim_eval(sim: Any, registry: Any, *, n: int, record: bool) -> int:
    """Headless DoD harness: N randomized pick&place episodes, success rate report."""
    recorder = None
    if record:
        from embodied.data_engine import EpisodeRecorder

        recorder = EpisodeRecorder()

    wins = 0
    for i in range(n):
        obs = sim.reset(randomize=True)
        cube = tuple(round(v, 3) for v in obs.objects["obj_cube"].pos)
        writer = None
        if recorder is not None:
            writer = recorder.start(
                task="put the red cube into the bin",
                embodiment_id=sim.spec().embodiment_id,
                seed=i,
            )
            sim.set_hooks(
                on_step=writer.on_step,
                on_guard_event=lambda e, w=writer: w.on_event("safety", {"kind": e.kind, "reason": e.reason}),
            )
        try:
            if writer:
                writer.on_event("skill_start", {"skill": "skill.manip.pick"})
            r1 = await registry.invoke("skill.manip.pick", {})
            if writer:
                writer.on_event("skill_end", {"skill": "skill.manip.pick", "ok": r1.ok, "detail": r1.detail})
            if r1.ok:
                if writer:
                    writer.on_event("skill_start", {"skill": "skill.manip.place"})
                r2 = await registry.invoke("skill.manip.place", {})
                if writer:
                    writer.on_event("skill_end", {"skill": "skill.manip.place", "ok": r2.ok, "detail": r2.detail})
            else:
                from embodied.skills.registry import SkillResult

                r2 = SkillResult(ok=False, detail="skipped: pick failed")
            success = r1.ok and r2.ok
            if writer:
                writer.finish(success, detail=f"pick={r1.detail} | place={r2.detail}")
        except Exception as e:
            if writer:
                writer.abort(repr(e))
            raise
        finally:
            if recorder is not None:
                sim.set_hooks(None, None)
        wins += int(success)
        print(f"episode {i + 1:>2}/{n}: cube={cube} pick={'ok' if r1.ok else r1.detail} "
              f"place={'ok' if r2.ok else r2.detail} -> {'SUCCESS' if success else 'FAIL'}")
    rate = wins / n if n else 0.0
    print(f"\nresult: {wins}/{n} success ({rate:.0%})")
    return 0 if rate >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
