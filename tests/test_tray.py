from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from brodi import tray
from brodi.activity import ActivityLog


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


@pytest.mark.skipif(tray.sys.platform != "win32", reason="Windows-only pystray backend")
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
