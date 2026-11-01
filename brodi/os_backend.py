"""Cross-platform OS layer: AppleScript on macOS, PowerShell on Windows 11.

Everything that talks to the operating system goes through this module: script execution,
syntax validation, argument escaping, and desktop context (frontmost/running/installed apps).
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profiler import profiled

SCRIPT_TIMEOUT_S = 20.0

_APP_ALIASES = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "edge": "Microsoft Edge",
    "explorer": "Explorer",
    "code": "Visual Studio Code",
    "slack": "Slack",
    "spotify": "Spotify",
    "notepad": "Notepad",
    "notepad++": "Notepad++",
}


def platform_name(platform: str | None = None) -> str:
    return platform or os.environ.get("BRODI_OS") or sys.platform


def is_windows(platform: str | None = None) -> bool:
    return platform_name(platform).startswith("win")


def platform_noun() -> str:
    return "this PC" if is_windows() else "this Mac"


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    output: str
    error: str = ""

    @property
    def text(self) -> str:
        return self.output if self.ok else self.error


def escape_applescript_string(value: str) -> str:
    """Escape a Python string for interpolation inside an AppleScript "..." literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def escape_powershell_string(value: str) -> str:
    """Escape for a PowerShell double-quoted literal: backtick, quote, and ``$``.

    Escaping ``$`` prevents subexpression injection (``$()``) from a spoken argument.
    """
    return value.replace("`", "``").replace('"', '`"').replace("$", "`$")


def _powershell_exe() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def _clean_powershell_error(error: str) -> str:
    """Keep the human sentence, drop stack noise, source echoes and file locations."""
    lines = []
    for line in error.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("+", "~~~", "At ")):
            continue
        stripped = re.sub(r"^.*\\tool\.ps1\s*:\s*", "", stripped)
        # PowerShell echoes the offending source before its message: "...; Write-Error\n\"x\" : boom".
        if " : " in stripped:
            head, _, tail = stripped.rpartition(" : ")
            if len(head) > 80 or head.startswith("Write-"):
                stripped = tail.strip()
        lines.append(stripped)
    return "\n".join(lines) or error.strip()


# Fail loudly rather than reporting success when a cmdlet only printed an error: the agent
# must hear "that failed", never a silent no-op. Scripts opt out locally with -ErrorAction.
_POWERSHELL_PRELUDE = "$ErrorActionPreference = 'Stop';\n"


