import json
from types import SimpleNamespace

import pytest
import websockets.sync.client

from brodi import browser_task, chrome


def test_discovers_chromes_random_port_without_default_endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv("BRODI_CDP_URL", raising=False)
    monkeypatch.setattr(chrome, "LOCAL_STATE", tmp_path / "Local State")
    (tmp_path / "DevToolsActivePort").write_text("54321\n/devtools/browser/test-browser\n")
    connections = []
    socket = object()

    def connect(url, **kwargs):
        connections.append(url)
        return socket

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    connection = browser_task.CDPConnection()
    connection._connect()
    assert connection._ws is socket
    assert connections == ["ws://127.0.0.1:54321/devtools/browser/test-browser"]


def test_explicit_endpoint_has_priority_over_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "LOCAL_STATE", tmp_path / "Local State")
    (tmp_path / "DevToolsActivePort").write_text("54321\n/devtools/browser/wrong-browser\n")
    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9000/devtools/browser/right-browser"}).encode()

    def urlopen(url, **kwargs):
        requested.append(url)
        return Response()

    connected = []
    monkeypatch.setattr(browser_task.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(websockets.sync.client, "connect", lambda url, **kwargs: connected.append(url))
    browser_task.CDPConnection("http://127.0.0.1:9000")._connect()
    assert requested == ["http://127.0.0.1:9000/json/version"]
    assert connected == ["ws://127.0.0.1:9000/devtools/browser/right-browser"]


def test_missing_browser_access_explains_setup(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(browser_task.urllib.request, "urlopen", fail)
    with pytest.raises(browser_task.CDPError, match="chrome://inspect/#remote-debugging"):
        browser_task.CDPConnection("http://127.0.0.1:9000")._connect()


@pytest.mark.parametrize("fails", [False, True])
def test_tab_lookup_closes_connection_even_after_failure(monkeypatch, fails):
    closed = []

    def call(*args):
        if fails:
            raise OSError("disconnected")
        return {"targetInfos": [{"targetId": "tab-1"}]}

    connection = SimpleNamespace(call=call, close=lambda: closed.append(True))
    monkeypatch.setattr(browser_task, "CDPConnection", lambda: connection)
    assert browser_task.tab_exists("tab-1") is not fails
    assert closed == [True]


def test_cdp_ignores_events_but_bounds_total_response_time(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(browser_task.time, "monotonic", lambda: clock[0])

    class Socket:
        def send(self, message):
            pass

        def recv(self, timeout):
            assert 0 < timeout <= 10
            clock[0] += 6
            return json.dumps({"method": "Page.event"})

    connection = browser_task.CDPConnection()
    connection._ws = Socket()
    with pytest.raises(browser_task.CDPError, match="within 10 seconds"):
        connection.call("Target.getTargets")


def test_keep_is_a_direct_browser_destination():
    assert browser_task.start_url_for("google_keep", "show the top note") == "https://keep.google.com/"


def test_missing_access_does_not_open_an_unrelated_probe_tab(monkeypatch):
    launched = []

    def fail(*args, **kwargs):
        raise browser_task.CDPError("not available")

    monkeypatch.setattr(browser_task.subprocess, "run", lambda *args, **kwargs: launched.append(args))
    with pytest.raises(browser_task.CDPError):
        browser_task._resolve_profile_context(SimpleNamespace(call=fail), "Default")
    assert launched == []
