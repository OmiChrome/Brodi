"""Public demo phrases traverse the actual router, policy and native dispatch."""

import asyncio
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brodi import commands, native_launch, quick_actions, windows_desktop
from brodi.agent import DynamicMacAgent, State
from brodi.applescript import MacContext, ScriptResult
from brodi.registry import ToolRegistry

CASES = [
    ("Hey bro, close Notepad", "quit_app", {"app": "Notepad"}),
    ("Could you quit Chrome please?", "quit_app", {"app": "Google Chrome"}),
    ("exit Paint", "quit_app", {"app": "Paint"}),
    ("open calculator", "open_app", {"app": "Calculator"}),
    ("launch paint", "open_app", {"app": "Paint"}),
    ("open file explorer", "open_app", {"app": "Explorer"}),
    ("open snipping tool", "open_app", {"app": "Snipping Tool"}),
    ("open note pad", "open_app", {"app": "Notepad"}),
    ("maximize Notepad", "window_control", {"app": "Notepad", "action": "maximize"}),
    ("minimize Chrome", "window_control", {"app": "Google Chrome", "action": "minimize"}),
    ("restore Notepad", "window_control", {"app": "Notepad", "action": "restore"}),
    ("switch to Notepad", "window_control", {"app": "Notepad", "action": "focus"}),
    ("focus Paint", "window_control", {"app": "Paint", "action": "focus"}),
    ("open my downloads", "finder_open_folder", {"folder": "downloads"}),
    ("show the documents folder", "finder_open_folder", {"folder": "documents"}),
    ("open desktop", "finder_open_folder", {"folder": "desktop"}),
    ("open my home folder", "finder_open_folder", {"folder": "home"}),
    ("take a screenshot", "take_screenshot", {}),
    ("next song", "media_control", {"action": "next"}),
    ("previous song", "media_control", {"action": "previous"}),
    ("toggle playback", "media_control", {"action": "play_pause"}),
    ("stop music", "media_control", {"action": "stop"}),
    ("volume up", "set_volume", {"level": "louder"}),
    ("turn it down", "set_volume", {"level": "quieter"}),
    ("what time is it?", "system_info", {"kind": "time"}),
    ("what is today's date?", "system_info", {"kind": "date"}),
    ("check battery", "system_info", {"kind": "battery"}),
    ("calculate 23 times 7", "calculate", {"expression": "23 * 7"}),
    ("open git hub", "chrome_open", {"site": "github", "query": ""}),
    ("open chat gpt", "chrome_open", {"site": "chatgpt", "query": ""}),
    ("open drive", "chrome_open", {"site": "google_drive", "query": ""}),
    ("search GitHub for voice agents", "chrome_open", {"site": "github", "query": "voice agents"}),
    (
        "search Wikipedia for artificial intelligence",
        "chrome_open",
        {"site": "wikipedia", "query": "artificial intelligence"},
    ),
]


def test_every_advertised_demo_phrase_resolves_without_a_model(monkeypatch, tmp_path):
    from brodi.demo_catalog import QUICK_DEMOS

    monkeypatch.setenv("BRODI_OS", "win32")
    registry = ToolRegistry(learned_path=tmp_path / "learned.json")
    for category in QUICK_DEMOS.values():
        for phrase in category:
            command = commands.parse_command(phrase)
            assert command and command.tool and not command.missing, phrase
            assert registry.get(command.tool).is_available(MacContext("Explorer", [], [])), phrase


