"""Lifecycle and platform startup contracts, without a live microphone or API calls."""

import json
import plistlib
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brodi import background, tray


@pytest.fixture(autouse=True)
def isolated_service(tmp_path, monkeypatch):
    monkeypatch.setenv("BRODI_DATA_DIR", str(tmp_path / "data"))


def fake_checkout(tmp_path):
    root = tmp_path / "Brodi with spaces"
    for executable in ["Scripts/pythonw.exe", "bin/python"]:
        path = root / ".venv" / executable
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (root / "background_service.py").touch()
    return root


def test_mac_login_job_uses_venv_without_shell_and_no_secrets(tmp_path):
    root = fake_checkout(tmp_path)
    config = plistlib.loads(plistlib.dumps(background.launch_agent_config(root)))
    assert config["ProgramArguments"] == [str(root / ".venv/bin/python"), str(root / "background_service.py"), "run"]
    assert config["WorkingDirectory"] == str(root)
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is False  # Stop stays stopped until Start or next login.
    assert "EnvironmentVariables" not in config


def test_windows_login_registration_roundtrips_without_admin(tmp_path, monkeypatch):
    root = fake_checkout(tmp_path)
    values = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    registry = SimpleNamespace(
        HKEY_CURRENT_USER="current-user",
        REG_SZ=1,
        KEY_SET_VALUE=2,
        CreateKey=Mock(return_value=Key()),
        OpenKey=Mock(return_value=Key()),
        SetValueEx=lambda key, name, reserved, kind, value: values.update({name: value}),
        QueryValueEx=query,
        DeleteValue=lambda key, name: values.pop(name),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(background.sys, "platform", "win32")
    monkeypatch.setattr(
        background,
        "service_command",
        lambda root: [
            r"C:\Brodi with spaces\.venv\Scripts\pythonw.exe",
            r"C:\Brodi with spaces\background_service.py",
            "run",
        ],
    )
    assert not background.installed(root)
    background.install(root)
    assert background.installed(root)
    assert "pythonw.exe" in values[background.identity(root)]
    registry.CreateKey.assert_called_once_with("current-user", background.RUN_KEY)
    background.uninstall(root)
    assert not background.installed(root)


def test_mac_install_bootstraps_user_agent_and_removes_only_its_plist(tmp_path, monkeypatch):
    root = fake_checkout(tmp_path)
    path = tmp_path / "LaunchAgents" / "brodi.plist"
    monkeypatch.setattr(background.sys, "platform", "darwin")
    monkeypatch.setattr(background.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(background, "launch_agent_path", lambda root: path)
    run = Mock()
    monkeypatch.setattr(background.subprocess, "run", run)
    background.install(root)
    assert background.installed(root)
    assert run.call_args.args[0] == ["launchctl", "bootstrap", "gui/501", str(path)]
    background.uninstall(root)
    assert not path.exists()
    assert run.call_args.args[0] == ["launchctl", "bootout", f"gui/501/{background.identity(root)}"]


def test_duplicate_service_lock_is_rejected_and_released(tmp_path):
    path = tmp_path / "instance.lock"
    with background.instance_lock(path) as first:
        assert first
        with background.instance_lock(path) as second:
            assert not second
    with background.instance_lock(path) as after_exit:
        assert after_exit


@pytest.mark.parametrize("created,command,valid", [(123, True, True), (999, True, False), (123, False, False)])
def test_pid_record_is_validated_before_signalling(tmp_path, monkeypatch, created, command, valid):
    root = fake_checkout(tmp_path)
    data = background.service_dir(root)
    data.mkdir(parents=True)
    (data / "process.json").write_text(json.dumps({"pid": 42, "created": 123}))
    process = SimpleNamespace(
        create_time=lambda: created,
        cmdline=lambda: [str(root / "background_service.py")] if command else ["other.py"],
        is_running=lambda: True,
        status=lambda: "running",
    )
    monkeypatch.setattr(background.psutil, "Process", lambda pid: process)
    assert (background.running_process(root) is process) is valid


def test_supervisor_recovers_crash_and_explicit_stop_does_not_restart(tmp_path):
    stop_path = tmp_path / "stop"
    controller = Mock()
    # Child running, then crashes, then successfully restarts.
    controller.processes.side_effect = [[object()], [], [object()]]
    clock = [100.0]
    waits = []

    def wait(delay):
        waits.append(delay)
        clock[0] += 1
        if len(waits) == 3:
            stop_path.touch()

    stop = SimpleNamespace(is_set=lambda: False, wait=wait)
    background.supervise(controller, stop, stop_path, clock=lambda: clock[0])
    controller.start.assert_called_once_with(manage_background=False, wake=True)
    controller.stop.assert_called_once_with(manage_background=False)
    # A stop request already present must never start a child.
    controller.reset_mock()
    background.supervise(controller, stop, stop_path)
    controller.start.assert_not_called()


def test_crash_loop_backs_off_and_remains_stoppable(tmp_path):
    controller = Mock()
    controller.processes.return_value = []
    controller.start.side_effect = RuntimeError("device unavailable")
    clock = [0.0]
    stop = Mock()
    stop.is_set.side_effect = lambda: clock[0] >= 10
    stop.wait.side_effect = lambda delay: clock.__setitem__(0, clock[0] + delay)
    background.supervise(controller, stop, tmp_path / "stop", clock=lambda: clock[0])
    assert controller.start.call_count == 4  # 0, 1, 3, 7, not every half second.
    controller.stop.assert_called_once_with(manage_background=False)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_supervised_child_requires_wake_and_uses_platform_launcher(tmp_path, monkeypatch, platform):
    controller = tray.AgentController(tmp_path)
    controller.log_path = tmp_path / "voice.log"
    monkeypatch.setattr(controller, "processes", list)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "false")
    monkeypatch.setattr(tray.sys, "platform", platform)
    launch = Mock()
    monkeypatch.setattr(tray.subprocess, "Popen", launch)
    controller.start(manage_background=False, wake=True)
    environment = launch.call_args.kwargs["env"]
    assert environment["BRODI_WAKE_REQUIRED"] == "true"
    assert environment["BRODI_BACKGROUND"] == "true"
    assert environment["BRODI_STARTUP_GREETING"] == "false"
    assert environment["BRODI_VOICE_MODE"] == "pipeline"
    command = launch.call_args.args[0]
    if platform == "darwin":
        assert command[1:3] == ["-m", "brodi.console_service"]
        assert command[-2:] == ["--python", controller.python]
    else:
        assert command[-2:] == [str(tmp_path / "agent.py"), "console"]


def test_controller_stops_supervisor_before_voice(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(background, "stop_service", lambda root: order.append("supervisor"))
    controller = tray.AgentController(tmp_path)
    monkeypatch.setattr(controller, "processes", lambda: order.append("voice") or [])
    controller.stop()
    assert order == ["supervisor", "voice"]


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_supervisor_opens_native_tray_on_both_platforms(tmp_path, monkeypatch, platform):
    from contextlib import nullcontext

    root = fake_checkout(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("BRODI_TRAY", "true")
    monkeypatch.setattr(background.sys, "platform", platform)
    monkeypatch.setattr(background.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(background, "load_environment", lambda root: None)
    monkeypatch.setattr(background, "instance_lock", lambda path: nullcontext(True))
    monkeypatch.setattr(background.signal, "signal", lambda *args: None)
    monkeypatch.setattr(background.psutil, "Process", lambda: SimpleNamespace(create_time=lambda: 123))
    supervisor = Mock()
    monkeypatch.setattr(background, "supervise", supervisor)
    launch = Mock()
    monkeypatch.setattr(background.subprocess, "Popen", launch)
    background.run(root)
    assert launch.call_args.args[0] == [background.service_command(root)[0], "-m", "brodi.tray"]
    assert launch.call_args.kwargs["cwd"] == root
    assert supervisor.call_count == 1
