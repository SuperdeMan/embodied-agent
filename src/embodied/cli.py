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
    sim.add_argument("--eval", type=int, default=0, metavar="N", help="quick eval: N episodes on --seed (ad-hoc, not versioned)")
    sim.add_argument("--task", default="", metavar="YAML", help="versioned eval task file (eval/tasks/*.yaml); overrides --eval")
    sim.add_argument("--seed", type=int, default=0)
    sim.add_argument("--record", action="store_true", help="record episodes to outputs/episodes (EPISODES_DIR)")
    sim.add_argument("--snapshot", default="", help="save a scene PNG to this path on exit")
    sim.add_argument("--remote", default="", metavar="ADDR", help="drive a serve-control process instead of a local sim")
    sim.add_argument("--guardian", default="", metavar="ADDR", help="stream agent heartbeats to this guardian (with --remote)")
    sim.add_argument("--pick-policy", default="", metavar="ONNX",
                     help="serve skill.manip.pick from this learned policy (same manifest, needs --group policy)")
    sim.add_argument("--place-policy", default="", metavar="ONNX",
                     help="serve skill.manip.place from this learned policy")

    collect = sub.add_parser("collect", help="scripted-expert data collection: record randomized pick&place episodes")
    collect.add_argument("--episodes", type=int, default=50)
    collect.add_argument("--seed", type=int, default=0)
    collect.add_argument("--object", default="obj_cube")
    collect.add_argument("--region", default="bin_region")
    collect.add_argument("--command", default="", help="task label for the dataset (defaults to the eval command)")
    collect.add_argument("--out", default="", help="episodes root (default outputs/episodes, or EPISODES_DIR)")

    tele = sub.add_parser("teleop", help="keyboard teleoperation of the tabletop sim (records episodes)")
    tele.add_argument("--seed", type=int, default=0)
    tele.add_argument("--step", type=float, default=0.01, metavar="M", help="cartesian jog step in meters")
    tele.add_argument("--command", default="", help="task label for recorded episodes (defaults to the eval command)")
    tele.add_argument("--out", default="", help="episodes root (default outputs/episodes, or EPISODES_DIR)")
    tele.add_argument("--no-record", action="store_true")

    conv = sub.add_parser("convert", help="convert recorded episodes into a LeRobotDataset (needs --group learn)")
    conv.add_argument("--root", default="", help="episodes root (default outputs/episodes, or EPISODES_DIR)")
    conv.add_argument("--out", required=True, help="dataset output directory (must not exist yet)")
    conv.add_argument("--fps", type=int, default=50)
    conv.add_argument("--segment", choices=["episode", "skill"], default="episode",
                      help="one dataset episode per recording, or split at skill boundaries")
    conv.add_argument("--skills", nargs="*", default=[],
                      help="skill mode: keep only these skills' segments (per-skill policy datasets)")
    conv.add_argument("--include-failures", action="store_true", help="also convert success=false episodes")
    conv.add_argument("--repo-id", default="", help="dataset repo id (default local/<out dirname>)")

    console = sub.add_parser("console", help="web console: voice/text control of the tabletop sim")
    _add_common(console)
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=8390)
    console.add_argument("--seed", type=int, default=0)

    sc = sub.add_parser("serve-control", help="realtime-control process (gRPC ControlService)")
    sc.add_argument("--port", type=int, default=8391)
    sc.add_argument("--seed", type=int, default=0)
    sc.add_argument("--require-supervisor", action="store_true",
                    help="halt unless the safety guardian holds SupervisorLink (production topology)")
    sc.add_argument("--mock", action="store_true", help="serve mock skills instead of the sim (no mujoco)")

    sg = sub.add_parser("serve-guardian", help="safety-guardian process (gRPC SafetyService)")
    sg.add_argument("--port", type=int, default=8392)
    sg.add_argument("--control", default="127.0.0.1:8391", metavar="ADDR")

    args = parser.parse_args(argv)
    if args.cmd in (None, "chat"):
        return asyncio.run(_chat(getattr(args, "provider", "auto"), bool(getattr(args, "yes", False))))
    if args.cmd == "sim":
        return asyncio.run(_sim(args))
    if args.cmd == "collect":
        return asyncio.run(_collect(args))
    if args.cmd == "convert":
        return _convert(args)
    if args.cmd == "teleop":
        return _teleop(args)
    if args.cmd == "console":
        return _console(args)
    if args.cmd == "serve-control":
        return asyncio.run(_serve_control(args))
    if args.cmd == "serve-guardian":
        return asyncio.run(_serve_guardian(args))
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