@pytest.mark.parametrize("phrase,tool,args", CASES)
def test_demo_phrases_execute_without_a_model(phrase, tool, args, monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    monkeypatch.setenv("BRODI_FAST_LAUNCH", "true")
    assistant = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    # Recognition of standard apps must not depend on the one-second context poller.
    assistant.context._ctx = MacContext("Explorer", [], [])
    called = []

    async def native(name, values):
        called.append((name, values))
        return ScriptResult(True, "Accepted")

    async def no_network(*args, **kwargs):
        raise AssertionError("A ready-made command must skip model routing")

    monkeypatch.setattr(native_launch, "execute", native)
    monkeypatch.setattr(assistant.router.client, "evaluate", no_network)

    async def scenario():
        try:
            result = await assistant.handle(phrase)
            assert result.executed, result.speak
            assert result.route.tool.name == tool
            assert result.route.args == args
            assert assistant.state is State.IDLE
            if tool in {"calculate", "system_info"}:
                assert not called
            else:
                assert called == [(tool, args)]
        finally:
            await assistant.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "phrase",
    [
        "close Notepad and delete my files",
        "close every app",
        "force kill Notepad",
        "open calculator.exe; evil",
        "minimize Notepad then send the note",
        "next song and send a message",
        "calculate __import__('os')",
        "open my downloads and delete everything",
        "open notepad and close chrome",
    ],
)
def test_fast_parser_never_discards_extra_instructions(phrase, monkeypatch):
    monkeypatch.setenv("BRODI_OS", "win32")
    assert commands.parse_command(phrase) is None


def test_compound_window_actions_remain_a_bounded_plan(monkeypatch):
    from brodi.multi_step import deterministic_plan

    monkeypatch.setenv("BRODI_OS", "win32")
    plan = deterministic_plan("minimize Notepad and maximize Chrome", [])
    assert [step.args["action"] for step in plan.steps] == ["minimize", "maximize"]


def test_mac_app_commands_and_windows_only_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "darwin")
    assert commands.parse_command("close TextEdit").args == {"app": "TextEdit"}
    assert commands.parse_command("switch to Safari").tool == "open_app"
    assert commands.parse_command("minimize Notepad") is None
    registry = ToolRegistry(learned_path=tmp_path / "learned.json")
    assert not registry.get("window_control").is_available(MacContext("Finder", [], []))
    assert not registry.get("media_control").is_available(MacContext("Finder", [], []))


@pytest.mark.parametrize("expression,answer", [("23 * 7", "161"), ("(100 - 5) / 5", "19"), ("-4 + 0.5", "-3.5")])
def test_arithmetic_is_deterministic(expression, answer):
    assert quick_actions.run("calculate", {"expression": expression}).output == answer


@pytest.mark.parametrize("expression", ["1/0", "2**1000000", "1e999", "True+1", "abs(-1)", "x", "9" * 81])
def test_arithmetic_rejects_code_and_unbounded_numbers(expression):
    assert not quick_actions.run("calculate", {"expression": expression}).ok


def test_close_selects_foreground_or_fails_on_ambiguity():
    a, b = windows_desktop.Window(100, 42, "a"), windows_desktop.Window(101, 43, "b")
    assert windows_desktop.choose_window([a, b], 101) == b
    assert windows_desktop.choose_window([a], 999) == a
    with pytest.raises(ValueError, match="Several windows"):
        windows_desktop.choose_window([a, b], 999)
    with pytest.raises(ValueError, match="no open window"):
        windows_desktop.choose_window([], 999)


def test_close_posts_wm_close_and_never_terminates(monkeypatch):
    window = windows_desktop.Window(123, 42, "Notes")
    api = Mock()
    api.GetWindowThreadProcessId.side_effect = lambda hwnd, pointer: setattr(pointer._obj, "value", 42)
    api.GetForegroundWindow.return_value = 123
    monkeypatch.setattr(windows_desktop, "user32", lambda: api)
    monkeypatch.setattr(windows_desktop, "windows", lambda app: [window])
    monkeypatch.setattr(windows_desktop, "_process_matches", lambda pid, app: True)
    result = windows_desktop.run("quit_app", {"app": "Notepad"})
    assert result.ok and "requested" in result.output and "unsaved" in result.output
    api.PostMessageW.assert_called_once_with(123, 0x0010, 0, 0)