def _run_powershell(script: str, timeout: float = SCRIPT_TIMEOUT_S) -> ScriptResult:
    """Run PowerShell source (sync). Used by compile checks and simple probes.

    Output is captured through temp files instead of pipes: a launched GUI app inherits
    PowerShell's stdout, and an inherited pipe would keep this call blocked until that app
    exits (Explorer, Chrome, Notepad...).
    """
    with tempfile.TemporaryDirectory(prefix="brodi-ps-") as tmp:
        source = Path(tmp) / "tool.ps1"
        source.write_text(_POWERSHELL_PRELUDE + script, encoding="utf-8")
        out_path, err_path = Path(tmp) / "out.txt", Path(tmp) / "err.txt"
        with open(out_path, "w+b") as out, open(err_path, "w+b") as err:
            try:
                proc = subprocess.run(
                    [
                        _powershell_exe(),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(source),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return ScriptResult(False, "", f"PowerShell timed out after {timeout:.0f}s")
            except OSError as exc:
                return ScriptResult(False, "", str(exc))
            out.seek(0)
            err.seek(0)
            output = out.read().decode("utf-8", "replace").strip()
            error = _clean_powershell_error(err.read().decode("utf-8", "replace").strip())
    if proc.returncode != 0:
        return ScriptResult(False, output, error or f"PowerShell exited {proc.returncode}")
    return ScriptResult(True, output, error)


async def _run_osascript(script: str, timeout: float) -> ScriptResult:
    proc = await asyncio.create_subprocess_exec(
        "osascript",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(script.encode("utf-8")), timeout)
    except TimeoutError:
        proc.kill()
        return ScriptResult(False, "", f"AppleScript timed out after {timeout:.0f}s")
    output = out.decode("utf-8", "replace").strip()
    error = err.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        error = re.sub(r"^\d+:\d+:\s*(execution|syntax) error:\s*", "", error)
        return ScriptResult(False, output, error or f"osascript exited {proc.returncode}")
    return ScriptResult(True, output, error)


@profiled("os.script")
async def run_os_script(script: str, timeout: float = SCRIPT_TIMEOUT_S) -> ScriptResult:
    """Run an OS script with the platform's interpreter and capture its result."""
    if is_windows():
        return await asyncio.to_thread(_run_powershell, script, timeout)
    return await _run_osascript(script, timeout)


# ------------------------------------------------------------------ syntax validation


def _compile_applescript(script: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "tool.applescript"
        source.write_text(script)
        try:
            proc = subprocess.run(
                ["osacompile", "-o", str(Path(directory) / "tool.scpt"), str(source)],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    err = proc.stderr.decode("utf-8", "replace").strip()
    err = re.sub(r"^\d+:\d+:\s*(execution|syntax) error:\s*", "", err)
    return False, err or "osacompile rejected the script"


_POWERSHELL_AST_PREFIX = "$source = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('"
_POWERSHELL_AST_SUFFIX = """'))
$tokens = $null; $errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { [Console]::Error.WriteLine(("line {0}: {1} Offending line: {2}" -f $errors[0].Extent.StartLineNumber, $errors[0].Message, $errors[0].Extent.Text)); exit 1 }
"""


def compile_script(script: str) -> tuple[bool, str]:
    """Validate a script without executing it. Returns ``(ok, error)``."""
    if is_windows():
        payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        result = _run_powershell(_POWERSHELL_AST_PREFIX + payload + _POWERSHELL_AST_SUFFIX, timeout=30)
        # The AST wrapper prints its diagnostic on stdout; stderr carries host noise on failure.
        return result.ok, (result.error or result.output)
    return _compile_applescript(script)


# ------------------------------------------------------------------ desktop context


def _normalise_app_name(name: str) -> str:
    stem = name.strip()
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    return _APP_ALIASES.get(stem.lower(), stem)


def _windows_active_app() -> str:
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil

        return _normalise_app_name(psutil.Process(pid.value).name())
    except Exception:
        return "Explorer"


def _mac_active_app() -> str:
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "Finder"
    out = proc.stdout.decode("utf-8", "replace").strip()
    return out or "Finder"


def get_active_app() -> str:
    return _windows_active_app() if is_windows() else _mac_active_app()


def get_active_window_title() -> str:
    if not is_windows():
        return ""  # App context remains available; do not add an expensive AppleScript probe.
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        window = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        return buffer.value[:300]
    except Exception:
        return ""


def _windows_running_apps() -> list[str]:
    import psutil

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    names: set[str] = set()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: Any, _lparam: Any) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            names.add(_normalise_app_name(psutil.Process(pid.value).name()))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback_type(callback), 0)
    except Exception:
        return ["Explorer"]
    return sorted(names, key=str.lower) or ["Explorer"]


def _mac_running_apps() -> list[str]:
    script = 'tell application "System Events" to get name of every application process whose background only is false'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ["Finder"]
    out = proc.stdout.decode("utf-8", "replace").strip()
    apps = [a.strip() for a in out.split(",") if a.strip()]
    return sorted(set(apps), key=str.lower) or ["Finder"]


def get_running_apps() -> list[str]:
    return _windows_running_apps() if is_windows() else _mac_running_apps()


_MAC_APP_DIRS = (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")


def _windows_installed_apps(limit: int) -> list[str]:
    roots = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Microsoft/Windows/Start Menu/Programs",
    ]
    names: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for link in root.rglob("*.lnk"):
            names.add(link.stem)
    return sorted(names, key=str.lower)[:limit]


def get_installed_apps(limit: int = 200) -> list[str]:
    """Installed application names. Cheap filesystem scan (no launching)."""
    if is_windows():
        return _windows_installed_apps(limit)
    names: set[str] = set()
    for directory in _MAC_APP_DIRS:
        if not directory.is_dir():
            continue
        for app in directory.glob("*.app"):
            names.add(app.stem)
    return sorted(names, key=str.lower)[:limit]
