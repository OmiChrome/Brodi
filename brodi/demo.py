"""Predefined presentation tasks, offline rehearsal and measured live execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import load_environment
from .profiler import Profile, span

TASKS = {
    "launch": {
        "title": "Open an app instantly",
        "commands": ["Hey bro, open Notepad"],
        "tools": ["open_app"],
        "args": [{"app": "Notepad"}],
    },
    "workspace": {
        "title": "Prepare a browser workspace",
        "commands": ["Open Chrome and go to github.com"],
        "tools": ["open_app", "chrome_open"],
        "args": [{"app": "Google Chrome"}, {"site": "other", "query": "github.com"}],
    },
    "research": {
        "title": "Search with a predefined topic",
        "commands": ["Search YouTube for ambient piano"],
        "tools": ["chrome_open"],
        "args": [{"site": "youtube", "query": "ambient piano"}],
    },
    "followup": {
        "title": "Ask for missing information",
        "commands": ["Search YouTube", "lofi beats"],
        "tools": ["chrome_open"],
        "args": [{"site": "youtube", "query": "lofi beats"}],
    },
    "automation": {
        "title": "Run a named presentation task",
        "commands": ["Run presentation workspace"],
        "tools": ["open_app", "chrome_open"],
        "args": [{"app": "Google Chrome"}, {"site": "other", "query": "github.com"}],
    },
    "polite": {
        "title": "Understand a polite request locally",
        "commands": ["Could you open Notepad"],
        "tools": ["open_app"],
        "args": [{"app": "Notepad"}],
    },
    "calculator": {
        "title": "Open Calculator",
        "commands": ["Open Calculator"],
        "tools": ["open_app"],
        "args": [{"app": "Calculator"}],
    },
    "paint": {"title": "Open Paint", "commands": ["Open Paint"], "tools": ["open_app"], "args": [{"app": "Paint"}]},
    "downloads": {
        "title": "Open Downloads",
        "commands": ["Open my downloads"],
        "tools": ["finder_open_folder"],
        "args": [{"folder": "downloads"}],
    },
    "calendar": {
        "title": "Open Google Calendar",
        "commands": ["Open Google Calendar"],
        "tools": ["chrome_open"],
        "args": [{"site": "google_calendar", "query": ""}],
    },
    "github_search": {
        "title": "Search GitHub",
        "commands": ["Search GitHub for voice agents"],
        "tools": ["chrome_open"],
        "args": [{"site": "github", "query": "voice agents"}],
    },
    "time": {
        "title": "Read the time",
        "commands": ["What time is it"],
        "tools": ["system_info"],
        "args": [{"kind": "time"}],
    },
    "date": {
        "title": "Read today's date",
        "commands": ["What is today's date"],
        "tools": ["system_info"],
        "args": [{"kind": "date"}],
    },
    "battery": {
        "title": "Read battery level",
        "commands": ["Check battery"],
        "tools": ["system_info"],
        "args": [{"kind": "battery"}],
    },
    "calculate": {
        "title": "Calculate an answer",
        "commands": ["Calculate 23 times 7"],
        "tools": ["calculate"],
        "args": [{"expression": "23 * 7"}],
    },
}


async def run_tasks(names: list[str], *, live: bool = False, repeat: int = 1) -> tuple[dict, Profile]:
    from .agent import DynamicMacAgent
    from .applescript import MacContext
    from .automations import predefined_plan
    from .memory import MemoryStore
    from .multi_step import deterministic_plan
    from .registry import ToolRegistry

    profile = Profile("live-demo" if live else "rehearsal")
    rows = []
    with TemporaryDirectory(prefix="brodi-demo-") as directory:
        registry = ToolRegistry(learned_path=Path(directory) / "learned.json")
        agent = DynamicMacAgent(registry, enable_learning=False, memory=MemoryStore(Path(directory) / "memory.db"))
        # Rehearsal uses real parsing, routing, registry and policy with fixed context.
        if not live:
            agent.context._ctx = MacContext("Explorer", ["Explorer", "Google Chrome"], ["Notepad", "Google Chrome"])

        async def no_network(*args, **kwargs):
            raise RuntimeError("Presentation tasks must use deterministic routing, never a model")

        agent.router.client.evaluate = no_network
        try:
            with profile.capture():
                if live:
                    await agent.start()
                for iteration in range(repeat):
                    for name in names:
                        task = TASKS[name]
                        print(f"{task['title']} ({iteration + 1}/{repeat})", flush=True)
                        for index, command in enumerate(task["commands"]):
                            started = time.perf_counter()
                            if live:
                                outcome = await agent.handle(command)
                                ok = outcome.executed or (
                                    name == "followup" and index == 0 and agent.pending_question is not None
                                )
                                steps = outcome.steps
                                memory_hit = outcome.memory_hit
                            else:
                                # Resolve the scripted answer as the complete search for validation.
                                text = "Search YouTube for lofi beats" if name == "followup" and index else command
                                ctx = await agent.context.latest()
                                with span("rehearsal.validate"):
                                    plan = predefined_plan(text) or deterministic_plan(
                                        text, ctx.running_apps + ctx.installed_apps
                                    )
                                    route = await agent.router.route(text, ctx) if plan is None else None
                                    steps = (
                                        [s.model_dump() for s in plan.steps]
                                        if plan
                                        else [{"tool": route.tool.name, "args": route.args}]
                                        if route.tool
                                        else []
                                    )
                                    from . import policy

                                    ok = bool(steps) and all(
                                        (tool := registry.get(s["tool"])) is not None
                                        and tool.is_available(ctx)
                                        and not tool.risky
                                        and not policy.check(tool.render(s["args"]), tool.active_scope)
                                        for s in steps
                                    )
                                    if route and route.kind == "clarify":
                                        ok = bool(route.missing_arg)
                                        steps = []
                                memory_hit = False
                            if steps:
                                ok = (
                                    ok
                                    and [s["tool"] for s in steps] == task["tools"]
                                    and [s["args"] for s in steps] == task["args"]
                                )
                            row = {
                                "task": name,
                                "iteration": iteration + 1,
                                "command_index": index,
                                "ok": ok,
                                "dispatch_ms": round((time.perf_counter() - started) * 1000, 3),
                                "memory_hit": memory_hit,
                                "tools": [s["tool"] for s in steps],
                            }
                            rows.append(row)
                            print(f"  {'PASS' if ok else 'FAIL'} {command}: {row['dispatch_ms']:.1f} ms", flush=True)
                            if not ok:
                                return {"mode": "live" if live else "rehearsal", "ok": False, "rows": rows}, profile
        finally:
            await agent.aclose()
    return {
        "mode": "live" if live else "rehearsal",
        "ok": all(r["ok"] for r in rows),
        "rows": rows,
        "measurement": "Post-transcription dispatch, excluding first paint, navigation and TTS. Live opens apps/tabs; rehearsal validates without launching.",
    }, profile


def save_report(output: Path, result: dict, profile: Profile) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps({**result, "profile": profile.report()}, indent=2), encoding="utf-8")
    (output / "trace.json").write_text(json.dumps(profile.trace()), encoding="utf-8")


def main() -> None:
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", choices=["all", *TASKS], default="all")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--catalog", action="store_true", help="show ready-made Windows demo phrases")
    parser.add_argument(
        "--max-dispatch-ms", type=float, help="fail if any measured command exceeds this dispatch budget"
    )
    parser.add_argument("--run", action="store_true", help="actually launch apps/tabs; default is offline rehearsal")
    parser.add_argument("--repeat", type=int, default=1, help="repeat tasks to demonstrate local shortcut memory")
    parser.add_argument("--output", type=Path, default=Path(".freebuff/profiles/demo"))
    ns = parser.parse_args()
    if ns.catalog:
        from .demo_catalog import QUICK_DEMOS

        print(json.dumps(QUICK_DEMOS, indent=2))
        return
    if ns.max_dispatch_ms is not None and ns.max_dispatch_ms <= 0:
        parser.error("--max-dispatch-ms must be positive")
    if ns.repeat < 1 or ns.repeat > 100:
        parser.error("--repeat must be between 1 and 100")
    if ns.list:
        print(json.dumps(TASKS, indent=2))
        return
    result, profile = asyncio.run(
        run_tasks(list(TASKS) if ns.task == "all" else [ns.task], live=ns.run, repeat=ns.repeat)
    )
    if ns.max_dispatch_ms is not None:
        result["budget_ms"] = ns.max_dispatch_ms
        result["budget_passed"] = all(row["dispatch_ms"] <= ns.max_dispatch_ms for row in result["rows"])
        result["ok"] = result["ok"] and result["budget_passed"]
    save_report(ns.output, result, profile)
    print(f"Report: {ns.output / 'report.json'}")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