def test_reused_window_handle_fails_closed(monkeypatch):
    api = Mock()
    api.GetWindowThreadProcessId.side_effect = lambda hwnd, pointer: setattr(pointer._obj, "value", 99)
    monkeypatch.setattr(windows_desktop, "user32", lambda: api)
    monkeypatch.setattr(windows_desktop, "windows", lambda app: [windows_desktop.Window(123, 42, "Notes")])
    assert not windows_desktop.run("quit_app", {"app": "Notepad"}).ok
    api.PostMessageW.assert_not_called()


def test_uwp_window_requires_matching_child_executable(monkeypatch):
    api = Mock()
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(windows_desktop, "user32", lambda: api)
    monkeypatch.setattr(
        windows_desktop.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "ApplicationFrameHost.exe")
    )
    monkeypatch.setattr(windows_desktop, "_process_matches", lambda pid, app: pid == 42 and app == "Calculator")
    api.GetWindowThreadProcessId.side_effect = lambda hwnd, pointer: setattr(pointer._obj, "value", 42)
    api.EnumChildWindows.side_effect = lambda hwnd, callback, param: callback(101, 0)
    assert windows_desktop._window_matches(100, 999, "Calculator")
    assert not windows_desktop._window_matches(100, 999, "Notepad")


def test_media_input_balances_key_down_and_up_and_reports_rejection(monkeypatch):
    api = Mock()
    monkeypatch.setattr(windows_desktop, "user32", lambda: api)
    captured = []

    def send(count, events, size):
        captured.extend((event.type, event.payload.keyboard.vk, event.payload.keyboard.flags) for event in events)
        assert size == ctypes.sizeof(events[0])
        return count

    api.SendInput.side_effect = send
    assert windows_desktop.run("media_control", {"action": "next"}).ok
    assert captured == [(1, 0xB0, 0), (1, 0xB0, 2)]
    assert not windows_desktop.run("media_control", {"action": "arbitrary"}).ok
    api.SendInput.side_effect = None
    api.SendInput.return_value = 0
    assert not windows_desktop.run("media_control", {"action": "next"}).ok


def test_screenshots_never_overwrite_an_existing_file(monkeypatch, tmp_path):
    pytest.importorskip("PIL")  # Pillow is a Windows-only runtime dependency.
    from PIL import Image, ImageGrab

    monkeypatch.setattr(windows_desktop, "known_folder", lambda name: tmp_path)
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (8, 8), "blue"))
    for _ in range(2):
        assert windows_desktop.run("take_screenshot", {}).ok
    files = list(tmp_path.glob("*.png"))
    assert len(files) == 2
    with Image.open(files[0]) as image:
        assert image.getpixel((0, 0)) == (0, 0, 255)


def test_native_failure_does_not_retry_with_a_shell(monkeypatch, tmp_path):
    from brodi import agent as module

    monkeypatch.setenv("BRODI_OS", "win32")
    monkeypatch.setenv("BRODI_FAST_LAUNCH", "true")
    assistant = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    assistant.context._ctx = MacContext("Explorer", [], [])

    async def reject(*args):
        return ScriptResult(False, "", "Several windows match")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Never fall back after an ambiguous close")

    monkeypatch.setattr(native_launch, "execute", reject)
    monkeypatch.setattr(module, "run_applescript", forbidden)

    async def scenario():
        result = await assistant.handle("close Notepad")
        assert not result.executed and "Several windows" in result.speak
        await assistant.aclose()

    asyncio.run(scenario())


def test_close_fallback_never_force_kills_and_policy_still_checks_native_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    registry = ToolRegistry(learned_path=tmp_path / "learned.json")
    script = registry.get("quit_app").script_win
    assert "CloseMainWindow" in script and "Stop-Process" not in script
    tool = registry.get("window_control")
    tool.script_win = 'Remove-Item "document.txt"'
    assert not tool.is_available(MacContext("Explorer", [], []))
    tool.script_win = ""
    tool.source = "learned"
    assert not tool.is_available(MacContext("Explorer", [], []))