async def _serve_control(args: argparse.Namespace) -> int:
    import logging

    from embodied.control.service import ControlState, serve, watchdog_loop
    from embodied.skills.registry import SkillRegistry

    logging.basicConfig(level=logging.INFO)
    registry = SkillRegistry()
    sim = None
    if args.mock:
        from embodied.skills.builtin.mock import register_builtin

        register_builtin(registry)
    else:
        from embodied.control.drivers.mujoco_sim import TabletopSim
        from embodied.skills.scripted.manip import register_sim_skills

        sim = TabletopSim(seed=int(args.seed))
        register_sim_skills(registry, sim)
    state = ControlState(registry, sim, require_supervisor=bool(args.require_supervisor))
    server, bound = await serve(state, port=int(args.port))
    print(f"realtime-control on 127.0.0.1:{bound} (skills={len(registry.catalog())}, "
          f"require_supervisor={state.require_supervisor})")
    wd = asyncio.create_task(watchdog_loop(state))
    try:
        await server.wait_for_termination()
    finally:
        wd.cancel()
    return 0


async def _serve_guardian(args: argparse.Namespace) -> int:
    import logging

    from embodied.safety.guard import GuardLimits
    from embodied.safety.guardian import GuardianState, agent_watchdog_loop, serve, supervisor_link_loop

    logging.basicConfig(level=logging.INFO)
    state = GuardianState(str(args.control), GuardLimits(joint_lower=(), joint_upper=()))
    server, bound = await serve(state, port=int(args.port))
    print(f"safety-guardian on 127.0.0.1:{bound}, supervising control at {args.control}")
    tasks = [asyncio.create_task(agent_watchdog_loop(state)), asyncio.create_task(supervisor_link_loop(state))]
    try:
        await server.wait_for_termination()
    finally:
        for t in tasks:
            t.cancel()
    return 0


async def _sim_remote(args: argparse.Namespace) -> int:
    """agent-core against a serve-control process. World-state context/verification are
    local-sim features; remotely the verifier reads UNKNOWN (never convicts) until a
    world streaming channel exists (noted in roadmap)."""
    import grpc

    from embodied.cognition.engine import PlannerEngine
    from embodied.cognition.offline import SIM_PLAN_RULES
    from embodied.runtime.liveness import agent_heartbeat_loop
    from embodied.skills.remote import RemoteSkillRegistry

    provider, pname = _build_provider(args.provider, plan_rules=SIM_PLAN_RULES)
    channel = grpc.aio.insecure_channel(str(args.remote))
    registry = await RemoteSkillRegistry(channel).connect()
    engine = PlannerEngine(provider, registry, confirm=_make_confirm(bool(args.yes)))
    beat = None
    if args.guardian:
        beat = asyncio.create_task(agent_heartbeat_loop(str(args.guardian)))
        print(f"heartbeats -> guardian {args.guardian}")
    try:
        return await _repl(
            engine, registry,
            f"embodied agent-core(remote) · provider={pname} · control={args.remote} · skills={len(registry.catalog())}",
        )
    finally:
        if beat is not None:
            beat.cancel()
        await channel.close()


