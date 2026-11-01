import shlex
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from brodi import tray
from brodi.activity import ActivityLog


@pytest.fixture(autouse=True)
def isolated_desktop(tmp_path, monkeypatch):
    from brodi import background

    monkeypatch.setenv("BRODI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BRODI_ACTIVITY_LOG", str(tmp_path / "events"))
    monkeypatch.setattr(background, "installed", lambda root: False)
    monkeypatch.setattr(background, "stop_service", lambda root: None)


def process(pid, command, cwd, *, parent=0, name="python.exe"):
    return SimpleNamespace(
        pid=pid,
        info={"name": name, "cmdline": command},
        cwd=lambda: str(cwd),
        ppid=lambda: parent,
        children=lambda **kw: [],
        terminate=Mock(),
        kill=Mock(),
        cmdline=lambda: command,
    )


def test_process_matching_requires_exact_entrypoint_and_checkout(tmp_path):
    root = tmp_path / "brodi"
    other = tmp_path / "other"
    assert tray.owns_agent(["python", str(root / "agent.py"), "console"], str(other), root)
    assert tray.owns_agent(["python", "agent.py", "console"], str(root), root)
    assert tray.owns_agent(["python", "-m", "brodi.wake_runtime"], str(root), root)
    assert not tray.owns_agent(["python", "agent.py", "console"], str(other), root)
    assert not tray.owns_agent(["python", str(root / "agent.py"), "dev"], str(root), root)
    assert not tray.owns_agent(["python", "fake-agent.py", "console"], str(root), root)
    assert not tray.owns_agent(["python", "-m", "brodi.wake_runtime"], str(other), root)


def test_only_checkout_agents_are_discovered_and_leaf_pid_is_used(tmp_path, monkeypatch):
    root = tmp_path / "brodi"
    launcher = process(10, ["python", str(root / "agent.py"), "console"], root)
    child = process(11, ["python", str(root / "agent.py"), "console"], root, parent=10)
    unrelated = process(12, ["python", "agent.py", "console"], tmp_path / "other")
    inaccessible = Mock(info={"name": "python.exe", "cmdline": []})
    inaccessible.cwd.side_effect = psutil.AccessDenied()
    monkeypatch.setattr(tray.psutil, "process_iter", lambda attrs: [launcher, child, unrelated, inaccessible])
    controller = tray.AgentController(root)
    assert controller.processes() == [launcher, child]
    assert controller.leaf_pid(controller.processes()) == 11


def test_stop_ends_assistant_and_uia_but_leaves_opened_apps(tmp_path, monkeypatch):
    root = tmp_path / "brodi"
    agent = process(10, ["python", "agent.py", "console"], root)
    uia = process(11, ["python", "-m", "brodi.windows_uia", "--worker"], root)
    chrome = process(12, ["chrome.exe"], root, name="chrome.exe")
    agent.children = lambda **kw: [uia, chrome]
    controller = tray.AgentController(root)
    monkeypatch.setattr(controller, "processes", lambda: [agent])
    monkeypatch.setattr(tray.psutil, "wait_procs", lambda processes, timeout: (processes, []))
    controller.stop()
    agent.terminate.assert_called_once()
    uia.terminate.assert_called_once()
    chrome.terminate.assert_not_called()
    chrome.kill.assert_not_called()


def test_start_is_idempotent_and_uses_hidden_argument_vector(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    controller = tray.AgentController(tmp_path)
    controller.log_path = tmp_path / "voice.log"
    existing = [object()]
    monkeypatch.setattr(controller, "processes", lambda: existing)
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.start()
    launch.assert_not_called()
    existing.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("BRODI_LOCAL_WAKE", "false")
    controller.start()
    assert launch.call_args.args[0][-2:] == [str(tmp_path / "agent.py"), "console"]
    assert not launch.call_args.kwargs.get("shell", False)
    assert launch.call_args.kwargs["creationflags"] == getattr(tray.subprocess, "CREATE_NO_WINDOW", 0)


def test_start_without_key_reports_setup_error(tmp_path, monkeypatch):
    controller = tray.AgentController(tmp_path)
    monkeypatch.setattr(controller, "processes", list)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        controller.start()


def test_activity_and_log_use_argument_vectors_and_current_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    controller = tray.AgentController(tmp_path)
    controller.log_path = tmp_path / "voice log.log"
    monkeypatch.setattr(controller, "processes", lambda: [process(30, [], tmp_path)])
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.show_activity()
    assert launch.call_args.args[0][-2:] == ["--pid", "30"]
    assert launch.call_args.kwargs["creationflags"] == getattr(tray.subprocess, "CREATE_NEW_CONSOLE", 0)
    controller.show_log()
    assert launch.call_args.args[0] == ["notepad.exe", str(controller.log_path)]
    assert controller.log_path.exists()
    assert not launch.call_args.kwargs.get("shell", False)


def test_status_uses_current_agent_events_and_clears_stale_state(tmp_path, monkeypatch):
    path = tmp_path / "events"
    monkeypatch.setattr(tray, "default_path", lambda: path)
    controller = Mock()
    controller.leaf_pid.return_value = 20
    controller.processes.return_value = [SimpleNamespace(pid=20)]
    app = tray.TrayApp(controller)
    journal = ActivityLog(path)
    monkeypatch.setattr("brodi.activity.os.getpid", lambda: 20)
    journal.emit("status", "Listening locally for Hey bro or Yo bro")
    app.refresh()
    assert app.running and "Listening" in app.status
    controller.processes.return_value = []
    app.refresh()
    assert not app.running and app.status == "Stopped" and not app.states


def test_quit_stops_assistant_and_icon_and_busy_actions_do_not_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "default_path", lambda: tmp_path / "events")
    controller = Mock()
    app = tray.TrayApp(controller)
    app.icon = Mock()
    app.busy.acquire()
    assert not app.perform("restart")
    controller.restart.assert_not_called()
    app.busy.release()
    assert app.perform("quit")
    controller.stop.assert_called_once()
    app.icon.stop.assert_called_once()
    assert app.closing.is_set()


