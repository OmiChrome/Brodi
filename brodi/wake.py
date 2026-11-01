"""Text-only wake-prefix utilities for command tests; no audio or model loading."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass


@dataclass
class WakeStats:
    detections: int = 0
    commands: int = 0
    expired_windows: int = 0
    false_positives: int = 0

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / max(1, self.detections)


class WakeController:
    """Testable text/engine boundary: wake phrase -> remainder, or a brief answer window."""

    def __init__(
        self,
        on_command: Callable[[str], None],
        *,
        on_wake: Callable[[], None] | None = None,
        on_event: Callable | None = None,
        words: list[str] | None = None,
        mode: str | None = None,
        window_s: float = 20,
    ):
        words = words or os.environ.get("BRODI_WAKE_WORDS", "hey bro,yo bro").split(",")
        self.pattern = re.compile(
            r"^\s*(?:"
            + "|".join(re.escape(w.strip()).replace(r"\ ", r"[\s,]+") for w in words if w.strip())
            + r")\b[\s,!.:;?-]*(?:please\s+)?",
            re.I,
        )
        self.mode = mode or os.environ.get("BRODI_WAKE_MODE", "both")
        if self.mode not in {"continuous", "hotkey", "both"}:
            raise ValueError("BRODI_WAKE_MODE must be continuous, hotkey, or both")
        self.on_command, self.on_wake, self.on_event = on_command, on_wake, on_event
        self.window_s = window_s
        self.active_until = 0.0
        self.in_utterance = False
        self.stats = WakeStats()

    def _event(self, message: str):
        if self.on_event:
            self.on_event(
                "progress",
                message,
                counters={**asdict(self.stats), "false_positive_rate": self.stats.false_positive_rate},
            )

    def activate(self) -> None:
        self.active_until = time.monotonic() + self.window_s
        self.in_utterance = True
        self.stats.detections += 1
        if self.on_wake:
            self.on_wake()
        self._event("Wake detected; listening for your task")

    def mark_false_positive(self) -> None:
        self.stats.false_positives = min(self.stats.detections, self.stats.false_positives + 1)
        self._event("Wake marked as a false positive")

    def tick(self) -> None:
        if self.active_until and time.monotonic() > self.active_until:
            self.stats.expired_windows += 1
            self.active_until = 0
            self.in_utterance = False
            self._event("Wake listening window expired; waiting locally")

    def feed_text(self, text: str, *, final: bool = True) -> None:
        now = time.monotonic()
        match = self.pattern.match(text)
        if match and self.mode != "hotkey" and not self.in_utterance:
            self.activate()
        active = now <= self.active_until
        if not active:
            if self.active_until:
                self.stats.expired_windows += 1
                self.active_until = 0
            self.in_utterance = False
            return
        if final:
            remainder = text[match.end() :].strip() if match else text.strip()
            self.in_utterance = False
            self.active_until = time.monotonic() + self.window_s
            if remainder:
                self.stats.commands += 1
            self.on_command(remainder)
