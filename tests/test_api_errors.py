import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from livekit.agents import APIError, APIStatusError
from openai import AuthenticationError

from brodi import api_errors
from brodi.activity import ActivityLog, JournalReader


@pytest.fixture(autouse=True)
def no_real_windows(monkeypatch):
    monkeypatch.setattr(api_errors, "_shown", set())
    monkeypatch.setattr(api_errors.sys, "argv", ["pytest"])
    monkeypatch.delenv("BRODI_BACKGROUND", raising=False)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            SimpleNamespace(
                error=APIError("failed", body={"code": "credit_balance_exhausted", "type": "insufficient_quota"})
            ),
            "credits_exhausted",
        ),
        (APIStatusError("failed", status_code=401), "invalid_key"),
        (APIError("failed", body={"error": {"code": "invalid_api_key"}}), "invalid_key"),
        (APIError("You have no credits remaining."), "credits_exhausted"),
        (APIError("Incorrect API key provided: sk-test-secret"), "invalid_key"),
        (APIError("failed", body={"code": "project_spend_limit_exceeded"}), "quota_exceeded"),
        (APIError("failed", body={"type": "insufficient_quota"}), "quota_exceeded"),
        (RuntimeError("Set OPENAI_API_KEY in .env.local before starting Brodi."), "missing_key"),
        (APIStatusError("Rate limit reached", status_code=429), None),
        (APIStatusError("Service unavailable", status_code=503), None),
        (RuntimeError("microphone disconnected"), None),
    ],
)
def test_error_classification(error, expected):
    assert api_errors.classify_api_error(error) == expected


def test_sdk_error_wrapped_by_livekit_and_cyclic_cause():
    response = httpx2.Response(401, request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    cause = AuthenticationError("Rejected", response=response, body={"code": "invalid_api_key"})
    wrapper = APIError("connection failed")
    wrapper.__cause__ = cause
    cause.__context__ = wrapper
    assert api_errors.classify_api_error(SimpleNamespace(error=wrapper)) == "invalid_key"


def test_report_is_actionable_and_never_copies_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(api_errors.sys, "argv", ["agent.py", "console"])
    notify = Mock()
    monkeypatch.setattr(api_errors, "notify_api_error", notify)
    journal = ActivityLog(tmp_path / "events")
    assert api_errors.report_api_error(APIError("Incorrect API key provided: private-credential"), journal)
    event = JournalReader(journal.path).poll()[0]
    assert event["message"] == "Invalid API key"
    assert "restart Brodi" in event["details"]["reason"]
    assert "private-credential" not in journal.path.read_text()
    notify.assert_called_once_with("invalid_key")


def test_remote_workers_only_record_the_error(tmp_path, monkeypatch):
    notify = Mock()
    monkeypatch.setattr(api_errors, "notify_api_error", notify)
    journal = ActivityLog(tmp_path / "events")
    assert api_errors.report_api_error(APIError("No credits remaining"), journal)
    notify.assert_not_called()


def test_retries_launch_only_one_detached_notice(monkeypatch):
    monkeypatch.setattr(api_errors.sys, "platform", "win32")
    launch = Mock()
    monkeypatch.setattr(api_errors.subprocess, "Popen", launch)
    for _ in range(3):
        api_errors.notify_api_error("invalid_key")
    launch.assert_called_once()
    assert launch.call_args.args[0][-3:] == ["-m", "brodi.api_errors", "invalid_key"]
    assert launch.call_args.kwargs["stdin"] == api_errors.subprocess.DEVNULL


def test_notice_failure_does_not_break_audio_cleanup(monkeypatch):
    monkeypatch.setattr(api_errors.sys, "platform", "win32")
    monkeypatch.setattr(api_errors.subprocess, "Popen", Mock(side_effect=OSError("unavailable")))
    api_errors.notify_api_error("invalid_key")
    assert not api_errors._shown


def test_restarted_workers_do_not_repeat_recent_dismissed_notice(tmp_path, monkeypatch):
    from brodi import background

    monkeypatch.setattr(background, "service_dir", lambda: tmp_path)
    show = Mock()
    monkeypatch.setattr(api_errors, "show_dialog", show)
    api_errors.show_notice("invalid_key")
    api_errors.show_notice("invalid_key")
    show.assert_called_once_with(api_errors.ISSUES["invalid_key"])
    api_errors.show_notice("credits_exhausted")
    assert show.call_count == 2


def test_no_second_window_while_first_is_open(tmp_path, monkeypatch):
    from brodi import background

    monkeypatch.setattr(background, "service_dir", lambda: tmp_path)
    opened, dismiss = threading.Event(), threading.Event()

    def dialog(issue):
        opened.set()
        assert dismiss.wait(5)

    show = Mock(side_effect=dialog)
    monkeypatch.setattr(api_errors, "show_dialog", show)
    worker = threading.Thread(target=api_errors.show_notice, args=("invalid_key",))
    worker.start()
    try:
        assert opened.wait(5)
        api_errors.show_notice("credits_exhausted")
        assert show.call_count == 1
    finally:
        dismiss.set()
        worker.join(5)


@pytest.mark.parametrize("own_window", [True, False])
def test_flashes_only_our_warning_caption_and_taskbar(monkeypatch, own_window):
    import ctypes

    def set_owner(hwnd, pointer):
        pointer._obj.value = api_errors.os.getpid() if own_window else 0

    user32 = SimpleNamespace(
        FindWindowW=Mock(return_value=123),
        GetWindowThreadProcessId=Mock(side_effect=set_owner),
        FlashWindowEx=Mock(),
    )
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=user32), raising=False)
    finished = Mock(wait=Mock(return_value=False))
    api_errors._flash_dialog("Brodi - Invalid API key", finished)
    if own_window:
        user32.FlashWindowEx.assert_called_once()
        info = user32.FlashWindowEx.call_args.args[0]._obj
        assert (info.hwnd, info.dwFlags, info.uCount, info.dwTimeout) == (123, 3, 6, 500)
    else:
        user32.FlashWindowEx.assert_not_called()


def test_command_api_failures_report_the_actual_problem(tmp_path, monkeypatch):
    from brodi.agent import DynamicMacAgent
    from brodi.registry import ToolRegistry

    journal = ActivityLog(tmp_path / "events")
    core = DynamicMacAgent(
        ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False, activity=journal
    )
    monkeypatch.setattr(core, "_handle", AsyncMock(side_effect=APIError("No credits remaining")))

    async def check():
        outcome = await core.handle("a task")
        assert not outcome.executed
        await core.aclose()

    asyncio.run(check())
    errors = [event for event in JournalReader(journal.path).poll() if event["kind"] == "error"]
    assert errors[0]["message"] == "API credits exhausted"