def test_errors_are_reported_without_crashing_tray(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "default_path", lambda: tmp_path / "events")
    controller = Mock()
    controller.start.side_effect = RuntimeError("Missing local voice model")
    app = tray.TrayApp(controller)
    app.icon = Mock()
    assert not app.perform("start")
    assert not app.busy.locked()
    app.icon.notify.assert_called_once_with("Missing local voice model", "Brodi needs attention")


@pytest.mark.skipif(sys.platform not in {"win32", "darwin"}, reason="Native tray backend")
def test_menu_tracks_running_state_and_uses_official_icon(tmp_path, monkeypatch):
    import pystray
    from PIL import Image

    monkeypatch.setattr(tray, "default_path", lambda: tmp_path / "events")
    app = tray.TrayApp(Mock())
    menu = app.menu(pystray)
    items = {item.text: item for item in menu if item.text}
    assert items["Start assistant"].enabled and not items["Stop assistant"].enabled
    app.running = True
    assert not items["Start assistant"].enabled and items["Stop assistant"].enabled
    assert items["Show activity"].default
    with Image.open(tray.ROOT / "assets/branding/brodi.ico") as image:
        assert (16, 16) in image.ico.sizes() and (256, 256) in image.ico.sizes()


def test_mac_launcher_is_owned_only_by_matching_checkout(tmp_path):
    command = ["python", "-m", "brodi.console_service", "voice.log"]
    assert tray.owns_agent(command, str(tmp_path), tmp_path)
    assert not tray.owns_agent(command, str(tmp_path / "other"), tmp_path)


def test_mac_start_uses_detached_terminal_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    controller = tray.AgentController(tmp_path)
    controller.log_path = tmp_path / "voice log.log"
    monkeypatch.setattr(controller, "processes", list)
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.start()
    assert launch.call_args.args[0] == [
        controller.python,
        "-m",
        "brodi.console_service",
        str(controller.log_path),
        "--python",
        controller.python,
    ]
    assert launch.call_args.kwargs["start_new_session"]
    assert "creationflags" not in launch.call_args.kwargs


def test_windows_start_keeps_configured_audio_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("BRODI_INPUT_DEVICE", "USB microphone")
    monkeypatch.setenv("BRODI_OUTPUT_DEVICE", "Headphones")
    controller = tray.AgentController(tmp_path)
    monkeypatch.setattr(controller, "processes", list)
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.start()
    assert launch.call_args.args[0][-4:] == ["--input-device", "USB microphone", "--output-device", "Headphones"]


def test_macos_activity_opens_terminal_and_quotes_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    root = tmp_path / "Brodi's $(folder)"
    root.mkdir()
    controller = tray.AgentController(root)
    monkeypatch.setattr(controller, "processes", lambda: [process(30, [], root)])
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.show_activity()
    arguments = launch.call_args.args[0]
    assert arguments[:3] == ["/usr/bin/open", "-a", "Terminal"]
    command = tray.Path(arguments[3])
    lines = command.read_text().splitlines()
    assert shlex.split(lines[1]) == ["cd", "--", str(root)]
    assert shlex.split(lines[2]) == [
        "exec",
        controller.python,
        "-m",
        "brodi.activity",
        "--path",
        str(tray.default_path()),
        "--pid",
        "30",
    ]
    assert command.stat().st_mode & 0o777 == 0o700
    controller.log_path = tmp_path / "voice log.log"
    controller.show_log()
    assert launch.call_args.args[0] == ["/usr/bin/open", "-a", "TextEdit", str(controller.log_path)]


