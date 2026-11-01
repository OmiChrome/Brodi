import ctypes
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brodi import activity
from brodi.activity import ActivityLog, JournalReader, clean, render_event, view


@pytest.mark.parametrize(
    "available,muted,message,label,color",
    [
        (True, True, "Mic muted", "MUTED", "41"),
        (True, False, "Listening for Hey bro", "LISTENING", "42"),
        (True, True, "Resuming microphone", "RESUMING", "43"),
        (False, True, "Mic muted", "CONNECTING", "43"),
    ],
)
def test_mic_status_has_explicit_label_and_color(available, muted, message, label, color):
    event = {
        "time": 1,
        "kind": "status",
        "message": message,
        "details": {"mic_available": available, "mic_muted": muted, "hotkey_available": True},
    }
    status = activity.microphone_status(event)
    assert status.label == label and status.color.endswith(color)
    text = render_event(event, color=True)
    assert label in text and "MIC" in text
    assert "Mic muted:" not in text and "Hotkey available:" not in text
    assert "\x1b" not in render_event(event)


def test_mic_shortcut_failure_shows_tray_fallback():
    status = activity.microphone_status(
        {"message": "Mic muted", "details": {"mic_available": True, "mic_muted": True, "hotkey_available": False}}
    )
    assert "tray" in status.hint and "shortcut unavailable" in status.hint


def test_live_banner_keeps_current_mic_outside_tail_and_updates(monkeypatch, tmp_path):
    journal = ActivityLog(tmp_path / "events")
    journal.emit("status", "Mic muted", mic_available=True, mic_muted=True, hotkey_available=True)
    journal.emit("action", "Opening Calculator")
    monkeypatch.setattr(activity, "_supports_color", lambda stream: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(activity.shutil, "get_terminal_size", lambda *args: os.terminal_size((80, 24)))
    updates = iter([("Resuming microphone", True), ("Listening for Hey bro", False)])

    def tick(seconds):
        update = next(updates, None)
        if update is None:
            raise KeyboardInterrupt
        journal.emit("status", update[0], mic_available=True, mic_muted=update[1], hotkey_available=True)
        journal.emit("action", "Opening Calculator")

    monkeypatch.setattr(activity.time, "sleep", tick)
    output = io.StringIO()
    view(journal.path, tail=1, stream=output)
    frames = output.getvalue().split("\x1b[H")[1:]
    for frame, label in zip(frames, ("MUTED", "RESUMING", "LISTENING"), strict=True):
        assert f"MIC {label}" in frame.splitlines()[2]
        assert "Opening Calculator" in frame
    assert "\x1b[?25h\x1b[?1049l" in output.getvalue()
    assert output.getvalue().endswith("Activity view closed. Brodi keeps running.\n")


@pytest.mark.parametrize("query_ok,enable_ok,expected", [(1, 1, True), (0, 1, False), (1, 0, False)])
def test_windows_console_enables_ansi_or_falls_back(monkeypatch, query_ok, enable_ok, expected):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 0x123456789))

    def get_mode(handle, mode):
        mode._obj.value = 3
        return query_ok

    api = SimpleNamespace(GetConsoleMode=Mock(side_effect=get_mode), SetConsoleMode=Mock(return_value=enable_ok))
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    stream = Mock(isatty=lambda: True, fileno=lambda: 1)
    assert activity._supports_color(stream) is expected
    if query_ok:
        api.SetConsoleMode.assert_called_once_with(0x123456789, 7)
    else:
        api.SetConsoleMode.assert_not_called()


@pytest.mark.parametrize("setting,value", [("NO_COLOR", "1"), ("NO_COLOR", ""), ("TERM", "dumb")])
def test_color_opt_out_never_writes_terminal_controls(monkeypatch, tmp_path, setting, value):
    monkeypatch.setenv(setting, value)

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    output = Terminal()
    view(tmp_path / "missing", follow=False, stream=output)
    assert "\x1b" not in output.getvalue()


def test_journal_records_ordered_input_action_and_output(tmp_path):
    path = tmp_path / "activity.jsonl"
    journal = ActivityLog(path)
    journal.emit("input", "Hey bro, open Chrome")
    journal.emit("action", "Opening Chrome")
    journal.emit("output", "Got it.")
    events = JournalReader(path).poll()
    assert [e["kind"] for e in events] == ["input", "action", "output"]
    assert len({e["session"] for e in events}) == 1
    assert all(e["pid"] == os.getpid() for e in events)


def test_duplicate_status_is_coalesced_but_repeated_commands_are_kept(tmp_path):
    journal = ActivityLog(tmp_path / "events")
    journal.emit("status", "Listening for a task")
    journal.emit("status", "Listening for a task")
    journal.emit("input", "open Chrome")
    journal.emit("input", "open Chrome")
    assert len(JournalReader(journal.path).poll()) == 3


