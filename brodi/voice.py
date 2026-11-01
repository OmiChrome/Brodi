"""Voice activation, independent of audio hardware and network providers."""

from __future__ import annotations

import time

from .commands import normalize


class VoiceGate:
    """Require a wake phrase, then allow brief commands and answers for a short window."""

    def __init__(self, *, required: bool = True, window_s: float = 20):
        self.required = required
        self.window_s = max(1, window_s)
        self.active_until = 0.0

    def activate(self) -> None:
        self.active_until = time.monotonic() + self.window_s

    def accepts(self, text: str, *, pending: bool = False) -> bool:
        if not text.strip():
            return False
        _, wake = normalize(text)
        if wake:
            self.activate()
        return not self.required or wake or pending or time.monotonic() < self.active_until
