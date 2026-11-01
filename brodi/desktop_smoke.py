"""Opt-in live Windows smoke: real state checks on a separate, empty Notepad document."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory


async def run(*, repeat: int = 3, budget_ms: float = 500) -> dict:
    from .agent import DynamicMacAgent
    from .memory import MemoryStore
    from .os_backend import is_windows
    from .registry import ToolRegistry
    from .windows_desktop import user32, windows

    if not is_windows():
        raise RuntimeError("The native desktop smoke test requires Windows")
    api = user32()
    for name in ("IsWindow", "IsZoomed"):
        fn = getattr(api, name)
        fn.argtypes, fn.restype = [wintypes.HWND], wintypes.BOOL

    async def wait_for(predicate, message):
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.03)
        return message

    path = Path(".freebuff") / f"brodi-window-smoke-{uuid.uuid4().hex[:10]}.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("", encoding="utf-8")
    before = {w.handle for w in windows("Notepad")}
    subprocess.Popen(["notepad.exe", str(path.resolve())])
    matches = []

    def opened():
        nonlocal matches
        matches = [w for w in windows("Notepad") if path.stem in w.title and w.handle not in before]
        return len(matches) == 1

    await wait_for(opened, "The separate test document opened")
    window = matches[0]
    rows = []
    with TemporaryDirectory(prefix="brodi-desktop-demo-") as directory:
        agent = DynamicMacAgent(
            ToolRegistry(learned_path=Path(directory) / "learned.json"),
            enable_learning=False,
            memory=MemoryStore(Path(directory) / "memory.db"),
        )

        async def no_network(*args, **kwargs):
            raise RuntimeError("The ready-made desktop smoke must not call a routing model")

        agent.router.client.evaluate = no_network
        try:
            await agent.start()
            for iteration in range(repeat):
                for action in ("maximize", "restore", "minimize", "restore", "focus"):
                    # Establish a unique foreground target, without modifying the tool's selection logic.
                    api.ShowWindowAsync(window.handle, 9)
                    await wait_for(lambda: not api.IsIconic(window.handle), "restored for the next check")
                    if not api.SetForegroundWindow(window.handle) and api.GetForegroundWindow() != window.handle:
                        raise RuntimeError("Windows denied test focus; bring the empty test document to the front")
                    started = time.perf_counter()
                    outcome = await agent.handle(f"{action} Notepad")
                    elapsed = (time.perf_counter() - started) * 1000
                    if not outcome.executed:
                        raise RuntimeError(outcome.speak)
                    predicate = {
                        "maximize": lambda: api.IsZoomed(window.handle),
                        "minimize": lambda: api.IsIconic(window.handle),
                        "restore": lambda: not api.IsIconic(window.handle) and not api.IsZoomed(window.handle),
                        "focus": lambda: api.GetForegroundWindow() == window.handle,
                    }[action]
                    await wait_for(predicate, action + " verified")
                    rows.append(
                        {
                            "command": f"{action} Notepad",
                            "iteration": iteration + 1,
                            "dispatch_ms": round(elapsed, 3),
                            "verified": True,
                        }
                    )
            api.SetForegroundWindow(window.handle)
            if api.GetForegroundWindow() != window.handle:
                raise RuntimeError("The empty test window lost focus before closing; nothing closed")
            started = time.perf_counter()
            outcome = await agent.handle("close Notepad")
            elapsed = (time.perf_counter() - started) * 1000
            if not outcome.executed:
                raise RuntimeError(outcome.speak)
            await wait_for(lambda: not api.IsWindow(window.handle), "close verified")
            rows.append({"command": "close Notepad", "dispatch_ms": round(elapsed, 3), "verified": True})
        finally:
            await agent.aclose()
    return {
        "ok": all(row["dispatch_ms"] <= budget_ms for row in rows),
        "budget_ms": budget_ms,
        "rows": rows,
        "model_calls": 0,
        "test_document": str(path),
        "measurement": "Post-transcription dispatch. Real window states checked separately, outside the dispatch timer.",
    }
