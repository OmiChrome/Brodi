"""Real CDP + DOM playback state fixture; no Spotify account or real audio involved."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_chrome_integration import real_browser as real_browser

from brodi import spotify

pytestmark = [
    pytest.mark.chrome,
    pytest.mark.skipif(os.environ.get("BRODI_CHROME_TESTS") != "1", reason="set BRODI_CHROME_TESTS=1"),
]

TRACK_ID = "x" * 22


@pytest.fixture
def spotify_fixture(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path.startswith("/search/"):
                content = f"""<main><h1>Search</h1><div data-testid="tracklist-row">
                <a href="/track/{TRACK_ID}">Hello</a><a href="/artist/adele">Adele</a></div>
                <div data-testid="tracklist-row"><a href="/track/{"y" * 22}">Hello</a><a href="/artist/other">Other</a></div></main>"""
            else:
                content = f"""<main><h1>Hello</h1><a href="/artist/adele">Adele</a>
                <button data-testid="play-button" onclick="window.clicks++;document.getElementById('player').hidden=false;document.getElementById('pause').setAttribute('aria-label','Pause')">Play</button></main>
                <footer id="player" hidden><a data-testid="context-item-link" href="/track/{TRACK_ID}">Hello</a>
                <a href="/artist/adele">Adele</a><button id="pause" data-testid="control-button-playpause" aria-label="Play">Pause</button>
                <span data-testid="playback-position">0:01</span></footer><script>window.clicks=0</script>"""
            self.wfile.write(
                (
                    "<!doctype html><title>Playback fixture</title><style>button{margin:30px;padding:20px}</style>"
                    + content
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(spotify, "ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_real_search_click_and_player_verification(real_browser, spotify_fixture):
    player = spotify.SpotifyPlayer()
    try:
        result = player.play_sync("Hello by Adele", cdp=real_browser, timeout=8)
        assert result.ok, result.error
        data = json.loads(result.output)
        assert data["verified"] and data["playing"] and data["title"] == "Hello"
        assert data["url"].endswith(TRACK_ID)
        session = real_browser.call("Target.attachToTarget", targetId=player.target_id, flatten=True)["sessionId"]
        response = real_browser.call(
            "Runtime.evaluate", session_id=session, expression="window.clicks", returnByValue=True
        )
        assert response["result"]["value"] == 1
        real_browser.call("Target.detachFromTarget", sessionId=session)
    finally:
        if player.target_id:
            real_browser.call("Target.closeTarget", targetId=player.target_id)
