"""Backward-compatible facade over :mod:`brodi.os_backend`.

The public names (``run_applescript``, ``MacContext``, ``ContextPoller``, ...) are kept so
existing callers and tests work unchanged; execution and context now dispatch to AppleScript
on macOS or PowerShell on Windows 11.
"""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import dataclass

from .os_backend import (
    SCRIPT_TIMEOUT_S,
    ScriptResult,
    escape_applescript_string,
    run_os_script,
)
from .os_backend import (
    get_active_app as _get_active_app,
)
from .os_backend import (
    get_installed_apps as _get_installed_apps,
)
from .os_backend import (
    get_running_apps as _get_running_apps,
)
from .profiler import profiled

OSASCRIPT_TIMEOUT_S = SCRIPT_TIMEOUT_S

__all__ = [
    "ContextPoller",
    "MacContext",
    "OSASCRIPT_TIMEOUT_S",
    "ScriptResult",
    "escape_applescript_string",
    "get_active_app",
    "get_installed_apps",
    "get_running_apps",
    "run_applescript",
    "snapshot_context",
]


async def run_applescript(script: str, timeout: float = OSASCRIPT_TIMEOUT_S) -> ScriptResult:
    """Run a script through the active platform backend."""
    return await run_os_script(script, timeout)


async def get_active_app() -> str:
    return await asyncio.to_thread(_get_active_app)


async def get_running_apps() -> list[str]:
    return await asyncio.to_thread(_get_running_apps)


def get_installed_apps(limit: int = 200) -> list[str]:
    return _get_installed_apps(limit)


@dataclass(frozen=True)
class MacContext:
    active_app: str
    running_apps: list[str]
    installed_apps: list[str]


async def snapshot_context() -> MacContext:
    active, running = await asyncio.gather(get_active_app(), get_running_apps())
    return MacContext(active_app=active, running_apps=running, installed_apps=get_installed_apps())


class ContextPoller:
    """Keeps a fresh desktop context in the background so a voice turn never waits on the OS.

    Frontmost/running apps refresh every ``interval`` seconds; the installed-app scan
    (filesystem) refreshes every ``installed_interval`` seconds.
    """

    def __init__(self, interval: float = 1.0, installed_interval: float = 60.0):
        self.interval = interval
        self.installed_interval = installed_interval
        self._ctx: MacContext | None = None
        self._task: asyncio.Task | None = None
        self._installed: list[str] = []
        self._installed_at = 0.0

    async def start(self) -> MacContext:
        if self._task is None:
            self._ctx = await self._refresh()
            # A long-lived poller must not retain a command's trace/context.
            self._task = asyncio.create_task(self._loop(), name="brodi-context-poller", context=Context())
        assert self._ctx is not None
        return self._ctx

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @profiled("context")
    async def latest(self) -> MacContext:
        """Cached snapshot; starts the poller on first use."""
        if self._ctx is None:
            return await self.start()
        return self._ctx

    @profiled("context.refresh")
    async def _refresh(self) -> MacContext:
        import time

        now = time.monotonic()
        if now - self._installed_at > self.installed_interval:
            self._installed = await asyncio.to_thread(get_installed_apps)
            self._installed_at = now
        active, running = await asyncio.gather(get_active_app(), get_running_apps())
        return MacContext(active_app=active, running_apps=running, installed_apps=self._installed)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                self._ctx = await self._refresh()
            except Exception:  # keep polling on transient OS failures
                pass
