"""Deterministic Spotify Web search/playback through the user's configured Chrome profile."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from . import browser_task, chrome
from .os_backend import ScriptResult

ORIGIN = "https://open.spotify.com"
# Public Spotify track pages, verified against Spotify's own title/artist metadata.
# These skip search for the two stage examples; all other songs use Spotify search.
DEMO_TRACKS = {
    ("mockingbird", "eminem"): "5EZgl8mOj4voJnnzHAjKKs",
    ("godsplan", "drake"): "2VWbHHhWnMzKWPUs4IEEW9",
}


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text).casefold() if c.isalnum())


@dataclass(frozen=True)
class Song:
    title: str
    artist: str = ""

    @property
    def query(self) -> str:
        return self.title + (" " + self.artist if self.artist else "")


def song_query(query: str) -> Song:
    query = re.sub(r"\s+", " ", query).strip().strip('"“”')
    if not query or len(query) > 200:
        raise ValueError("Tell me a song title and, if you know it, the artist.")
    parts = re.split(r"\s+by\s+", query, flags=re.I)
    title, artist = (" by ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (query, "")
    if not title.strip() or (len(parts) > 1 and not artist.strip()):
        raise ValueError("Tell me the song title and artist.")
    artist = {"eninem": "Eminem", "eminem": "Eminem"}.get(artist.casefold(), artist)
    return Song(title.strip('"“” '), artist.strip())


def track_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == urlsplit(ORIGIN).scheme
        and parsed.netloc == urlsplit(ORIGIN).netloc
        and bool(re.fullmatch(r"/(?:intl-[a-z]+/)?track/[A-Za-z0-9]{22}/?", parsed.path))
        and not parsed.username
        and not parsed.password
    )


def matches(song: Song, title: str, artists: list[str]) -> bool:
    return normalized(song.title) == normalized(title) and (
        not song.artist or normalized(song.artist) in {normalized(a) for a in artists}
    )


def choose_track(song: Song, tracks: list[dict]) -> dict | None:
    # Spotify orders search results. Require the complete requested title/artist;
    # never pick a cover, playlist, album, remix, or arbitrary first search card.
    return next(
        (
            track
            for track in tracks
            if track_url(track.get("url", "")) and matches(song, track.get("title", ""), track.get("artists", []))
        ),
        None,
    )


SNAPSHOT_JS = r"""(() => {
  const visible = e => { if (!e) return false; const r=e.getBoundingClientRect();
    const s=getComputedStyle(e); return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; };
  const artists = e => Array.from(e?.querySelectorAll('a[href*="/artist/"]') || []).map(a=>a.textContent.trim()).filter(Boolean);
  const main = document.querySelector('main') || document.querySelector('[role="main"]');
  const rows = Array.from(main?.querySelectorAll('[data-testid="tracklist-row"], [role="row"]') || []);
  const tracks = rows.map(row => {
    const link = row.querySelector('a[data-testid="internal-track-link"], a[href*="/track/"]');
    return link ? {url:link.href, title:link.textContent.trim(), artists:artists(row)} : null;
  }).filter(Boolean).slice(0,40);
  const footer = document.querySelector('[data-testid="now-playing-widget"]') || document.querySelector('footer');
  const track = footer?.querySelector('a[data-testid="context-item-link"], a[href*="/track/"]');
  const control = document.querySelector('[data-testid="control-button-playpause"]');
  const metadata = navigator.mediaSession?.metadata;
  const login = document.querySelector('[data-testid="login-button"], [data-testid="login-button-large"]');
  const loginDialog = Array.from(document.querySelectorAll('[role="dialog"]')).some(e=>visible(e) && /log in|sign in|sign up/i.test(e.textContent));
  return {url:location.href, title:document.title, ready:document.readyState,
    heading: main?.querySelector('h1')?.textContent.trim() || '', artists:artists(main), tracks,
    logged_out:visible(login) || loginDialog,
    blocked: Array.from(document.querySelectorAll('[role="alert"], [data-testid="toast"]')).filter(visible).map(e=>e.textContent.trim()).join(' ').slice(0,400),
    player:{url:track?.href || '', title:track?.textContent.trim() || metadata?.title || '',
      artists:artists(footer).length ? artists(footer) : [metadata?.artist || ''],
      playing: !!control && (/pause/i.test(control.getAttribute('aria-label') || '') || navigator.mediaSession?.playbackState==='playing'),
      position:document.querySelector('[data-testid="playback-position"]')?.textContent || ''}};
})()"""


def _click_js(song: Song) -> str:
    # Recheck identity in the same JS turn as the click, so navigation cannot swap
    # the requested song for another page between observation and action.
    return (
        r"""(() => {
      const expected = """
        + json.dumps({"origin": ORIGIN, "title": song.title, "artist": song.artist})
        + r""";
      const norm = s => (s||'').normalize('NFKD').toLowerCase().replace(/[^\p{L}\p{N}]/gu,'');
      if (location.origin !== expected.origin) return {ok:false,error:'Spotify page changed'};
      const main=document.querySelector('main') || document.querySelector('[role="main"]');
      if (norm(main?.querySelector('h1')?.textContent)!==norm(expected.title)) return {ok:false,error:'Track title changed'};
      const artists=Array.from(main.querySelectorAll('a[href*="/artist/"]')).map(e=>norm(e.textContent));
      if (expected.artist && !artists.includes(norm(expected.artist))) return {ok:false,error:'Track artist changed'};
      const buttons=Array.from(main.querySelectorAll('[data-testid="play-button"]')).filter(e=>{
        const r=e.getBoundingClientRect();return r.width>0 && r.height>0 && !e.disabled && e.getAttribute('aria-disabled')!=='true';
      });
      if(buttons.length!==1) return {ok:false,error:'Spotify play control is not ready'};
      const button=buttons[0], r=button.getBoundingClientRect(), top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(!top || !(top===button || button.contains(top))) return {ok:false,error:'A dialog is covering Spotify playback'};
      button.click(); return {ok:true};
    })()"""
    )


class SpotifyPlayer:
    def __init__(self):
        self.target_id: str | None = None

    def play_sync(
        self, query: str, *, cdp=None, timeout: float = 20, stop: threading.Event | None = None
    ) -> ScriptResult:
        try:
            song = song_query(query)
        except ValueError as exc:
            return ScriptResult(False, "", str(exc))
        deadline = time.monotonic() + timeout
        stop = stop or threading.Event()
        own = cdp is None
        # Fail setup promptly instead of consuming the whole playback budget.
        connection = cdp or browser_task.CDPConnection(deadline=min(deadline, time.monotonic() + 5))
        session = None
        known = DEMO_TRACKS.get((normalized(song.title), normalized(song.artist)))
        url = f"{ORIGIN}/track/{known}" if known else f"{ORIGIN}/search/{quote(song.query, safe='')}/tracks"

        def check():
            if stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Spotify took too long. Check its player before trying again.")

        def call(method, **kwargs):
            check()
            return connection.call(method, **kwargs)

        def evaluate(source, *, gesture=False):
            result = call(
                "Runtime.evaluate", session_id=session, expression=source, returnByValue=True, userGesture=gesture
            )
            if result.get("exceptionDetails"):
                raise RuntimeError("Spotify's page could not be read")
            return result.get("result", {}).get("value") or {}

        def snapshot():
            state = evaluate(SNAPSHOT_JS)
            if state.get("url") == "about:blank":
                return {}  # Navigation has not committed yet; no actions here.
            parsed = urlsplit(state.get("url", ""))
            if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
                raise RuntimeError("Spotify navigated away. Open its web player and try again.")
            return state

        try:
            targets = call("Target.getTargets").get("targetInfos", [])
            if own:
                connection.deadline = deadline
            existing = next(
                (
                    t
                    for t in targets
                    if t.get("targetId") == self.target_id
                    and t.get("type") == "page"
                    and t.get("url", "").startswith(ORIGIN + "/")
                ),
                None,
            )
            if existing is None:
                params = {"url": "about:blank", "background": False}
                if own:
                    context = browser_task._resolve_profile_context(connection, chrome.profile_dir(), strict=True)
                    # A verified default profile legitimately has no browserContextId.
                    if context:
                        params["browserContextId"] = context
                self.target_id = call("Target.createTarget", **params)["targetId"]
            session = call("Target.attachToTarget", targetId=self.target_id, flatten=True)["sessionId"]
            call("Page.enable", session_id=session)
            call("Page.navigate", session_id=session, url=url)
            call("Target.activateTarget", targetId=self.target_id)
            selected_url = url if known else ""
            clicked = False
            while True:
                check()
                state = snapshot()
                if state.get("logged_out"):
                    return ScriptResult(False, "", "Spotify is open. Sign in there, then ask me to play it again.")
                if not selected_url:
                    track = choose_track(song, state.get("tracks", []))
                    if track:
                        selected_url = track["url"]
                        call("Page.navigate", session_id=session, url=selected_url)
                        continue
                elif matches(song, state.get("heading", ""), state.get("artists", [])):
                    player = state.get("player", {})
                    if player.get("playing") and matches(song, player.get("title", ""), player.get("artists", [])):
                        if (
                            track_url(player.get("url", ""))
                            and urlsplit(player["url"]).path.rstrip("/").split("/")[-1]
                            == urlsplit(selected_url).path.rstrip("/").split("/")[-1]
                        ):
                            return ScriptResult(
                                True,
                                json.dumps(
                                    {
                                        "title": player["title"],
                                        "artists": player["artists"],
                                        "url": player["url"],
                                        "playing": True,
                                        "verified": True,
                                        "target_id": self.target_id,
                                    }
                                ),
                            )
                    if not clicked:
                        outcome = evaluate(_click_js(song), gesture=True)
                        if outcome.get("ok"):
                            clicked = True
                        elif "dialog" in outcome.get("error", "").lower():
                            return ScriptResult(False, "", outcome["error"] + ". Dismiss it and try again.")
                if clicked and state.get("blocked"):
                    return ScriptResult(False, "", "Spotify could not start playback: " + state["blocked"])
                stop.wait(0.15)
        except browser_task.CDPError as exc:
            if own and session is None and not stop.is_set() and time.monotonic() < deadline:
                try:
                    subprocess.Popen(
                        chrome.launch_chrome_command(url, chrome.profile_dir()),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return ScriptResult(False, "", "I opened Spotify. " + str(exc))
                except OSError:
                    pass
            return ScriptResult(False, "", str(exc))
        except (ValueError, OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as exc:
            return ScriptResult(False, "", str(exc))
        finally:
            if session and not stop.is_set() and time.monotonic() < deadline:
                try:
                    connection.call("Target.detachFromTarget", sessionId=session)
                except Exception:
                    pass
            if own:
                connection.close()

    async def play(self, query: str, *, timeout: float = 20) -> ScriptResult:
        stop = threading.Event()
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.play_sync, query, timeout=timeout, stop=stop), timeout)
        except TimeoutError:
            return ScriptResult(False, "", "Spotify took too long. Check its player before trying again.")
        finally:
            stop.set()  # A cancelled worker must never click Play later.
