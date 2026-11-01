"""Small, exact voice commands that don't need a network round trip to understand."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlsplit

from . import os_backend

WAKE = re.compile(r"^(?:(?:hey|yo|hello|hi|okay|ok)[\s,]+(?:brodi|bro|brody)|brodi|brody)\b[\s,!.:;?-]*", re.I)
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
    }
)


def normalize(text: str) -> tuple[str, bool]:
    text = re.sub(r"\s+", " ", text).strip()
    match = WAKE.match(text)
    if match:
        text = text[match.end() :].strip()
    text = re.sub(r"^please\s+|[, ]+please[.!?]*$", "", text, flags=re.I)
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
    if clean.lower() in {"search", "search for", "serach"}:
        return Command("chrome_open", {"site": "google", "query": ""}, "What should I search for?", "query")
    # Keep compound tasks in the browser agent, rather than dropping the second instruction.
    if re.search(r"\b(?:google keep|keep)\b", clean, re.I) and re.search(
        r"\b(?:show|read|open)\b.*\b(?:top|first|latest)\s+note\b", clean, re.I
    ):
        return Command("web_task", {"site": "google_keep"})
    if re.search(r"\b(?:and|then)\s+(?:open|show|read|play|click|send|delete|add|find|search)\b", clean, re.I):
        return None
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
        site = site_named(target)
        if site:
            return Command("chrome_open", {"site": site, "query": ""})
        spoken_url = re.sub(r"\s+dot\s+", ".", target, flags=re.I)
        if re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:[/:?#]\S*)?", spoken_url):
            return Command("chrome_open", {"site": "other", "query": spoken_url})
        app_aliases = (
            {
                "chrome": "Google Chrome",
                "google chrome": "Google Chrome",
                "notepad": "Notepad",
                "edge": "Microsoft Edge",
                "microsoft edge": "Microsoft Edge",
            }
            if os_backend.is_windows()
            else {
                "chrome": "Google Chrome",
                "google chrome": "Google Chrome",
                "textedit": "TextEdit",
                "safari": "Safari",
            }
        )
        app_aliases.update({a.lower(): a for a in apps or []})
        if target.lower() in app_aliases:
            return Command("open_app", {"app": app_aliases[target.lower()]})
    return None
