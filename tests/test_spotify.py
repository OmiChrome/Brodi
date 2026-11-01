import asyncio
import json
import threading
import time

import pytest

from brodi import browser_task, spotify
from brodi.agent import DynamicMacAgent
from brodi.applescript import MacContext
from brodi.commands import browser_url, parse_command
from brodi.memory import MemoryStore
from brodi.os_backend import ScriptResult
from brodi.registry import ToolRegistry


@pytest.mark.parametrize(
    "phrase,query",
    [
        ("hey bro play mocking bird by eninem", "mocking bird by eninem"),
        ("play God's Plan by Drake on Spotify", "God's Plan by Drake"),
        ("could you play Blinding Lights by The Weeknd", "Blinding Lights by The Weeknd"),
        ("put on the song Hello by Adele", "Hello by Adele"),
    ],
)
def test_direct_song_commands(phrase, query):
    command = parse_command(phrase)
    assert (command.tool, command.args) == ("spotify_play", {"query": query})


def test_missing_song_and_other_players_are_not_swallowed():
    assert parse_command("play a song on Spotify").missing == "query"
    assert parse_command("play Mockingbird on YouTube") is None
    assert parse_command("play Mockingbird and then open Notepad") is None


@pytest.mark.parametrize(
    "phrase",
    [
        "open trending page in github",
        "hey bro open GitHub trending",
        "show the trending page on Git Hub",
        "go to GitHub's trending page",
        "could you open github trending",
    ],
)
def test_github_trending_is_a_direct_navigation(phrase):
    command = parse_command(phrase)
    assert command.tool == "chrome_open"
    assert browser_url(**command.args) == "https://github.com/trending"


def test_exact_artist_title_and_origin_are_required():
    song = spotify.song_query("mocking bird by eninem")
    candidates = [
        {"title": "Mockingbird", "artists": ["Cover Artist"], "url": spotify.ORIGIN + "/track/" + "a" * 22},
        {"title": "Mockingbird Remix", "artists": ["Eminem"], "url": spotify.ORIGIN + "/track/" + "b" * 22},
        {"title": "Mockingbird", "artists": ["Eminem"], "url": "https://evil.test/track/" + "c" * 22},
        {"title": "Mockingbird", "artists": ["Eminem"], "url": spotify.ORIGIN + "/track/" + "d" * 22},
    ]
    assert spotify.choose_track(song, candidates) == candidates[-1]
    assert not spotify.track_url("https://open.spotify.com.evil.test/track/" + "d" * 22)
    assert not spotify.track_url("https://user@open.spotify.com/track/" + "d" * 22)


class CDP:
    def __init__(self, *, logged_out=False, wrong_song=False, unrelated_origin=False):
        self.calls = []
        self.url = "about:blank"
        self.clicked = 0
        self.logged_out = logged_out
        self.wrong_song = wrong_song
        self.unrelated_origin = unrelated_origin

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "owned", "type": "page", "url": self.url}]}
        if method == "Target.createTarget":
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Page.navigate":
            self.url = params["url"]
        if method == "Runtime.evaluate":
            if params["expression"] != spotify.SNAPSHOT_JS:
                self.clicked += 1
                assert params["userGesture"] is True
                return {"result": {"value": {"ok": True}}}
            state = {
                "url": "https://evil.test/" if self.unrelated_origin else self.url,
                "heading": "Mockingbird",
                "artists": ["Eminem"],
                "logged_out": self.logged_out,
                "player": {
                    "title": "Another song" if self.wrong_song else "Mockingbird",
                    "artists": ["Eminem"],
                    "playing": bool(self.clicked),
                    "url": self.url,
                },
            }
            return {"result": {"value": state}}
        return {}


def test_click_once_verify_and_reuse_only_owned_tab():
    cdp = CDP()
    player = spotify.SpotifyPlayer()
    result = player.play_sync("Mockingbird by Eminem", cdp=cdp, timeout=1)
    assert result.ok, result.error
    assert json.loads(result.output)["verified"] is True
    assert cdp.clicked == 1
    assert player.play_sync("Mockingbird by Eminem", cdp=cdp, timeout=1).ok
    assert cdp.clicked == 1  # Already playing the requested song, don't toggle pause.
    assert sum(method == "Target.createTarget" for method, _ in cdp.calls) == 1


