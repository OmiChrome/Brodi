"""Voice activation, independent of audio hardware and network providers."""

from __future__ import annotations

import time
import unicodedata
from typing import TYPE_CHECKING

from .commands import normalize

if TYPE_CHECKING:
    from .agent import Outcome


def english_script(text: str) -> bool:
    """Reject non-Latin speech transcripts before tools; retain English names with accents.

    This is a script guard, not a language classifier. The cloud recognizer is
    separately pinned to English and the intent router accepts English commands.
    """
    return all(not char.isalpha() or "LATIN" in unicodedata.name(char, "") for char in text)


def spoken_reply(outcome: Outcome, *, confirmation: bool = False, missing: str = "") -> str | None:
    """Only fixed English phrases reach speech; full details remain in activity.

    Do not truncate arbitrary result text: that can hide a failure or accidentally
    read a number, a foreign-language page, or a generated tool's long template.
    Every possible reply has at most two words.
    """
    if outcome.stop:
        return "Goodbye."
    if confirmation:
        return "Review action."
    if missing:
        return {
            "query": "Search terms?",
            "app": "Which app?",
            "recipient": "Which recipient?",
            "content": "What text?",
            "text": "What text?",
            "exact_dates": "Which date?",
            "destination": "Which destination?",
            "origin": "Which origin?",
            "product": "Which product?",
            "site": "Which site?",
        }.get(missing, "More details?")
    route = outcome.route
    if outcome.handoff_to_llm or (route and route.kind == "chat"):
        return "Commands only."
    text = (outcome.speak or "").strip()
    if not text:
        return None
    lower = text.casefold()
    if lower in {"cancelled.", "canceled."}:
        return "Cancelled."
    if lower in {"hey bro, i'm listening.", "yo bro.", "listening."} or (route and route.kind == "greeting"):
        return "Listening."
    if "expired" in lower:
        return "Please repeat."
    if "timed out" in lower or "timeout" in lower:
        return "Timed out."
    if not outcome.executed:
        if "blocked" in lower or "policy" in lower:
            return "Action blocked."
        if any(word in lower for word in ("didn't", "couldn't", "can't", "failed", "unavailable", "not installed")):
            return "Didn't work."
        if "?" in text:
            return "More details?"
        return "Check activity."
    if route and route.tool and route.tool.speak not in {"done", "Got it.", "Gotcha.", "On it.", "Done."}:
        return "Check activity."
    return "Got it."


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
