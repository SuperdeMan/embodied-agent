"""agent-core text mode (M0 DoD): `embodied chat` — REPL wired to planner + skill registry.

Voice, console and the realtime/safety processes attach in M1; this entry point stays
the minimal way to talk to the cognition core without any hardware or keys.
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
    chat = sub.add_parser("chat", help="text-mode conversation with the agent core")
    chat.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "offline", "llm"],
        help="auto: use the configured LLM when LLM_API_KEY is set, otherwise the offline scripted provider",
    )
    chat.add_argument("--yes", action="store_true", help="auto-confirm dangerous skills (demos only)")
    args = parser.parse_args(argv)
    if args.cmd in (None, "chat"):
        provider = getattr(args, "provider", "auto")
        auto_yes = bool(getattr(args, "yes", False))
        return asyncio.run(_chat(provider, auto_yes))
    parser.error(f"unknown command {args.cmd!r}")
    return 2


def _build_provider(kind: str) -> tuple[Any, str]:
    if kind == "offline" or (kind == "auto" and not os.getenv("LLM_API_KEY")):
        from embodied.cognition.offline import ScriptedToolProvider

        return ScriptedToolProvider(), "offline-scripted"
    from embodied.providers import build_provider

    p = build_provider()
    return p, type(p).__name__


async def _chat(kind: str, auto_yes: bool) -> int:
    from embodied.cognition.planner import PlannerConfig, TextPlanner
    from embodied.skills.builtin.mock import register_builtin
    from embodied.skills.registry import SkillRegistry

    registry = SkillRegistry()
    register_builtin(registry)
    provider, pname = _build_provider(kind)

    async def confirm(skill: str, params: dict[str, Any]) -> bool:
        if auto_yes:
            print(f"[confirm] {skill} auto-approved (--yes)")
            return True
        ans = await asyncio.to_thread(input, f"[confirm] 执行危险技能 {skill}? (y/N) ")
        return ans.strip().lower() in ("y", "yes")

    planner = TextPlanner(provider, registry, PlannerConfig(), confirm=confirm)
    print(f"embodied agent-core · provider={pname} · skills={len(registry.catalog())}")
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


if __name__ == "__main__":
    raise SystemExit(main())
