"""Local setup checks. Never print credentials or read note content."""

import os
from pathlib import Path

from . import browser_task, chrome, policy


def report() -> list[tuple[str, bool, str]]:
    executable = chrome.find_chrome_executable()
    installed = Path(executable).is_file()
    error = browser_task.access_error() if installed else "Install Chrome to run browser tasks."
    return [
        (
            "OpenAI key",
            bool(os.environ.get("OPENAI_API_KEY")),
            "configured" if os.environ.get("OPENAI_API_KEY") else "set OPENAI_API_KEY in .env.local",
        ),
        ("Chrome", installed, "installed" if installed else "not installed"),
        ("Browser access", error is None, error or "connected"),
        ("Safety policy", policy.ENABLED, "enabled" if policy.ENABLED else "disabled by BRODI_POLICY=off"),
    ]