def _register_skills(registry: Any, sim: Any, *, pick_policy: str = "", place_policy: str = "") -> None:
    """Scripted skills, with learned implementations claiming their manifests when given
    (architecture §4.3: same manifest, implementation swapped)."""
    from embodied.skills.scripted.manip import register_sim_skills

    pick = place = None
    skip: list[str] = []
    if pick_policy or place_policy:
        from embodied.providers.policy import OnnxPolicy
        from embodied.skills.policies import register_policy_sim_skills

        if pick_policy:
            pick = OnnxPolicy(pick_policy)
            skip.append("skill.manip.pick")
        if place_policy:
            place = OnnxPolicy(place_policy)
            skip.append("skill.manip.place")
    register_sim_skills(registry, sim, skip=tuple(skip))
    if pick or place:
        register_policy_sim_skills(registry, sim, pick=pick, place=place)


async def _sim(args: argparse.Namespace) -> int:
    from embodied.cognition.offline import SIM_PLAN_RULES
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.skills.registry import SkillRegistry

    if args.remote:
        return await _sim_remote(args)

    sim = TabletopSim(seed=int(args.seed))
    registry = SkillRegistry()
    _register_skills(registry, sim, pick_policy=str(args.pick_policy), place_policy=str(args.place_policy))

    code: int
    if args.task:
        code = await _sim_eval_task(args)
    elif args.eval > 0:
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


async def _collect(args: argparse.Namespace) -> int:
    """Scripted-expert rollouts (no planner) with recording on: the M2 data source."""
    from embodied.cognition.world_state import object_in_region
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.data_engine.collect import collect_episodes
    from embodied.skills.registry import SkillRegistry
    from embodied.skills.scripted.manip import register_sim_skills

    sim = TabletopSim(seed=int(args.seed))
    registry = SkillRegistry()
    register_sim_skills(registry, sim)
    obj, region = str(args.object), str(args.region)
    report = await collect_episodes(
        sim,
        registry,
        episodes=int(args.episodes),
        command=str(args.command) or EVAL_COMMAND,
        skill_sequence=(("skill.manip.pick", {"object": obj}), ("skill.manip.place", {"region": region})),
        judge=lambda snap: object_in_region(snap, obj, region, margin=0.005),
        root=(str(args.out) or None),
        seed=int(args.seed),
    )
    print(f"\ncollected {len(report.episodes)} episodes, {report.successes} success ({report.rate:.0%})")
    print("failures are kept and labeled (success=false in meta.json); the converter filters by default")
    return 0


TELEOP_KEYS = """teleop keys (letters only — no arrow keys, no numpad):
  w/s forward/back (+x/-x) · a/d left/right (-y/+y) · r/f up/down (+z/-z)
  space  toggle gripper open/close      0  home pose
  enter  finish episode as SUCCESS and start the next
  x      finish episode as FAIL and start the next
  esc    quit (a half-done episode is kept, labeled failed)"""


