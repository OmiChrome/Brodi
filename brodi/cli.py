"""Text REPL for exercising the router/executor without audio.

uv run python -m brodi.cli                 # interactive
uv run python -m brodi.cli "mute the mac"  # one-shot
uv run python -m brodi.cli --dry "open github dot com"   # route only, don't execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from .config import load_environment

if TYPE_CHECKING:
    from .agent import DynamicMacAgent


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="brodi")
    ap.add_argument("utterance", nargs="*")
    ap.add_argument("--dry", action="store_true", help="route only; do not execute or learn")
    ap.add_argument("--no-learn", action="store_true", help="disable the LLM tool-generation fallback")
    ap.add_argument("--policy", action="store_true", help="list every tool with its policy status and exit")
    ap.add_argument("--doctor", action="store_true", help="check setup and browser access without audio or model calls")
    ap.add_argument("--memory", choices=["list", "clear", "forget"], help="inspect or explicitly clear local memory")
    ap.add_argument("--memory-key", help="fact key or shortcut command to forget")
    ap.add_argument("--yes", action="store_true", help="confirm the requested memory deletion")
    ap.add_argument("-v", "--verbose", action="store_true")
    ns = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    load_environment()

    if ns.memory:
        import json

        from .memory import MemoryStore

        store = MemoryStore()
        if ns.memory == "list":
            print(json.dumps(store.list(), ensure_ascii=False, indent=2))
            return 0
        if ns.memory == "forget" and not ns.memory_key:
            ap.error("--memory forget requires --memory-key")
        confirmed = ns.yes or (sys.stdin.isatty() and input("Delete saved memory? Type yes: ").strip().lower() == "yes")
        if not confirmed:
            print("Nothing deleted. Pass --yes or confirm interactively.")
            return 2
        print(f"Forgot {store.delete(ns.memory_key if ns.memory == 'forget' else None, confirmed=True)} entries.")
        return 0

    if ns.doctor:
        from .diagnostics import report

        checks = await asyncio.to_thread(report)
        for name, ok, detail in checks:
            print(f"{'OK' if ok else 'NEEDS SETUP'} {name}: {detail}")
        return 0 if all(ok for _, ok, _ in checks) else 1

    from .agent import DynamicMacAgent

    if ns.policy:
        from . import policy as _policy
        from .registry import ToolRegistry

        print(f"policy: {'strict' if _policy.ENABLED else 'OFF (BRODI_POLICY=off)'}")
        for t in sorted(ToolRegistry().tools.values(), key=lambda t: (not t.blocked, t.source, t.name)):
            print(
                f"  {'BLOCKED ' if t.blocked else 'allowed '} {t.source:7s} {t.name:28s} {'' if not t.blocked else '; '.join(t.blocked_by)}"
            )
        return 0
    agent = DynamicMacAgent(enable_learning=not (ns.no_learn or ns.dry))
    await agent.start()
    try:
        if ns.utterance:
            await _one(agent, " ".join(ns.utterance), ns.dry)
            return 0
        ctx = await agent.context.latest()
        print(
            f"brodi ready. frontmost={ctx.active_app} running={len(ctx.running_apps)} tools={len(agent.registry.tools)}"
        )
        print("type a command (ctrl-d to quit)\n")
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                print()
                break
            if line:
                stop = await _one(agent, line, ns.dry)
                if stop:
                    break
        return 0
    finally:
        await agent.aclose()


async def _one(agent: DynamicMacAgent, text: str, dry: bool) -> bool:
    """Returns True when the user asked the assistant to stop."""
    if dry:
        ctx = await agent.context.latest()
        route = await agent.router.route(text, ctx)
        print(f"  -> {route.kind}: {route.summary} conf={route.confidence:.2f} {route.latency_ms:.0f}ms")
        print(f"     top: {dict(sorted(route.probabilities.items(), key=lambda kv: -kv[1])[:4])}")
        return False
    out = await agent.handle(text)
    r = out.route
    tag = "llm" if out.handoff_to_llm else (r.summary if r else "?")
    t = " ".join(f"{k}={v:.0f}" for k, v in out.timings.items())
    print(f"  -> {tag} [{t}] state={agent.state.value}")
    if out.learned:
        print(f"     learned: {out.learned.name} scope={out.learned.scope} args={[a.name for a in out.learned.args]}")
    if out.speak:
        print(f"  agent> {out.speak}")
    return out.stop


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
