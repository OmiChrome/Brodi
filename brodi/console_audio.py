"""Release LiveKit's local audio devices before its job event loop closes."""

from __future__ import annotations

import logging

from livekit.agents import AgentSession
from livekit.agents.cli.cli import AgentsConsole

log = logging.getLogger(__name__)


def close_console_audio(session: AgentSession | None) -> None:
    """Stop callbacks for this session, including on failed session startup.

    LiveKit 1.8's legacy console owns sounddevice streams outside the job loop
    and doesn't release them when the session closes. Its callbacks then try to
    schedule work on the closed loop, producing Windows Python-CFFI dialogs.
    Keep the private console integration here; room and TCP sessions have no
    local devices to close.
    """
    console = AgentsConsole._instance
    if session is None or console is None or not hasattr(console, "set_speaker_enabled"):
        return

    with console._lock:
        if not console._io_acquired or console._io_session is not session:
            return
        # Both callbacks check this flag. Disable dispatch before waiting for
        # in-flight callbacks to finish, while their event loop is still alive.
        console._io_acquired = False

    # Do not hold the console lock: the output callback also acquires it, and
    # sounddevice.close() waits for callbacks to return.
    for disable in (console.set_microphone_enabled, console.set_speaker_enabled):
        try:
            disable(False)
        except Exception:
            log.exception("Could not close a console audio device")
