"""Small, exact voice commands that don't need a network round trip to understand."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlsplit

from . import os_backend
from .calculator import request_expression

TASK_HINT = "I couldn't understand the task. Ask me to open an app, search, or calculate."
CALCULATION_QUESTION = "What should I calculate? Say the full expression."

WAKE = re.compile(r"^(?:(?:hey|yo|hello|hi|okay|ok)[\s,]+(?:brodi|bro|brody)|brodi|brody|bro)\b[\s,!.:;?-]*", re.I)
SITE_URLS = {
    "google": "https://www.google.com/",
    "youtube": "https://www.youtube.com/",
    "gmail": "https://mail.google.com/",
    "google_keep": "https://keep.google.com/",
    "google_calendar": "https://calendar.google.com/",
    "google_drive": "https://drive.google.com/",
    "google_docs": "https://docs.google.com/",
    "google_maps": "https://www.google.com/maps",
    "github": "https://github.com/",
    "x_twitter": "https://x.com/",
    "linkedin": "https://www.linkedin.com/",
    "notion": "https://www.notion.so/",
    "slack_web": "https://app.slack.com/",
    "figma": "https://www.figma.com/",
    "chatgpt": "https://chatgpt.com/",
    "claude": "https://claude.ai/",
    "reddit": "https://www.reddit.com/",
    "wikipedia": "https://www.wikipedia.org/",
    "amazon": "https://www.amazon.com/",
    "netflix": "https://www.netflix.com/",
    "spotify_web": "https://open.spotify.com/",
    "hacker_news": "https://news.ycombinator.com/",
    "stack_overflow": "https://stackoverflow.com/",
    "hugging_face": "https://huggingface.co/",
    "linear": "https://linear.app/",
    "vercel": "https://vercel.com/",
    "livekit": "https://docs.livekit.io/",
    "gradium": "https://gradium.ai/",
    "typesafe": "https://typesafe.ai/",
    "outlook": "https://outlook.live.com/",
    "whatsapp_web": "https://web.whatsapp.com/",
}
SEARCH_URLS = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "gmail": "https://mail.google.com/mail/#search/{q}",
    "google_drive": "https://drive.google.com/drive/search?q={q}",
    "google_maps": "https://www.google.com/maps/search/{q}",
    "github": "https://github.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "x_twitter": "https://x.com/search?q={q}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "stack_overflow": "https://stackoverflow.com/search?q={q}",
    "hacker_news": "https://hn.algolia.com/?q={q}",
    "hugging_face": "https://huggingface.co/search/full-text?q={q}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "spotify_web": "https://open.spotify.com/search/{q}",
}
ALIASES = {k.replace("_", " "): k for k in SITE_URLS}
ALIASES.update(
    {
        "keep": "google_keep",
        "you tube": "youtube",
        "google mail": "gmail",
        "twitter": "x_twitter",
        "x": "x_twitter",
        "whatsapp": "whatsapp_web",
        "slack": "slack_web",
        "spotify": "spotify_web",
        "git hub": "github",
        "stack overflow": "stack_overflow",
        "hacker news": "hacker_news",
        "chat gpt": "chatgpt",
        "drive": "google_drive",
        "maps": "google_maps",
        "calendar": "google_calendar",
    }
)

WINDOWS_APPS = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "browser": "Google Chrome",
    "notepad": "Notepad",
    "note pad": "Notepad",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "calculator": "Calculator",
    "calculatar": "Calculator",
    "calcutlar": "Calculator",
    "calc": "Calculator",
    "paint": "Paint",
    "microsoft paint": "Paint",
    "explorer": "Explorer",
    "file explorer": "Explorer",
    "files": "Explorer",
    "snipping tool": "Snipping Tool",
}
MAC_APPS = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "browser": "Google Chrome",
    "textedit": "TextEdit",
    "text edit": "TextEdit",
    "safari": "Safari",
    "calculator": "Calculator",
    "finder": "Finder",
}
FOLDERS = {name: name for name in ("desktop", "downloads", "documents", "home", "applications")}
FOLDERS.update(
    {
        "download folder": "downloads",
        "downloads folder": "downloads",
        "desktop folder": "desktop",
        "documents folder": "documents",
        "home folder": "home",
    }
)


def app_named(target: str, apps: list[str] | None = None) -> str | None:
    aliases = {a.casefold(): a for a in apps or []}
    aliases.update(WINDOWS_APPS if os_backend.is_windows() else MAC_APPS)
    return aliases.get(re.sub(r"^(?:my|the)\s+", "", target.strip(), flags=re.I).casefold())


def normalize(text: str) -> tuple[str, bool]:
    text = re.sub(r"\s+", " ", text).strip()
    match = WAKE.match(text)
    if match:
        text = text[match.end() :].strip()
    text = re.sub(r"^please\s+|[, ]+please[.!?]*$", "", text, flags=re.I)
    # A wake-only request is a greeting, not a task for the routing model.
    # Match the entire remainder so dictated text and extra instructions survive.
    if match and re.fullmatch(r"wake up[.!?]*", text, re.I):
        text = ""
    # Strip only an explicit request prefix followed by a known command verb.
    # Embedded search/message content must retain the user's literal words.
    text = re.sub(
        r"^(?:can|could|would|will) you\s+(?:please\s+)?(?=(?:open|launch|start|close|quit|exit|minimize|maximize|restore|focus|switch to|bring up|show|take|capture|play|put on|pause|resume|skip|calculate|compute|multiply|divide|add|subtract|go to|navigate to|search|look up|type|write|enter|run)\b)",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^serach\b", "search", text, flags=re.I)
    return text.strip(), bool(match)


def site_named(text: str) -> str | None:
    return ALIASES.get(re.sub(r"^(?:my|the)\s+", "", text.strip().lower()).rstrip(".!?"))


def browser_url(site: str, query: str) -> str:
    query = query.strip()
    # A named site without search terms is its home page, never an empty results page.
    if site in SITE_URLS and (not query or site_named(query) == site):
        return SITE_URLS[site]
    if site in {"other", "google"} and re.match(r"^https?://", query, re.I):
        parsed = urlsplit(query)
        if parsed.hostname and not parsed.username and not parsed.password:
            return query
    if site in {"other", "google"} and re.fullmatch(r"[\w-]+(?:\.[\w-]+)+(?:[/:?#]\S*)?", query):
        return "https://" + query
    template = SEARCH_URLS.get(site, SEARCH_URLS["google"])
    return template.format(q=quote_plus(query))


@dataclass
class Command:
    tool: str = ""
    args: dict[str, str] = field(default_factory=dict)
    reply: str = ""
    missing: str = ""


def parse_command(text: str, apps: list[str] | None = None) -> Command | None:
    text, wake = normalize(text)
    if wake and not text:
        return Command(reply="Hey bro, I'm listening.")
    clean = text.rstrip(".!?")
    typing = re.fullmatch(r"(?:type|write|enter)\s+(.+?)\s+(?:into|in)\s+(.+)", clean, re.I)
    if typing:
        return Command("type_text", {"text": typing[1], "app": typing[2]})
    # Exact reviewed actions only. Additional instructions must never disappear.
    window = re.fullmatch(r"(close|quit|exit|minimize|maximize|restore|focus|switch to)\s+(.+)", clean, re.I)
    if window and (app := app_named(window[2], apps)):
        action = window[1].casefold()
        if action in {"close", "quit", "exit"}:
            return Command("quit_app", {"app": app})
        if os_backend.is_windows() and app in WINDOWS_APPS.values():
            return Command("window_control", {"app": app, "action": "focus" if action == "switch to" else action})
        if os_backend.is_windows() and action in {"focus", "switch to"}:
            return Command("focus_window", {"app": app})
        if action in {"focus", "switch to"}:
            return Command("open_app", {"app": app})
    folder = re.fullmatch(r"(?:open|show|go to)\s+(?:(?:my|the)\s+)?(.+)", clean, re.I)
    if folder and folder[1].casefold() in FOLDERS:
        return Command("finder_open_folder", {"folder": FOLDERS[folder[1].casefold()]})
    if clean.casefold() in {
        "take a screenshot",
        "take screenshot",
        "screenshot",
        "capture the screen",
        "grab the screen",
    }:
        return Command("take_screenshot")
    info = {
        "what time is it": "time",
        "what's the time": "time",
        "tell me the time": "time",
        "what is the time": "time",
        "what's today's date": "date",
        "what is today's date": "date",
        "what day is it": "date",
        "today's date": "date",
        "battery level": "battery",
        "check battery": "battery",
        "how much battery is left": "battery",
    }.get(clean.casefold())
    if info:
        return Command("system_info", {"kind": info})
    media = {
        "play pause": "play_pause",
        "toggle playback": "play_pause",
        "toggle music": "play_pause",
        "next track": "next",
        "next song": "next",
        "skip this song": "next",
        "skip song": "next",
        "previous track": "previous",
        "previous song": "previous",
        "stop music": "stop",
        "stop the music": "stop",
    }.get(clean.casefold())
    if media and os_backend.is_windows():
        return Command("media_control", {"action": media})
    if clean.casefold() in {
        "volume up",
        "turn it up",
        "make it louder",
        "volume down",
        "turn it down",
        "make it quieter",
    }:
        return Command(
            "set_volume",
            {
                "level": "quieter"
                if clean.casefold() in {"volume down", "turn it down", "make it quieter"}
                else "louder"
            },
        )
    if (expression := request_expression(clean)) is not None:
        return Command("calculate", {"expression": expression})
    if (expression := request_expression(clean, incomplete=True)) is not None:
        return Command("calculate", {"expression": expression}, CALCULATION_QUESTION, "expression")
    if clean.lower() in {"search", "search for", "serach"}:
        return Command("chrome_open", {"site": "google", "query": ""}, "What should I search for?", "query")
    # Keep compound tasks in the browser agent, rather than dropping the second instruction.
    if re.search(r"\b(?:google keep|keep)\b", clean, re.I) and re.search(
        r"\b(?:show|read|open)\b.*\b(?:top|first|latest)\s+note\b", clean, re.I
    ):
        return Command("web_task", {"site": "google_keep"})
    if re.search(r"\b(?:and|then)\s+(?:open|show|read|play|click|send|delete|add|find|search)\b", clean, re.I):
        return None
    trending = re.fullmatch(
        r"(?:open|show|go to|navigate to|bring up)\s+(?:the\s+)?(?:"
        r"(?:git\s*hub)(?:'s)?\s+trending(?:\s+page)?|"
        r"trending(?:\s+page)?\s+(?:on|in|of)\s+(?:git\s*hub))",
        clean,
        re.I,
    )
    if trending:
        return Command("chrome_open", {"site": "other", "query": "https://github.com/trending"})
    song = re.fullmatch(r"(?:play|put on)(?:\s+(.+))?", clean, re.I)
    if song:
        query = re.sub(r"\s+(?:on|in)\s+(?:spotify|spotify web|the spotify web player)$", "", song[1] or "", flags=re.I)
        query = re.sub(r"^(?:the song|the track|song|track)\s+", "", query, flags=re.I).strip()
        # Leave explicitly named other players and generic playback to their existing tools.
        if not re.search(
            r"\b(?:on|in)\s+(?:youtube|apple music|amazon music)\b", query, re.I
        ) and query.casefold() not in {"music", "pause", "video", "the music"}:
            if query.casefold() in {"", "spotify", "a song", "a track", "a song on spotify", "something"}:
                return Command("spotify_play", {"query": ""}, "Which song, bro?", "query")
            return Command("spotify_play", {"query": query})
    search = re.fullmatch(r"(?:search|look up|find)\s+(.+)", clean, re.I)
    if search:
        rest = search[1]
        # "search YouTube for cats", "search cats on YouTube", or just "search YouTube".
        for alias in sorted(ALIASES, key=len, reverse=True):
            site = ALIASES[alias]
            if site not in SEARCH_URLS:
                continue
            m = re.fullmatch(rf"(?:on\s+)?{re.escape(alias)}(?:\s+(?:for\s+)?(.+))?", rest, re.I)
            if m:
                query = (m[1] or "").strip()
                if query.lower() == "for":
                    query = ""
                return Command(
                    "chrome_open",
                    {"site": site, "query": query},
                    f"What should I search for on {alias.title()}?" if not query else "",
                    "query" if not query else "",
                )
            m = re.fullmatch(rf"(.+?)\s+on\s+{re.escape(alias)}", rest, re.I)
            if m:
                return Command("chrome_open", {"site": site, "query": re.sub(r"^for\s+", "", m[1], flags=re.I)})
        # Broad 'find' requests may require more than a search page; leave those to the router.
        if clean.lower().startswith("search "):
            return Command("chrome_open", {"site": "google", "query": re.sub(r"^for\s+", "", rest, flags=re.I)})
    opening = re.fullmatch(r"(?:open|launch|start|go to|navigate to|switch to|bring up)\s+(.+)", clean, re.I)
    if opening:
        target = opening[1].strip()
        target = re.sub(r"\b(?:git|get)\s+hub(?=\s+dot|\.)", "github", target, flags=re.I)
        site = site_named(target)
        if site:
            return Command("chrome_open", {"site": site, "query": ""})
        spoken_url = re.sub(r"\s+dot\s+", ".", target, flags=re.I)
        if re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:[/:?#]\S*)?", spoken_url):
            return Command("chrome_open", {"site": "other", "query": spoken_url})
        if app := app_named(target, apps):
            return Command("open_app", {"app": app})
    return None