def test_secrets_and_terminal_controls_are_not_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-credential")
    journal = ActivityLog(tmp_path / "events")
    journal.emit(
        "input",
        "\x1b[31mopen Chrome\x1b[0m private-test-credential",
        password="do-not-print",
        text="sk-abcdefghijklmnopqrstuvwxyz",
    )
    raw = journal.path.read_text(encoding="utf-8")
    assert "private-test-credential" not in raw
    assert "do-not-print" not in raw
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw
    assert "[redacted]" in raw
    assert "\x1b" not in clean("\x1b]0;spoofed title\x07hello")


def test_journal_write_failure_does_not_break_commands(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    ActivityLog(blocker / "events").emit("status", "Listening")


def test_browser_thread_and_voice_thread_share_valid_jsonl(tmp_path):
    journal = ActivityLog(tmp_path / "events")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: journal.emit("observation", f"Page {i}"), range(40)))
    assert len(JournalReader(journal.path).poll()) == 40


def test_reader_waits_for_partial_line_and_skips_broken_records(tmp_path):
    path = tmp_path / "events"
    reader = JournalReader(path)
    assert reader.poll() == []
    line = json.dumps({"kind": "status", "message": "Listening", "time": 100})
    path.write_text(line[:20])
    assert reader.poll() == []
    with path.open("a") as stream:
        stream.write(line[20:] + "\nnot json\n")
    assert [e["message"] for e in reader.poll()] == ["Listening"]
    assert reader.poll() == []


def test_reader_recovers_after_truncation(tmp_path):
    path = tmp_path / "events"
    journal = ActivityLog(path)
    journal.emit("status", "A longer old status to ensure truncation")
    reader = JournalReader(path)
    reader.poll()
    path.write_text('{"kind":"status","message":"New","time":1}\n')
    assert reader.poll()[0]["message"] == "New"


def test_render_is_readable_and_wraps_gathered_information():
    rendered = render_event(
        {
            "time": 100,
            "kind": "observation",
            "message": "Reading the page",
            "details": {"title": "YouTube", "visible_text": "cats and dogs " * 80},
        },
        width=70,
    )
    assert "GATHERED" in rendered and "Title: YouTube" in rendered
    assert "Visible text:" in rendered
    assert all(len(line) <= 70 for line in rendered.splitlines())
    assert "more in the activity journal" in rendered


def test_snapshot_selects_latest_session_and_has_no_debug_noise(tmp_path):
    path = tmp_path / "events"
    ActivityLog(path).emit("status", "Old session")
    journal = ActivityLog(path)
    journal.emit("input", "Hey bro, open Chrome")
    journal.emit("action", "Opening Chrome")
    journal.emit("status", "Listening for a task")
    output = io.StringIO()
    assert view(path, follow=False, stream=output) == 0
    text = output.getvalue()
    assert "Old session" not in text
    assert "YOU" in text and "DOING" in text
    assert "Opening Chrome" in text and "Listening for a task" in text
    assert "Traceback" not in text


def test_pid_filter_does_not_present_old_process_as_current(tmp_path):
    path = tmp_path / "events"
    ActivityLog(path).emit("status", "Old listening state")
    output = io.StringIO()
    view(path, follow=False, pid=-1, stream=output)
    assert "Old listening state" not in output.getvalue()


def test_keyboard_interrupt_only_closes_view(monkeypatch, tmp_path):
    import brodi.activity as activity

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(activity.time, "sleep", interrupt)
    output = io.StringIO()
    assert view(tmp_path / "missing", stream=output) == 0
    assert "Brodi keeps running" in output.getvalue()


def test_q_exits_interactive_view_without_stopping_process(monkeypatch, tmp_path):
    class Keys(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Keys("q\n"))
    output = io.StringIO()
    assert view(tmp_path / "missing", pid=os.getpid(), stream=output) == 0
    assert "Activity view closed" in output.getvalue()


def test_invalid_timestamp_does_not_break_rendering():
    text = render_event({"time": float("inf"), "kind": "status", "message": "Listening"})
    assert "Listening" in text and "--:--:--" in text


def test_live_view_streams_another_process_and_exits_when_it_stops(tmp_path):
    path = tmp_path / "events"
    code = "import sys,time; from pathlib import Path; from brodi.activity import ActivityLog; log=ActivityLog(Path(sys.argv[1])); log.emit('status','Listening for a task'); time.sleep(0.5); log.emit('input','Hey bro, open Chrome'); log.emit('action','Opening Chrome'); log.emit('output','Got it.'); time.sleep(0.5)"
    worker = subprocess.Popen([sys.executable, "-c", code, str(path)])
    try:
        result = subprocess.run(
            [sys.executable, "-m", "brodi.activity", "--path", str(path), "--pid", str(worker.pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "Opening Chrome" in result.stdout
        assert "Hey bro, open Chrome" in result.stdout
        assert "Got it." in result.stdout
        assert "Brodi is stopped." in result.stdout
    finally:
        if worker.poll() is None:
            worker.terminate()
        worker.wait(timeout=5)
