import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from brodi.activity import ActivityLog, JournalReader, clean, render_event, view


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
