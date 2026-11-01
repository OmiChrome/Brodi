"""Chrome profile resolution and launching on macOS and Windows 11.

Chrome opens new windows in the last-used profile, which on a multi-profile machine is often
the wrong account. Every Chrome-launching script uses the ``{{chrome_profile}}`` built-in,
resolved here by matching BRODI_CHROME_PROFILE_EMAIL against Chrome's Local State file.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger("brodi.chrome")


def local_state_path(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Chrome's Local State file for the current (or given) platform."""
    env = os.environ if env is None else env
    home = home or Path.home()
    if (platform or os.environ.get("BRODI_OS") or sys.platform).startswith("win"):
        base = Path(env.get("LOCALAPPDATA") or (home / "AppData/Local"))
        return base / "Google/Chrome/User Data/Local State"
    return home / "Library/Application Support/Google/Chrome/Local State"


def find_chrome_executable(platform: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Path to chrome.exe on Windows 11 (first existing install), else the best guess."""
    env = os.environ if env is None else env
    host = platform or env.get("BRODI_OS") or sys.platform
    if host == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
        return str(next((path for path in candidates if path.is_file()), candidates[0]))
    if not host.startswith("win"):
        return (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium")
            or "google-chrome"
        )
    candidates = []
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = env.get(key)
        if base:
            candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    return found or str(candidates[-1] if candidates else "chrome.exe")


def launch_chrome_command(
    url: str,
    profile: str,
    new_window: bool = False,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """OS-appropriate command list that opens ``url`` in the pinned Chrome profile."""
    if (platform or os.environ.get("BRODI_OS") or sys.platform).startswith("win"):
        command = [find_chrome_executable(platform, env), f"--profile-directory={profile}"]
        if new_window:
            command.append("--new-window")
        if url:
            command.append(url)
        return command
    command = ["open", "-na", "Google Chrome", "--args", f"--profile-directory={profile}"]
    if new_window:
        command.append("--new-window")
    if url:
        command.append(url)
    return command


LOCAL_STATE = local_state_path()
PROFILE_EMAIL = os.environ.get("BRODI_CHROME_PROFILE_EMAIL", "")  # empty: use Chrome's last-used profile
PROFILE_DIR_OVERRIDE = os.environ.get("BRODI_CHROME_PROFILE_DIR")  # e.g. "Profile 4"
HOME_URL = os.environ.get("BRODI_CHROME_HOME", "")  # empty = Chrome's new-tab page

_cache: tuple[float, str] | None = None
_TTL = 60.0


def profiles() -> dict[str, dict[str, str]]:
    """{profile_dir: {"name":..., "email":...}} from Local State; empty if unreadable."""
    try:
        info = json.loads(LOCAL_STATE.read_text())["profile"]["info_cache"]
    except (OSError, KeyError, ValueError):
        return {}
    return {k: {"name": v.get("name", ""), "email": v.get("user_name", "")} for k, v in info.items()}


def profile_dir() -> str:
    """Directory name for the configured account. Falls back to Chrome's last-used profile."""
    global _cache
    if PROFILE_DIR_OVERRIDE:
        return PROFILE_DIR_OVERRIDE
    now = time.monotonic()
    if _cache and now - _cache[0] < _TTL:
        return _cache[1]
    found = ""
    if PROFILE_EMAIL:
        for directory, meta in profiles().items():
            if meta["email"].lower() == PROFILE_EMAIL.lower():
                found = directory
                break
    if not found:
        try:
            found = json.loads(LOCAL_STATE.read_text())["profile"].get("last_used", "Default")
        except (OSError, KeyError, ValueError):
            found = "Default"
        if PROFILE_EMAIL:
            log.warning("no Chrome profile signed in as %s; using %s", PROFILE_EMAIL, found)
    _cache = (now, found)
    return found


def system_vars() -> dict[str, str]:
    """Built-in placeholders every tool script may use."""
    return {"chrome_profile": profile_dir(), "chrome_home": HOME_URL}
