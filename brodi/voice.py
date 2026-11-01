"""Voice activation, independent of audio hardware and network providers."""

from __future__ import annotations

import textwrap
import time
import unicodedata
from typing import TYPE_CHECKING

from .activity import clean
from .commands import CALCULATION_QUESTION, TASK_HINT, normalize

if TYPE_CHECKING:
    from .agent import Outcome

WAKE_REPLY = "Yes sir, I'm listening."


def english_script(text: str) -> bool:
    """Reject non-Latin speech transcripts before tools; retain English names with accents.

    This is a script guard, not a language classifier. The cloud recognizer is
    separately pinned to English and the intent router accepts English commands.
    """
    return all(not char.isalpha() or "LATIN" in unicodedata.name(char, "") for char in text)


def spoken_reply(outcome: Outcome, *, confirmation: bool = False, missing: str = "") -> str | None:
    """Brief acknowledgements, useful questions, and the actual reason for failures.

    Successful tool output stays in activity rather than becoming arbitrary speech.
    """
    if outcome.stop:
        return "Goodbye."
    route = outcome.route
    text = (outcome.speak or "").strip()
    lower = text.casefold()
    if lower in {"hey bro, i'm listening.", "yo bro.", "listening."} or (route and route.kind == "greeting"):
        return WAKE_REPLY
    if confirmation:
        return "Review action."
    if missing == "expression":
        return CALCULATION_QUESTION
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
    if outcome.handoff_to_llm or (route and route.kind == "chat"):
        return TASK_HINT
    if not outcome.executed and outcome.observation.get("error"):
        return _spoken_detail(str(outcome.observation["error"]))
    if not text:
        return None
    if lower in {"cancelled.", "canceled."}:
        return "Cancelled."
    if "expired" in lower:
        return "Please repeat."
    if "timed out" in lower or "timeout" in lower:
        return _spoken_detail(text)
    if not outcome.executed:
        if "blocked" in lower or "policy" in lower:
            return _spoken_detail(text)
        if any(word in lower for word in ("didn't", "couldn't", "can't", "failed", "unavailable", "not installed")):
            return _spoken_detail(text)
        if "?" in text:
            return "More details?"
        return "Check activity."
    if route and route.tool and route.tool.speak not in {"done", "Got it.", "Gotcha.", "On it.", "Done."}:
        return "Check activity."
    return "Got it."


def _spoken_detail(text: str) -> str:
    text = " ".join(str(clean(text)).split())
    if not english_script(text):
        return "The task failed. Check the activity view for the error."
    return textwrap.shorten(text, width=240, placeholder="...")


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