def test_activity_without_agent_shows_history(tmp_path, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    controller = tray.AgentController(tmp_path)
    monkeypatch.setattr(controller, "processes", list)
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.show_activity()
    assert launch.call_args.args[0][-1] == "--once"
    assert "--pid" not in launch.call_args.args[0]


def test_mic_state_survives_other_activity_and_clears_on_stop(tmp_path, monkeypatch):
    controller = Mock()
    controller.leaf_pid.return_value = 20
    controller.processes.return_value = [SimpleNamespace(pid=20)]
    app = tray.TrayApp(controller)
    journal = ActivityLog()
    monkeypatch.setattr("brodi.activity.os.getpid", lambda: 20)
    journal.emit("status", "Mic muted", mic_muted=True, mic_available=True, hotkey_available=True)
    journal.emit("status", "Opening Chrome")
    app.refresh()
    assert app.summary == "Mic muted" and app.status == "Opening Chrome"
    assert app.mic_available and app.hotkey_available and app.visual_state == "muted"
    assert app.perform("toggle_mic")
    controller.set_mic_muted.assert_called_once_with(20, False)
    # State changes only when voice acknowledges the request.
    assert app.mic_muted is True
    controller.processes.return_value = []
    app.refresh()
    assert app.mic_muted is None and not app.mic_available and not app.hotkey_available
    assert not app.mic_states and not app.hotkey_states


def test_tray_reads_mailbox_snapshot_when_journal_has_no_mic_event(tmp_path, monkeypatch):
    from brodi.mic_control import MicrophoneMailbox

    monkeypatch.setattr("brodi.mic_control.os.getpid", lambda: 20)
    mailbox = MicrophoneMailbox()
    mailbox.publish(muted=True, available=True, hotkey_available=False)
    controller = Mock()
    controller.leaf_pid.return_value = 20
    controller.processes.return_value = [SimpleNamespace(pid=20)]
    app = tray.TrayApp(controller)
    app.refresh()
    assert app.summary == "Mic muted" and app.mic_available
    assert not app.hotkey_available


def test_controller_delivers_mute_request_to_current_session(tmp_path, monkeypatch):
    from brodi.mic_control import MicrophoneMailbox

    monkeypatch.setattr("brodi.mic_control.os.getpid", lambda: 20)
    mailbox = MicrophoneMailbox()
    mailbox.publish(muted=False, available=True, hotkey_available=True)
    controller = tray.AgentController(tmp_path)
    monkeypatch.setattr(controller, "processes", lambda: [SimpleNamespace(pid=20)])
    controller.set_mic_muted(20, True)
    assert mailbox.poll() is True
    with pytest.raises(RuntimeError, match="session ended"):
        controller.set_mic_muted(21, False)


@pytest.mark.skipif(sys.platform not in {"win32", "darwin"}, reason="Native tray backend")
def test_menu_mic_control_and_shortcut_fallback(tmp_path, monkeypatch):
    import pystray

    app = tray.TrayApp(Mock())
    app.running, app.mic_available, app.mic_muted = True, True, True
    items = {item.text: item for item in app.menu(pystray)}
    assert items["Unmute microphone"].enabled
    assert "Shortcut unavailable — use the microphone control" in items
    app.hotkey_available, app.mic_muted = True, False
    items = {item.text: item for item in app.menu(pystray)}
    mute = next(item for text, item in items.items() if text.startswith("Mute microphone"))
    assert "Space" in mute.text
    assert "Shortcut unavailable — use the microphone control" not in items
    app.busy.acquire()
    assert not mute.enabled
    app.busy.release()


def test_notification_failure_keeps_error_visible_and_unlocks(tmp_path, monkeypatch):
    app = tray.TrayApp(Mock())
    app.controller.start.side_effect = RuntimeError("Setup needed")
    app.icon = Mock()
    app.icon.notify.side_effect = NotImplementedError()
    assert not app.perform("start")
    assert app.error == "Setup needed" and not app.busy.locked()
    assert app.visual_state == "attention"


def test_callback_reserves_action_before_launching_worker(tmp_path, monkeypatch):
    app = tray.TrayApp(Mock())
    thread = Mock()
    monkeypatch.setattr(tray.threading, "Thread", thread)
    app.callback("start")(None, None)
    app.callback("stop")(None, None)
    assert thread.call_count == 1
    assert app.busy.locked()
    app.busy.release()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS flock")
def test_mac_instance_lock_is_per_checkout_and_released(tmp_path):
    first = tray.acquire_instance(tmp_path)
    try:
        assert tray.acquire_instance(tmp_path) is None
        other = tray.acquire_instance(tmp_path / "other")
        assert other is not None
        other.close()
    finally:
        first.close()
    again = tray.acquire_instance(tmp_path)
    assert again is not None
    again.close()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_tray_icons_are_distinct_and_transparent(platform, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", platform)
    images = [tray.tray_image(state) for state in ("listening", "muted", "stopped")]
    assert all(image.size == (64, 64) and image.mode == "RGBA" for image in images)
    assert len({image.tobytes() for image in images}) == 3
    if platform == "darwin":
        assert images[0].getpixel((0, 0))[3] == 0