@pytest.mark.parametrize(
    "kwargs,clicks", [({"logged_out": True}, 0), ({"wrong_song": True}, 1), ({"unrelated_origin": True}, 0)]
)
def test_does_not_report_success_or_repeatedly_click_when_playback_fails(kwargs, clicks):
    cdp = CDP(**kwargs)
    result = spotify.SpotifyPlayer().play_sync("Mockingbird by Eminem", cdp=cdp, timeout=0.2)
    assert not result.ok and result.error
    assert cdp.clicked == clicks


def test_cancelled_worker_never_starts_an_action():
    stop = threading.Event()
    stop.set()
    cdp = CDP()
    result = spotify.SpotifyPlayer().play_sync("Mockingbird", cdp=cdp, stop=stop)
    assert not result.ok and not cdp.calls


def test_expired_cdp_deadline_does_not_connect(monkeypatch):
    cdp = browser_task.CDPConnection(deadline=time.monotonic() - 1)
    monkeypatch.setattr(cdp, "_connect", lambda: pytest.fail("must not connect after timeout"))
    with pytest.raises(browser_task.CDPError, match="timed out"):
        cdp.call("Page.navigate", url="https://open.spotify.com")


def test_verified_default_profile_without_context_id_is_supported(monkeypatch):
    cdp = CDP()
    cdp.close = lambda: None
    monkeypatch.setattr(browser_task, "CDPConnection", lambda **kwargs: cdp)
    monkeypatch.setattr(browser_task, "_resolve_profile_context", lambda *args, **kwargs: None)
    result = spotify.SpotifyPlayer().play_sync("Mockingbird by Eminem", timeout=1)
    assert result.ok, result.error
    creation = next(params for method, params in cdp.calls if method == "Target.createTarget")
    assert "browserContextId" not in creation


def test_missing_browser_access_opens_the_song_but_reports_failure(monkeypatch):
    opened = []

    class Unavailable:
        def __init__(self, **kwargs):
            pass

        def call(self, *args, **kwargs):
            raise browser_task.CDPError("Allow browser access first.")

        def close(self):
            pass

    monkeypatch.setattr(browser_task, "CDPConnection", Unavailable)
    monkeypatch.setattr(spotify.subprocess, "Popen", lambda command, **kwargs: opened.append(command))
    result = spotify.SpotifyPlayer().play_sync("Mockingbird by Eminem")
    assert not result.ok and "Allow browser access" in result.error
    assert len(opened) == 1 and any("open.spotify.com/track/" in arg for arg in opened[0])


def test_command_reaches_player_without_model_and_failed_playback_is_not_learned(monkeypatch, tmp_path):
    registry = ToolRegistry(learned_path=tmp_path / "tools.json")
    agent = DynamicMacAgent(registry, enable_learning=False, memory=MemoryStore(tmp_path / "memory.db"))
    agent.context._ctx = MacContext("Chrome", ["Google Chrome"], [])
    calls = []

    async def play(self, query, **kwargs):
        calls.append(query)
        if "Adele" in query:
            return ScriptResult(False, "", "Sign in to Spotify.")
        return ScriptResult(True, '{"playing":true,"verified":true}')

    async def forbidden(*args, **kwargs):
        pytest.fail("Ready-made music requests must not call the model")

    monkeypatch.setattr(spotify.SpotifyPlayer, "play", play)
    monkeypatch.setattr(agent.router.client, "evaluate", forbidden)

    async def scenario():
        for _ in range(2):
            result = await agent.handle("play God's Plan by Drake on Spotify")
            assert result.executed and result.speak == "Got it."
        result = await agent.handle("play Hello by Adele")
        assert not result.executed and "Sign in" in result.speak
        assert not agent.memory.match_shortcut("play Hello by Adele")
        await agent.aclose()

    asyncio.run(scenario())
    assert len(calls) == 3


def test_learned_tools_cannot_impersonate_spotify_runner(tmp_path):
    tool = ToolRegistry(learned_path=tmp_path / "tools.json").get("spotify_play")
    tool.source = "learned"
    assert not tool.is_available(MacContext("Chrome", [], []))
