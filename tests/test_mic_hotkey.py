"""Native shortcut contracts and an opt-in test against the host desktop."""

import asyncio
import ctypes
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brodi import hotkey


def test_windows_registers_exact_ctrl_space_without_repeat_and_releases(monkeypatch):
    pending = [True, False]

    def peek(message, window, first, last, remove):
        assert (window, first, last, remove) == (None, 0x0312, 0x0312, 1)
        message._obj.wParam = 1
        return pending.pop(0)

    api = SimpleNamespace(
        RegisterHotKey=Mock(return_value=1), UnregisterHotKey=Mock(), PeekMessageW=Mock(side_effect=peek)
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    backend = hotkey.WindowsHotkey()
    api.RegisterHotKey.assert_called_once_with(None, 1, 0x4002, 0x20)
    assert backend.poll() is True
    assert backend.poll() is False
    backend.close()
    api.UnregisterHotKey.assert_called_once_with(None, 1)


def test_windows_registration_conflict_reports_failure(monkeypatch):
    api = SimpleNamespace(RegisterHotKey=Mock(return_value=0), UnregisterHotKey=Mock(), PeekMessageW=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1409, raising=False)
    with pytest.raises(RuntimeError, match="Control\\+Space is unavailable.*1409"):
        hotkey.WindowsHotkey()


def test_mac_exact_shortcut_held_key_and_release(monkeypatch):
    api = SimpleNamespace(
        **{
            name: Mock(return_value=0)
            for name in (
                "GetApplicationEventTarget",
                "RegisterEventHotKey",
                "UnregisterEventHotKey",
                "ReceiveNextEvent",
                "GetEventKind",
                "ReleaseEvent",
            )
        }
    )
    api.GetApplicationEventTarget.return_value = 12
    api.GetEventKind.side_effect = [5, 5, 6, 5, 6]
    monkeypatch.setattr(ctypes, "CDLL", lambda *args: api)
    backend = hotkey.MacHotkey()
    args = api.RegisterEventHotKey.call_args.args
    assert args[:2] == (49, 4096)
    assert args[3:5] == (12, 1)  # exclusive, delivered to this helper
    assert [backend.poll() for _ in range(5)] == [True, False, False, True, False]
    assert api.ReleaseEvent.call_count == 5
    api.ReceiveNextEvent.return_value = -9875
    assert backend.poll() is False
    backend.close()
    api.UnregisterEventHotKey.assert_called_once()


@pytest.mark.asyncio
async def test_helper_protocol_toggles_and_reports_death(monkeypatch):
    lines = asyncio.StreamReader()
    lines.feed_data(b"ready\n")
    process = SimpleNamespace(stdout=lines, stdin=Mock(), wait=Mock())

    async def wait():
        return 0

    process.wait = wait

    async def spawn(*args, **kwargs):
        assert args[1:] == ("-m", "brodi.hotkey")
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    toggle, error = Mock(), Mock()
    listener = hotkey.GlobalMicHotkey(toggle, error)
    await listener.start()
    lines.feed_data(b"toggle\ntoggle\n")
    lines.feed_eof()
    await listener.reader
    assert toggle.call_count == 2
    error.assert_called_once()
    await listener.aclose()
    process.stdin.close.assert_called_once()


def post_control_space(*, repeats=1):
    """Synthetic key events for an explicitly enabled native desktop smoke test."""
    if sys.platform == "darwin":
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGPreflightPostEventAccess.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        if not cg.CGPreflightPostEventAccess():
            pytest.fail("Native keyboard injection needs macOS Accessibility permission")
        for down in [True] * repeats + [False]:
            event = cg.CGEventCreateKeyboardEvent(None, 49, down)
            cg.CGEventSetFlags(event, 1 << 18)
            cg.CGEventPost(0, event)
            cf.CFRelease(event)
    else:
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint32, ctypes.c_size_t]
        api.keybd_event(0x11, 0, 0, 0)
        for _ in range(repeats):
            api.keybd_event(0x20, 0, 0, 0)
        api.keybd_event(0x20, 0, 2, 0)
        api.keybd_event(0x11, 0, 2, 0)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("BRODI_NATIVE_MIC_TESTS") != "1", reason="opt-in desktop keyboard and speaker test")
async def test_native_shortcut_chimes_and_cleanup(tmp_path, monkeypatch):
    from livekit.agents.voice import io

    from brodi.activity import ActivityLog
    from brodi.microphone import MicrophoneControl

    monkeypatch.setenv("BRODI_ACTIVITY_LOG", str(tmp_path / "activity.jsonl"))

    class Input(io.AudioInput):
        def __init__(self):
            super().__init__(label="Native smoke (no microphone recording)")

    input_io = io.AgentInput(lambda: None, lambda: None)
    input_io.audio = Input()
    session = SimpleNamespace(input=input_io, clear_user_turn=Mock())
    control = MicrophoneControl(session, ActivityLog())
    control.attach()
    await control.start()
    try:
        assert control.hotkey_available and control.listening
        external_keys = os.environ.get("BRODI_NATIVE_MIC_EXTERNAL_KEYS") == "1"
        if external_keys:
            print("READY: Listening. Hold Control+Space to mute.", flush=True)
        else:
            post_control_space(repeats=3)
        async with asyncio.timeout(45 if external_keys else 3):
            while not control.muted:
                await asyncio.sleep(0.01)
        await control.transition
        assert control.muted and not control.listening
        if external_keys:
            print("MUTED: Press Control+Space to resume.", flush=True)
        else:
            post_control_space()
        async with asyncio.timeout(45 if external_keys else 3):
            while not control.listening:
                await asyncio.sleep(0.01)
        assert input_io.audio_enabled
        from brodi.activity import JournalReader

        assert not any(event["kind"] == "error" for event in JournalReader(control.activity.path).poll())
    finally:
        await control.aclose()
    # A fresh helper can own the shortcut immediately after cleanup.
    listener = hotkey.GlobalMicHotkey(lambda: None, lambda error: pytest.fail(error))
    await listener.start()
    await listener.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("BRODI_NATIVE_MIC_TESTS") != "1", reason="opt-in native registration and speaker test"
)
async def test_native_registration_tray_cues_and_release(tmp_path, monkeypatch):
    """Native registration + real speaker output without keyboard injection access."""
    from livekit.agents.voice import io

    from brodi.activity import ActivityLog, JournalReader
    from brodi.mic_control import read_state, request_mute
    from brodi.microphone import MicrophoneControl

    monkeypatch.setenv("BRODI_ACTIVITY_LOG", str(tmp_path / "activity.jsonl"))
    input_io = io.AgentInput(lambda: None, lambda: None)
    input_io.audio = io.AudioInput(label="No recording")
    session = SimpleNamespace(input=input_io, clear_user_turn=Mock())
    control = MicrophoneControl(session, ActivityLog())
    control.attach()
    await control.start()
    try:
        assert control.hotkey_available and control.listening
        for muted in (True, False):
            request_mute(os.getpid(), muted)
            async with asyncio.timeout(4):
                while control.muted != muted:
                    await asyncio.sleep(0.01)
            await control.transition
            assert input_io.audio_enabled is not muted
            assert read_state(os.getpid())["mic_muted"] is muted
        events = JournalReader(control.activity.path).poll()
        assert not any(event["kind"] == "error" for event in events)
    finally:
        await control.aclose()
    assert read_state(os.getpid()) is None
    listener = hotkey.GlobalMicHotkey(lambda: None, lambda error: pytest.fail(error))
    await listener.start()
    await listener.aclose()
