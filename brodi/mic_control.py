"""Same-checkout microphone control for the tray and voice process.

Commands are explicit mute states, not toggles. A session nonce prevents a stale
request (or reused PID) from affecting a restarted voice session. No network port
or provider connection is involved.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from .activity import default_path


def control_dir() -> Path:
    return default_path().parent / "microphone"


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_state(pid: int) -> dict | None:
    try:
        value = json.loads((control_dir() / f"{int(pid)}.state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("pid") == pid else None
    except (OSError, ValueError):
        return None


def request_mute(pid: int, muted: bool) -> None:
    """Request an explicit state; the activity event is the acknowledgement."""
    if not isinstance(muted, bool):
        raise ValueError("muted must be a boolean")
    state = read_state(pid)
    if not state or not state.get("mic_available"):
        raise RuntimeError("Microphone control is not ready. Wait for Brodi to finish starting.")
    _write(
        control_dir() / f"{int(pid)}.request.json",
        {
            "session": state["session"],
            "muted": muted,
            "request_id": uuid.uuid4().hex,
        },
    )


class MicrophoneMailbox:
    def __init__(self):
        self.pid = os.getpid()
        self.session = uuid.uuid4().hex
        self.state_path = control_dir() / f"{self.pid}.state.json"
        self.request_path = control_dir() / f"{self.pid}.request.json"

    def publish(self, *, muted: bool, available: bool, hotkey_available: bool):
        _write(
            self.state_path,
            {
                "pid": self.pid,
                "session": self.session,
                "mic_muted": muted,
                "mic_available": available,
                "hotkey_available": hotkey_available,
            },
        )

    def poll(self) -> bool | None:
        # Leave the last request in place until acknowledged by a new session/state.
        # Reading without unlinking avoids losing a concurrent replacement.
        try:
            value = json.loads(self.request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(value, dict) and value.get("session") == self.session and isinstance(value.get("muted"), bool):
            command = value["muted"]
            # Distinguish an intentional repeated request from rereading the same file.
            request_id = value.get("request_id")
            if request_id != getattr(self, "_last_request", None):
                self._last_request = request_id
                return command
        return None

    def close(self):
        self.state_path.unlink(missing_ok=True)
        self.request_path.unlink(missing_ok=True)
