"""Real DOM/input tests. Opt in with BRODI_CHROME_TESTS=1; never use the user's profile."""

import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from brodi import browser_task, chrome
from brodi.decisions import ChoiceAnswer, DecisionResponse

pytestmark = [
    pytest.mark.chrome,
    pytest.mark.skipif(
        os.environ.get("BRODI_CHROME_TESTS") != "1", reason="set BRODI_CHROME_TESTS=1 for isolated real Chrome tests"
    ),
]

HTML = b"""<!doctype html><title>Brodi browser test</title>
<style>body{font:20px sans-serif;padding:30px}button,input,[role=listitem]{margin:12px;padding:12px}</style>
<form onsubmit="event.preventDefault(); document.getElementById('results').textContent='Results for '+document.getElementById('query').value">
<label for="query">Search topic</label><input id="query" value="old text" onclick="document.getElementById('count').textContent=++window.inputClicks">
<button>Search</button></form><p>Input clicks: <span id="count">0</span></p><section id="results"></section>
<label for="disabled">Disabled field</label><input id="disabled" disabled>
<label><input type="checkbox" id="check">Favourite</label>
<div role="listitem" tabindex="0" onclick="document.getElementById('note').showModal()">First note: Shopping</div>
<dialog id="note"><h2>Shopping</h2><p>Buy apples and bread</p><button onclick="this.parentElement.close()">Close note</button></dialog>
<script>window.inputClicks=0</script>"""


@pytest.fixture(scope="module")
def real_browser(tmp_path_factory):
    executable = chrome.find_chrome_executable()
    if not Path(executable).is_file():
        pytest.fail("Chrome was requested for integration tests but is not installed")
    profile = tmp_path_factory.mktemp("brodi-chrome-qa")
    command = [
        executable,
        "--headless",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-background-networking",
        "about:blank",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    connection = None
    try:
        deadline = time.monotonic() + 15
        discovery = profile / "DevToolsActivePort"
        while time.monotonic() < deadline:
            if discovery.is_file():
                port = int(discovery.read_text().splitlines()[0])
                connection = browser_task.CDPConnection(f"http://127.0.0.1:{port}")
                connection.call("Browser.getVersion")
                break
            if process.poll() is not None:
                pytest.fail(f"Isolated Chrome exited with {process.returncode}")
            time.sleep(0.1)
        else:
            pytest.fail("Isolated Chrome did not publish its debugging endpoint")
        yield connection
    finally:
        if connection:
            try:
                connection.call("Browser.close")
            except Exception:
                pass
            connection.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def fixture_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class BrowserSteps:
    def __init__(self, steps):
        self.steps = iter(steps)

    async def evaluate(self, state, questions):
        operation, target_label = next(self.steps)
        choices = {"operation": ChoiceAnswer(operation, 1, {operation: 1})}
        if target_label:
            criteria = questions["target"].criteria
            target = next(key for key, value in criteria.items() if target_label in value)
            choices["target"] = ChoiceAnswer(target, 1, {target: 1})
        return DecisionResponse(choices, {})


def test_real_search_replaces_text_clicks_once_and_submits(real_browser, fixture_url, monkeypatch):
    monkeypatch.setattr(browser_task, "_field_text_sync", lambda context: ("cats and dogs", {}))
    result = browser_task.run_task_sync(
        fixture_url,
        "Search for cats and dogs",
        cdp=real_browser,
        decisions=BrowserSteps([("fill", "Search topic"), ("click", "click | button | Search"), ("done", "")]),
    )
    try:
        assert result.status == "done", result.error
        assert "Results for cats and dogs" in result.page_text
        assert "Input clicks: 1" in result.page_text
        assert result.steps == 3
    finally:
        if result.target_id:
            real_browser.call("Target.closeTarget", targetId=result.target_id)


def test_real_note_card_opens_its_contents(real_browser, fixture_url):
    result = browser_task.run_task_sync(
        fixture_url,
        "Show the first note",
        cdp=real_browser,
        decisions=BrowserSteps([("click", "First note: Shopping"), ("done", "")]),
    )
    try:
        assert result.status == "done", result.error
        assert "Buy apples and bread" in result.page_text
        assert "Close note" in result.page_text
    finally:
        if result.target_id:
            real_browser.call("Target.closeTarget", targetId=result.target_id)


def test_real_followup_reuses_tab_without_repeating_completed_steps(real_browser, fixture_url):
    first = browser_task.run_task_sync(
        fixture_url,
        "Open the first note",
        cdp=real_browser,
        decisions=BrowserSteps([("click", "First note: Shopping"), ("done", "")]),
    )
    try:
        assert first.status == "done" and "Buy apples and bread" in first.page_text
        second = browser_task.run_task_sync(
            "",
            "Close that note",
            reuse_target=first.target_id,
            cdp=real_browser,
            decisions=BrowserSteps([("click", "Close note"), ("done", "")]),
        )
        assert second.status == "done", second.error
        assert second.target_id == first.target_id
        assert "Buy apples and bread" not in second.page_text
    finally:
        if first.target_id:
            real_browser.call("Target.closeTarget", targetId=first.target_id)


def test_real_observation_excludes_disabled_fields_and_clicks_checkboxes(real_browser, fixture_url):
    target = real_browser.call("Target.createTarget", url=fixture_url)["targetId"]
    session = real_browser.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    try:
        deadline = time.monotonic() + 5
        while True:
            page = browser_task.observe(real_browser, session)
            if page.get("ready") == "complete" and "Search topic" in page.get("text", ""):
                break
            assert time.monotonic() < deadline, "Fixture page did not load"
            time.sleep(0.05)
        assert not any(a["label"] == "Disabled field" for a in page["actions"])
        checkbox = next(a for a in page["actions"] if a["label"] == "Favourite")
        assert checkbox["kind"] == "click"
        browser_task._execute(real_browser, session, "click", checkbox, "Mark favourite")
        checked = real_browser.call(
            "Runtime.evaluate",
            session_id=session,
            expression="document.getElementById('check').checked",
            returnByValue=True,
        )
        assert checked["result"]["value"] is True
    finally:
        real_browser.call("Target.closeTarget", targetId=target)
