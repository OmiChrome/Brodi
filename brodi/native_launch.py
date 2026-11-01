"""Fast Windows launch for a small, trusted subset of seed tools. No shell or generated code."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from . import chrome, commands
from .os_backend import ScriptResult
from .profiler import profiled


def launch(tool: str, args: dict[str, str]) -> ScriptResult | None:
    command = None
    label = ""
    if tool == "chrome_open":
        label = commands.browser_url(args.get("site", "other"), args.get("query", ""))
        command = chrome.launch_chrome_command(label, chrome.profile_dir(), platform="win32")
    elif tool == "open_app":
        label = args.get("app", "")
        if label == "Google Chrome":
            command = chrome.launch_chrome_command(chrome.HOME_URL, chrome.profile_dir(), platform="win32")
        elif label == "Notepad":
            command = [shutil.which("notepad.exe") or "notepad.exe"]
        elif label in {"Calculator", "Paint", "Explorer", "Snipping Tool"}:
            root = Path(os.environ.get("WINDIR", r"C:\Windows"))
            executable = {
                "Calculator": root / "System32/calc.exe",
                "Paint": root / "System32/mspaint.exe",
                "Explorer": root / "explorer.exe",
                "Snipping Tool": root / "System32/SnippingTool.exe",
            }[label]
            if not executable.is_file():
                return ScriptResult(False, "", f"{label} is not installed here")
            command = [str(executable)]
        elif label == "Microsoft Edge":
            candidates = [
                Path(os.environ.get(root, "")) / "Microsoft/Edge/Application/msedge.exe"
                for root in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
            ]
            executable = next((str(p) for p in candidates if p.is_file()), shutil.which("msedge.exe"))
            if executable:
                command = [executable]
    if tool not in {"open_app", "chrome_open"}:
        from .windows_desktop import run

        return run(tool, args)
    if command is None:
        return None  # Unsupported names retain the existing PowerShell implementation.
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True
        )
        return ScriptResult(True, label if label.startswith("http") else f"{label} launch requested")
    except OSError as exc:
        return ScriptResult(False, "", str(exc))


@profiled("os.native_launch")
async def execute(tool: str, args: dict[str, str]) -> ScriptResult | None:
    return await asyncio.to_thread(launch, tool, args)
