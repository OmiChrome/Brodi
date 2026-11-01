"""Actionable API failures, with a desktop notice that survives voice shutdown."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .activity import ActivityLog

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
NOTICE_COOLDOWN_S = 300
_shown: set[str] = set()


@dataclass(frozen=True)
class ApiIssue:
    title: str
    message: str


ISSUES = {
    "credits_exhausted": ApiIssue(
        "API credits exhausted",
        "OpenAI reports that this API account has no credits remaining.\n\n"
        "Add API credits in OpenAI billing, or update Brodi with a funded API key. "
        "Then restart Brodi from the tray menu.",
    ),
    "invalid_key": ApiIssue(
        "Invalid API key",
        "OpenAI rejected Brodi's API key. It may be incorrect, expired, or revoked.\n\n"
        "Update OPENAI_API_KEY in Brodi's .env.local file, then restart Brodi from the tray menu.",
    ),
    "missing_key": ApiIssue(
        "API key missing",
        "Brodi has no OpenAI API key configured.\n\n"
        "Set OPENAI_API_KEY in Brodi's .env.local file, then restart Brodi from the tray menu.",
    ),
    "quota_exceeded": ApiIssue(
        "API usage limit reached",
        "OpenAI reports that this API account has reached a billing or usage limit.\n\n"
        "Check the account's credits and project or organization limits in OpenAI settings, "
        "then restart Brodi from the tray menu.",
    ),
}


def classify_api_error(error: object) -> str | None:
    """Unwrap SDK exceptions and LiveKit's STT/TTS error events, without exposing keys."""
    pending = [error]
    seen: set[int] = set()
    codes: set[str] = set()
    messages: list[str] = []
    unauthorized = False
    while pending and len(seen) < 20:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))

        def get(key, current=current):
            return current.get(key) if isinstance(current, dict) else getattr(current, key, None)

        for name in ("code", "type"):
            if isinstance(value := get(name), str):
                codes.add(value.lower())
        if isinstance(message := get("message"), str):
            messages.append(message.lower())
        elif isinstance(current, BaseException):
            messages.append(str(current).lower())
        unauthorized |= get("status_code") == 401
        pending.extend(get(name) for name in ("error", "body", "__cause__", "__context__"))

    text = " ".join(messages)
    if "credit_balance_exhausted" in codes or "no credits remaining" in text:
        return "credits_exhausted"
    if unauthorized or "invalid_api_key" in codes or "incorrect api key" in text or "invalid api key" in text:
        return "invalid_key"
    if (
        codes
        & {
            "insufficient_quota",
            "project_spend_limit_exceeded",
            "organization_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
            "usage_limit_exceeded",
        }
        or "exceeded your current quota" in text
    ):
        return "quota_exceeded"
    if "openai_api_key" in text and any(word in text for word in ("must be set", "required", "set openai_api_key")):
        return "missing_key"
    return None  # A temporary 429 rate limit is not exhausted credits.


def report_api_error(error: object, activity: ActivityLog | None) -> bool:
    code = classify_api_error(error)
    if code is None:
        return False
    issue = ISSUES[code]
    if activity is not None:
        activity.emit("error", issue.title, api_error=code, reason=issue.message)
    # Only local desktop sessions show windows; server workers keep the journal.
    if "console" in sys.argv[1:] or os.environ.get("BRODI_BACKGROUND", "").lower() in {"true", "1"}:
        notify_api_error(code)
    return True


def notify_api_error(code: str) -> None:
    if code in _shown or sys.platform not in {"win32", "darwin"}:
        return
    try:
        executable = Path(sys.executable)
        if sys.platform == "win32" and (windowless := executable.with_name("pythonw.exe")).exists():
            executable = windowless
        subprocess.Popen(
            [str(executable), "-m", "brodi.api_errors", code],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=sys.platform != "win32",
        )
        _shown.add(code)
    except OSError:
        log.exception("Could not display the API error window; details are in activity")


def show_notice(code: str) -> None:
    from .background import instance_lock, service_dir

    issue = ISSUES[code]
    folder = service_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # One window at a time across worker restarts. A dismissed notice is also
    # throttled so the supervisor cannot create a new popup on every retry.
    with instance_lock(folder / "api-notice.lock") as acquired:
        if not acquired:
            return
        stamp = folder / f"api-notice-{code}.shown"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < NOTICE_COOLDOWN_S:
            return
        show_dialog(issue)
        stamp.touch()


def show_dialog(issue: ApiIssue) -> None:
    title = "Brodi - " + issue.title
    if sys.platform == "win32":
        import ctypes

        finished = threading.Event()
        flasher = threading.Thread(target=_flash_dialog, args=(title, finished), daemon=True)
        flasher.start()
        try:
            ctypes.windll.user32.MessageBoxW(None, issue.message, title, 0x30 | 0x10000)
        finally:
            finished.set()
            flasher.join(timeout=0.2)
    elif sys.platform == "darwin":
        # Arguments carry only fixed, reviewed text, never raw provider errors.
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv\ndisplay alert (item 1 of argv) message (item 2 of argv) as critical\nend run",
                title,
                issue.message,
            ],
            check=True,
        )


def _flash_dialog(title: str, finished: threading.Event) -> None:
    """Flash this process's warning caption and taskbar button six times."""
    import ctypes
    from ctypes import wintypes

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
    for _ in range(100):
        if finished.wait(0.05):
            return
        hwnd = user32.FindWindowW("#32770", title)
        owner = wintypes.DWORD()
        if hwnd:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == os.getpid():
                info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 3, 6, 500)
                user32.FlashWindowEx(ctypes.byref(info))
                return


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Show a Brodi API error")
    parser.add_argument("code", choices=ISSUES)
    show_notice(parser.parse_args().code)
