import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brodi.windows_uia import UIAError, WindowsUIA, WindowTarget, check_target, literal_keys


@pytest.fixture(autouse=True)
def process_name(monkeypatch):
    monkeypatch.setattr("psutil.Process", lambda pid: SimpleNamespace(name=lambda: "notepad.exe"))


def editor(text=""):
    field = Mock()
    field.element_info = SimpleNamespace(control_type="Edit", is_password=False, automation_id="editor")
    field.get_value.side_effect = lambda: field.value
    field.value = text
    field.set_edit_text.side_effect = lambda value: setattr(field, "value", value)
    return field


def helper(field):
    window = Mock()
    window.window_text.return_value = "Test - Notepad"
    window.process_id.return_value = os.getpid()
    window.descendants.return_value = [field]
    window.is_minimized.return_value = False
    desktop = Mock()
    desktop.windows.return_value = [window]
    return WindowsUIA(desktop), window


def test_text_append_preserves_document_and_reads_back():
    field = editor("existing ")
    uia, _ = helper(field)
    result = uia.set_text(WindowTarget(title="Notepad"), "hello world")
    assert result["text"] == "existing hello world" and result["verified"]


def test_ambiguous_windows_are_rejected():
    uia, window = helper(editor())
    uia.desktop.windows.return_value = [window, window]
    with pytest.raises(UIAError, match="More than one"):
        uia.connect(WindowTarget(title="Notepad"))


def test_text_field_can_finish_loading_after_window_appears():
    field = editor()
    uia, window = helper(field)
    window.descendants.side_effect = [[], [field]]
    assert uia.find_edit(window) is field


def test_failed_call_does_not_duplicate_text_already_inserted():
    field = editor()

    def partially_succeeded(text):
        field.value = text
        raise RuntimeError("COM failed after setting text")

    field.set_edit_text.side_effect = partially_succeeded
    uia, _ = helper(field)
    assert uia.set_text(WindowTarget(title="Notepad"), "hello")["verified"]
    field.type_keys.assert_not_called()


def test_key_syntax_is_literal():
    assert literal_keys("hello +^%{}") == "hello {+}{^}{%}{{}{}}"


def test_replacing_existing_text_needs_confirmation():
    field = editor("existing notes")
    uia, _ = helper(field)
    with pytest.raises(UIAError, match="confirmation"):
        uia.set_text(WindowTarget(title="Notepad"), "replacement", append=False)
    field.set_edit_text.assert_not_called()


@pytest.mark.parametrize("target", ["PowerShell", "cmd.exe", "Windows Terminal", "1Password"])
def test_sensitive_windows_are_blocked(target):
    with pytest.raises(UIAError):
        check_target(target)