def _teleop(args: argparse.Namespace) -> int:
    """Keyboard teleop: WASD-style jogging, human-judged episode marking."""
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.control.teleop import TeleopSession

    try:
        import msvcrt

        def read_key() -> str:
            return msvcrt.getwch()
    except ImportError:  # POSIX fallback
        import termios
        import tty

        def read_key() -> str:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                return sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    sim = TabletopSim(seed=int(args.seed))
    session = TeleopSession(
        sim,
        task=str(args.command) or EVAL_COMMAND,
        root=(str(args.out) or None),
        record=not bool(args.no_record),
    )
    step = float(args.step)
    jogs = {"w": (step, 0, 0), "s": (-step, 0, 0), "a": (0, -step, 0), "d": (0, step, 0),
            "r": (0, 0, step), "f": (0, 0, -step)}
    print(f"embodied teleop · {sim.spec().embodiment_id} · step={step * 1000:.0f}mm · "
          f"record={'on' if session.recorder else 'off'}")
    print(TELEOP_KEYS)
    session.start_episode(randomize=True)
    print(session.status(), end="\r", flush=True)
    try:
        while True:
            ch = read_key()
            note = ""
            if ch == "\x1b":  # ESC
                break
            if ch in jogs:
                result = session.jog(*jogs[ch])
                note = "" if result.ok else f" !{result.reason}"
            elif ch == " ":
                session.toggle_gripper()
            elif ch == "0":
                session.home()
            elif ch in ("\r", "\n"):
                path = session.finish_episode(True)
                print(f"\n[teleop] episode saved SUCCESS -> {path}")
                session.start_episode(randomize=True)
            elif ch in ("x", "X"):
                path = session.finish_episode(False)
                print(f"\n[teleop] episode saved FAIL -> {path}")
                session.start_episode(randomize=True)
            print(f"{session.status()}{note}   ", end="\r", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    print(f"\n[teleop] session over: {session.episodes} episode(s)")
    return 0


def _convert(args: argparse.Namespace) -> int:
    from embodied.data_engine.lerobot_convert import convert_episodes

    root = str(args.root) or os.environ.get("EPISODES_DIR") or "outputs/episodes"
    convert_episodes(
        root,
        str(args.out),
        fps=int(args.fps),
        segment=str(args.segment),  # type: ignore[arg-type]
        skills=tuple(args.skills) or None,
        include_failures=bool(args.include_failures),
        repo_id=str(args.repo_id) or None,
    )
    return 0


def _console(args: argparse.Namespace) -> int:
    from embodied.cognition.offline import SIM_PLAN_RULES
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.hri.server import AppContext, run_console
    from embodied.providers.audio import (
        MockStreamingTTSProvider,
        build_asr_provider,
        build_tts_stream_provider,
    )
    from embodied.skills.registry import SkillRegistry
    from embodied.skills.scripted.manip import register_sim_skills

    sim = TabletopSim(seed=int(args.seed))
    registry = SkillRegistry()
    register_sim_skills(registry, sim)

    def make_engine(confirm):
        provider, _ = _build_provider(args.provider, plan_rules=SIM_PLAN_RULES)
        return _make_sim_engine(sim, registry, provider, confirm)

    asr = build_asr_provider()
    # Factory returns None when streaming TTS is unconfigured (origin semantics: off);
    # the console always speaks, so fall back to the silent mock.
    tts = build_tts_stream_provider() or MockStreamingTTSProvider()
    ctx = AppContext(sim, registry, make_engine, asr, tts)
    run_console(ctx, host=str(args.host), port=int(args.port))
    return 0


async def _sim_eval_task(args: argparse.Namespace) -> int:
    """Versioned eval: fresh sim per seed, fresh engine per episode, result JSON persisted.
    --pick-policy/--place-policy swap in learned implementations — same task, same judge,
    so scripted vs learned numbers are directly comparable (golden-gate discipline)."""
    from embodied.cognition.offline import SIM_PLAN_RULES, ScriptedPlanProvider
    from embodied.control.drivers.mujoco_sim import TabletopSim
    from embodied.data_engine.evaluation import EvalTask, run_task
    from embodied.skills.registry import SkillRegistry

    task = EvalTask.load(str(args.task))

    def make_sim(seed: int):
        sim = TabletopSim(seed=seed)
        registry = SkillRegistry()
        _register_skills(registry, sim, pick_policy=str(args.pick_policy), place_policy=str(args.place_policy))
        sim._eval_registry = registry  # paired at creation; engine factory reads it back
        return sim

    def make_engine(sim):
        return _make_sim_engine(sim, sim._eval_registry, ScriptedPlanProvider(SIM_PLAN_RULES), confirm=None)

    report = await run_task(task, make_sim=make_sim, make_engine=make_engine, record=bool(args.record))
    print(f"\n[{report.task_id}] result: {report.wins}/{len(report.episodes)} "
          f"({report.rate:.0%}) -> {'PASS' if report.passed else 'FAIL'}")
    print(f"result file: {report.out_path}")
    return 0 if report.passed else 1


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
