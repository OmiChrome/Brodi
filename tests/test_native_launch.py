from unittest.mock import Mock

from brodi import native_launch


def test_trusted_notepad_launch_is_an_argument_vector_without_a_shell(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(native_launch.subprocess, "Popen", popen)
    monkeypatch.setattr(native_launch.shutil, "which", lambda name: "C:/Windows/notepad.exe")
    result = native_launch.launch("open_app", {"app": "Notepad"})
    assert result.ok
    assert popen.call_args.args == (["C:/Windows/notepad.exe"],)
    assert not popen.call_args.kwargs.get("shell", False)


def test_unsupported_names_fall_back_without_launching(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(native_launch.subprocess, "Popen", popen)
    assert native_launch.launch("open_app", {"app": "Notepad; arbitrary command"}) is None
    assert native_launch.launch("terminal_run", {"command": "anything"}) is None
    popen.assert_not_called()


def test_launch_failure_surfaces_an_error(monkeypatch):
    monkeypatch.setattr(native_launch.subprocess, "Popen", Mock(side_effect=OSError("App unavailable")))
    result = native_launch.launch("open_app", {"app": "Notepad"})
    assert not result.ok and "unavailable" in result.error
