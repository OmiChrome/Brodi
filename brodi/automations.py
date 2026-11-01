"""Reviewed, deterministic presentation plans; execution uses normal agent gates."""

from .commands import normalize
from .multi_step import Plan, Step

# Tool identifiers and arguments only. No generated scripts or model decisions.
AUTOMATIONS = {
    "presentation workspace": (
        "Open Chrome and the project website",
        (("open_app", {"app": "Google Chrome"}), ("chrome_open", {"site": "other", "query": "github.com"})),
    ),
    "presentation research": (
        "Show a YouTube search for ambient piano",
        (("chrome_open", {"site": "youtube", "query": "ambient piano"}),),
    ),
    "presentation reference": (
        "Open the Python reference",
        (("chrome_open", {"site": "other", "query": "https://docs.python.org/3/"}),),
    ),
}


def predefined_plan(utterance: str) -> Plan | None:
    text = normalize(utterance)[0].casefold().rstrip(".!?")
    if not text.startswith("run "):
        return None
    task = AUTOMATIONS.get(text[4:])
    if task is None:
        return None
    return Plan(steps=[Step(tool=tool, args=dict(args)) for tool, args in task[1]])
