"""Structured local activity journal and a readable, live terminal view."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import textwrap
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

log = logging.getLogger("brodi.activity")
ROOT = Path(__file__).resolve().parent.parent
LABELS = {
    "status": "STATE",
    "input": "YOU",
    "action": "DOING",
    "output": "BRODI",
    "observation": "GATHERED",
    "result": "RESULT",
    "progress": "PROGRESS",
    "error": "ERROR",
}
COLORS = {
    "status": "36",
    "input": "35",
    "action": "33",
    "output": "32",
    "observation": "34",
    "result": "32",
    "progress": "36",
    "error": "31",
}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MIC_FIELDS = {"mic_muted", "mic_available", "hotkey_available", "hint"}


@dataclass(frozen=True)
class MicStatus:
    label: str = "WAITING"
    color: str = "1;30;43"
    hint: str = "Waiting for microphone status..."


def microphone_status(event: dict[str, Any]) -> MicStatus | None:
    details = event.get("details") or {}
    if not isinstance(details, dict) or not isinstance(details.get("mic_muted"), bool):
        if event.get("kind") == "status" and event.get("message") == "Starting Brodi":
            return MicStatus()
        if event.get("kind") == "status" and event.get("message") == "Brodi stopped":
            return MicStatus("OFFLINE", "1;97;41", "Brodi is stopped. Start it from the tray.")
        return None
    if not details.get("mic_available"):
        return MicStatus("CONNECTING", "1;30;43", "Microphone is not ready yet.")
    if event.get("message", "").startswith("Resuming microphone"):
        return MicStatus("RESUMING", "1;30;43", "Wait for LISTENING before speaking.")
    muted = details["mic_muted"]
    shortcut = (
        "Ctrl+Space to " + ("unmute" if muted else "mute")
        if details.get("hotkey_available")
        else "Use the tray microphone control (shortcut unavailable)"
    )
    hint = (
        "Voice commands paused."
        if muted
        else (
            "Say Hey bro, then your task." if "Hey bro" in event.get("message", "") else "Ready for your voice command."
        )
    )
    return MicStatus("MUTED" if muted else "LISTENING", "1;97;41" if muted else "1;30;42", f"{shortcut} | {hint}")


def _supports_color(stream: TextIO) -> bool:
    if not stream.isatty() or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # A fresh Windows console is a TTY but may not interpret ANSI sequences.
    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetConsoleMode.restype = wintypes.BOOL
        api.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.SetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if not api.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004 or api.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, ValueError):
        return False


def render_screen(events, mic: MicStatus, *, width: int, height: int) -> str:
    """Keep the current mic state above a bounded, readable tail of the journal."""
    width = max(30, width - 1)  # avoid autowrap at the console's right edge
    badge = f"  MIC {mic.label}  ".ljust(width)
    lines = ["\033[1mBRODI | LIVE ACTIVITY\033[0m", "", f"\033[{mic.color}m{badge}\033[0m"]
    lines.extend(textwrap.wrap(mic.hint, width=width))
    lines.extend(["", "\033[90m" + "-" * width + "\033[0m"])
    footer = "Q + Enter / Ctrl+C: close view. Brodi keeps running."
    footer_lines = textwrap.wrap(footer, width=width)
    space = max(0, height - len(lines) - len(footer_lines) - 1)
    activity_lines = [line for event in events for line in render_event(event, color=True, width=width).splitlines()]
    if not activity_lines:
        activity_lines = ["Waiting for activity..."]
    lines.extend(activity_lines[-space:] if space else [])
    lines.extend([""] * max(0, height - len(lines) - len(footer_lines) - 1))
    lines.extend([""] + footer_lines)
    return "\033[H" + "\n".join("\033[2K" + line for line in lines[:height]) + "\033[J"


def default_path() -> Path:
    value = os.environ.get("BRODI_ACTIVITY_LOG")
    return Path(value).expanduser().resolve() if value else ROOT / ".freebuff" / "activity.jsonl"


def clean(value: Any) -> Any:
    """Keep terminal controls and known credentials out of the human activity feed."""
    if isinstance(value, dict):
        return {
            str(clean(k)): ("[redacted]" if re.search(r"password|secret|api.?key|token", str(k), re.I) else clean(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value[:20]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = _CONTROLS.sub("", _ANSI.sub("", str(value)))
    for name, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"(?:KEY|SECRET|TOKEN|PASSWORD)$", name, re.I):
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[redacted]", text)
    return text[:4000]


class ActivityLog:
    def __init__(self, path: Path | None = None, *, source: str = "voice"):
        self.path = path or default_path()
        self.source = source
        self.session = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._last_status = ""
        self._warned = False

    def emit(self, kind: str, message: str, **details: Any) -> None:
        message = " ".join(str(clean(message)).split())
        with self._lock:
            if kind == "status" and message == self._last_status:
                return
            event = {
                "time": time.time(),
                "session": self.session,
                "pid": os.getpid(),
                "parent_pid": os.getppid(),
                "source": self.source,
                "kind": kind,
                "message": message,
                "details": clean(details),
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._last_status = message if kind == "status" else ""
            except OSError:
                if not self._warned:
                    log.warning("could not write activity log at %s", self.path)
                    self._warned = True


class JournalReader:
    """Read appended complete records; preserve a partial line for the next poll."""

    def __init__(self, path: Path):
        self.path = path
        self.position = 0
        self.partial = b""
        self.identity = None

    def poll(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if identity != self.identity or stat.st_size < self.position:
                    self.position, self.partial = 0, b""
                self.identity = identity
                # Bound initial history reads even when the raw journal has grown large.
                if self.position == 0 and stat.st_size > 256 * 1024:
                    stream.seek(-256 * 1024, os.SEEK_END)
                    stream.readline()
                else:
                    stream.seek(self.position)
                data = stream.read()
                self.position = stream.tell()
        except OSError:
            return []
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        events = []
        for line in lines:
            try:
                event = json.loads(line)
                if (
                    isinstance(event, dict)
                    and isinstance(event.get("time"), (int, float))
                    and isinstance(event.get("message"), str)
                    and isinstance(event.get("kind"), str)
                ):
                    events.append(event)
            except (ValueError, UnicodeDecodeError):
                continue
        return events


def render_event(event: dict[str, Any], *, color: bool = False, width: int = 100) -> str:
    kind = event.get("kind", "status")
    try:
        stamp = datetime.fromtimestamp(event["time"]).strftime("%H:%M:%S")
    except (ValueError, OverflowError, OSError):
        stamp = "--:--:--"
    mic = microphone_status(event)
    label = "MIC" if mic else LABELS.get(kind, "INFO")
    heading = f"{stamp}  {label:<8}"
    if color:
        heading = f"\033[{COLORS.get(kind, '37')}m{heading}\033[0m"
    message = f"{mic.label} | {mic.hint}" if mic else event["message"]
    message_lines = textwrap.wrap(" ".join(str(clean(message)).split()), width=max(10, width - 20)) or [""]
    if color and mic:
        message_lines[0] = f"\033[{mic.color}m{message_lines[0]}\033[0m"
    lines = [heading + "  " + message_lines[0]]
    lines.extend(" " * 20 + line for line in message_lines[1:])
    details = clean(event.get("details") or {})
    if not isinstance(details, dict):
        details = {"detail": details}
    for key, value in details.items():
        if mic and key in _MIC_FIELDS:
            continue
        if value in (None, "", {}, []):
            continue
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        wrapped = textwrap.wrap(f"{key.replace('_', ' ').capitalize()}: {text}", width=max(30, width - 20))
        lines.extend(" " * 20 + line for line in wrapped[:8])
        if len(wrapped) > 8:
            lines.append(" " * 20 + "... (more in the activity journal)")
    return "\n".join(lines)


def view(
    path: Path,
    *,
    follow: bool = True,
    tail: int = 12,
    since: float = 0,
    pid: int | None = None,
    stream: TextIO | None = None,
) -> int:
    stream = stream or sys.stdout
    color = _supports_color(stream)
    screen = follow and color
    width = shutil.get_terminal_size((100, 24)).columns
    if not screen:
        print("\nBRODI | LIVE ACTIVITY" if follow else "\nBRODI | ACTIVITY", file=stream, flush=True)
        print(
            "Q + Enter or Ctrl+C closes this view; Brodi keeps running." if follow else "Recent activity",
            file=stream,
            flush=True,
        )
        print("-" * min(width, 76), file=stream, flush=True)
    reader = JournalReader(path)
    events = reader.poll()
    # Don't replay another process's history as the current assistant's status.
    if pid is not None:
        events = [event for event in events if pid in (event.get("pid"), event.get("parent_pid"))]
    if events and not since:
        latest = events[-1].get("session")
        events = [event for event in events if event.get("session") == latest]
    mic = MicStatus()
    for event in events:
        mic = microphone_status(event) or mic
    events = [event for event in events if event["time"] >= since][-max(1, tail) :]
    recent = deque(events, maxlen=200)
    if not screen:
        print(f"MIC {mic.label} | {mic.hint}\n", file=stream, flush=True)
        for event in events:
            print(render_event(event, color=color, width=width), file=stream, flush=True)
    if not events and not screen:
        print(
            "Waiting for Brodi to report activity..." if follow else "No activity recorded for this session yet.",
            file=stream,
            flush=True,
        )
    if not follow:
        return 0
    import psutil

    close_requested = threading.Event()
    if sys.stdin.isatty():

        def read_keys() -> None:
            try:
                for line in sys.stdin:
                    if line.strip().lower() in {"q", "quit", "exit"}:
                        close_requested.set()
                        return
            except (OSError, ValueError):
                pass

        threading.Thread(target=read_keys, name="brodi-activity-keys", daemon=True).start()

    process = None
    if pid is not None:
        try:
            process = psutil.Process(pid)
        except psutil.Error:
            print("Brodi is stopped.", file=stream, flush=True)
            return 0
    size = shutil.get_terminal_size((100, 24))
    # The alternate screen keeps the microphone banner visible while the log
    # grows, and restores the caller's terminal when this view closes.
    if screen:
        stream.write("\033[?1049h\033[?25l")
        stream.flush()
    try:
        if screen:
            stream.write(render_screen(recent, mic, width=size.columns, height=size.lines))
            stream.flush()
        while True:
            if close_requested.is_set():
                ending = "Activity view closed. Brodi keeps running."
                break
            changed = False
            for event in reader.poll():
                if event["time"] >= since and (pid is None or pid in (event.get("pid"), event.get("parent_pid"))):
                    mic = microphone_status(event) or mic
                    recent.append(event)
                    changed = True
                    if not screen:
                        print(render_event(event, color=color, width=width), file=stream, flush=True)
            new_size = shutil.get_terminal_size((100, 24))
            if screen and (changed or new_size != size):
                size = new_size
                stream.write(render_screen(recent, mic, width=size.columns, height=size.lines))
                stream.flush()
            if process is not None:
                try:
                    stopped = not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
                except psutil.Error:
                    stopped = True
                if stopped:
                    ending = "Brodi is stopped."
                    break
            time.sleep(0.2)
    except KeyboardInterrupt:
        ending = "Activity view closed. Brodi keeps running."
    finally:
        if screen:
            stream.write("\033[0m\033[?25h\033[?1049l")
            stream.flush()
    print("\n" + ending, file=stream, flush=True)
    return 0


def main() -> None:
    from .config import load_environment

    load_environment()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Readable Brodi activity")
    parser.add_argument("--once", action="store_true", help="print a snapshot and exit")
    parser.add_argument("--tail", type=int, default=12)
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    try:
        code = view(args.path or default_path(), follow=not args.once, tail=args.tail, since=args.since, pid=args.pid)
    except BrokenPipeError:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
